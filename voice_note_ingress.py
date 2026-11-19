"""Save ordinary voice-note receipts before ACK and resolve them off the HTTP path."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .managed_inbox import ManagedInbox, boot_id
from .managed_work import DeliveryWork, scope
from .transcription import completion_channel, RECEIVED, message_of, pending, poll_message

logger = logging.getLogger(__name__)


class VoiceNoteIngress:
    """Use the existing private, locked inbox for ordinary pending originals."""

    def __init__(self, adapter, path: Path):
        self.adapter = adapter
        activation = str(uuid5(NAMESPACE_URL, f"{adapter._base_url}|{adapter._identity_id}|voice-notes"))
        self.inbox = ManagedInbox(path, activation)
        self.db = self.inbox.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS voice_note_resolution (
            event_id TEXT PRIMARY KEY, deadline REAL, polled INTEGER NOT NULL DEFAULT 0,
            result TEXT, completion TEXT, created REAL NOT NULL)""")
        self.db.execute("DELETE FROM voice_note_resolution WHERE created<? AND event_id NOT IN (SELECT event_id FROM deliveries WHERE state!='completed')",
                        (time.time() - 7 * 86400,))
        if "retry_at" not in {row[1] for row in self.db.execute("PRAGMA table_info(voice_note_resolution)")}:
            self.db.execute("ALTER TABLE voice_note_resolution ADD COLUMN retry_at REAL")
        self.tasks: dict[str, asyncio.Task] = {}
        self.running = False
        self.closing = False

    @staticmethod
    def key(channel: str, message_id: str) -> str:
        """Keep all event retries for one channel/source under one receipt."""
        return "evt_" + hashlib.sha256(f"{channel}:{message_id}".encode()).hexdigest()

    def start(self) -> None:
        """Resume saved waits only after the normal channel receivers are ready."""
        self.running = True
        for (event_id,) in self.db.execute("SELECT event_id FROM deliveries WHERE state='pending'").fetchall():
            self._schedule(event_id)
        if self.db.execute("SELECT 1 FROM deliveries WHERE state='unknown' LIMIT 1").fetchone():
            logger.error("A voice-note admission has an unknown outcome and requires review before replay")

    def accept(self, envelope: dict) -> None:
        """Persist the unchanged signed envelope and original deadline before ACK."""
        channel = RECEIVED[envelope["event_type"]]
        message = message_of(envelope, channel)
        event_id = self.key(channel, str(message["id"]))
        receipt = {
            "id": event_id, "event_type": envelope["event_type"],
            "original": copy.deepcopy(envelope),
            "hosted_delivery": {"version": 1, "activation_id": self.inbox.activation_id,
                                "event_id": event_id, "starts_agent": True},
        }
        now = time.time()
        self.db.execute("""INSERT INTO voice_note_resolution(event_id,deadline,created) VALUES(?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET deadline=COALESCE(voice_note_resolution.deadline,excluded.deadline)""",
                        (event_id, now + self.adapter._voice_note_receiver().wait_seconds, now))
        self.inbox.accept(receipt)
        if self.running:
            self._schedule(event_id)

    def complete(self, envelope: dict) -> None:
        """Persist callbacks without scheduling or waking a model."""
        channel = completion_channel(envelope)
        if not channel:
            return
        message = message_of(envelope, channel)
        if not message.get("id") or message.get("direction") == "outbound" or pending(message):
            return
        event_id = self.key(channel, str(message["id"]))
        self.db.execute("""DELETE FROM voice_note_resolution WHERE event_id IN (
            SELECT r.event_id FROM voice_note_resolution r LEFT JOIN deliveries d USING(event_id)
            WHERE d.event_id IS NULL ORDER BY r.created DESC LIMIT -1 OFFSET 1023)""")
        self.db.execute("""INSERT INTO voice_note_resolution(event_id,completion,created) VALUES(?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET completion=excluded.completion WHERE voice_note_resolution.result IS NULL""",
                        (event_id, json.dumps(message, allow_nan=False), time.time()))
        self.adapter._voice_note_receiver().complete(channel, message)

    def completion(self, channel: str, message_id: str) -> dict | None:
        """Read a saved callback for an already-durable conversation consumer."""
        row = self.db.execute("SELECT completion FROM voice_note_resolution WHERE event_id=?", (self.key(channel, message_id),)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def _schedule(self, event_id: str) -> None:
        if self.closing or event_id in self.tasks:
            return
        row = self.db.execute("SELECT state FROM deliveries WHERE event_id=?", (event_id,)).fetchone()
        if not row or row[0] != "pending":
            return
        task = self.tasks[event_id] = asyncio.create_task(self._resume(event_id))
        def finished(done):
            self.tasks.pop(event_id, None)
            if not done.cancelled() and done.exception() is not None:
                logger.error("Saved voice-note receipt could not be admitted (%s)", type(done.exception()).__name__)
        task.add_done_callback(finished)

    async def _resume(self, event_id: str) -> None:
        while await self._deliver(event_id):
            pass  # Only an explicit pre-admission refusal restores pending state.

    async def _deliver(self, event_id: str) -> bool | None:
        row = self.db.execute("""SELECT d.payload,r.deadline,r.result,r.completion
            FROM deliveries d JOIN voice_note_resolution r USING(event_id) WHERE d.event_id=? AND d.state='pending'""",
                              (event_id,)).fetchone()
        if row is None:
            return
        payload, deadline, result, completion = row
        envelope = json.loads(payload)["original"]
        channel = RECEIVED[envelope["event_type"]]
        message = message_of(envelope, channel)
        gate = self.adapter._voice_note_receiver()
        if result is None:
            if completion:
                gate.complete(channel, json.loads(completion))
            async def poll():
                changed = self.db.execute("UPDATE voice_note_resolution SET polled=1 WHERE event_id=? AND polled=0", (event_id,))
                if not changed.rowcount:
                    return message
                return await poll_message(self.adapter, channel, message)
            message = await gate.resolve(channel, message, poll, deadline=deadline)
            self.db.execute("UPDATE voice_note_resolution SET result=? WHERE event_id=?", (json.dumps(message), event_id))
        else:
            message = json.loads(result)
        retry_at, = self.db.execute("SELECT retry_at FROM voice_note_resolution WHERE event_id=?", (event_id,)).fetchone()
        if retry_at and retry_at > time.time():
            await asyncio.sleep(retry_at - time.time())
        while not callable(getattr(self.adapter, "_message_handler", None)):
            self.db.execute("UPDATE voice_note_resolution SET retry_at=? WHERE event_id=?", (time.time() + 5, event_id))
            await asyncio.sleep(5)
        gate.remember(channel, message)
        envelope["data"]["text_message" if channel == "text" else "message"] = message
        # Keep a wait replayable until it crosses into the existing model-admission path.
        claimed = self.db.execute("UPDATE deliveries SET state='dispatching',boot_id=? WHERE event_id=? AND state='pending'",
                                  (boot_id(), event_id))
        if not claimed.rowcount:
            return
        completed = False
        rejection = None
        retry = False
        try:
            work = DeliveryWork()
            with scope((work,)):
                response = await self.adapter._route_inkbox_event(envelope["event_type"], envelope, saved_voice_note=True)
            work.seal()
            retry = bool(response.get("native_receiver_starting")) and not work.parts and not work.failed
            completed = (200 <= response.status < 300 or response.status == 422) and await work.done
            if not completed and not work.uncertain:
                rejection = work.rejection
        finally:
            if retry:
                with self.inbox.transaction():
                    self.db.execute("UPDATE voice_note_resolution SET retry_at=? WHERE event_id=?", (time.time() + 5, event_id))
                    self.db.execute("UPDATE deliveries SET state='pending' WHERE event_id=?", (event_id,))
            else:
                self.inbox.finish(event_id, completed=completed, rejection=rejection)
                if not completed and rejection is None:
                    logger.error("Voice-note admission outcome requires review before replay")
        return retry

    async def close(self) -> None:
        """Keep unresolved originals and deadlines while stopping local waiters."""
        self.closing = True
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.inbox.close()
