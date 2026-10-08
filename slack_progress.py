"""One source-bound, coalesced progress message per admitted Slack turn."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from uuid import UUID

logger = logging.getLogger(__name__)
PREFIX = "inkbox-slack-progress:"
_ROUTE = ("connection_id", "conversation_id", "thread_ts", "message_ts", "source_event_id")
_TERMINAL = {"completed": "Completed.", "cancelled": "Stopped.", "failed": "Could not complete."}


class SlackProgress:
    def __init__(self, resource, path: Path, *, identity_id=None, interval=1.0):
        self.resource, self.path, self.identity_id = resource, path, identity_id
        self.interval = interval
        self.active: dict[str, dict] = {}
        self.records: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.closing = False

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
            json.dump(self.records, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.path)

    @staticmethod
    def _key(chat_id, meta):
        return hashlib.sha256(json.dumps([str(chat_id), *[meta.get(k) for k in _ROUTE]]).encode()).hexdigest()

    def has_chat(self, chat_id):
        return any(record["chat_id"] == str(chat_id) for record in self.active.values())

    async def notify(self, chat_id, meta, state):
        if self.closing:
            return
        if not all(isinstance(meta.get(k), str) and meta[k]
                   for k in ("connection_id", "conversation_id", "message_ts", "source_event_id")):
            return
        key = self._key(chat_id, meta)
        if state == "accepted":
            # An unresolved message from an earlier process is never replaced.
            if key not in self.records:
                self.active.setdefault(key, {"chat_id": str(chat_id), "route": {
                    k: meta.get(k) for k in (*_ROUTE, "workspace_id")}, "revision": 0})
            return
        record = self.active.get(key)
        if record is None:
            return
        if state in _TERMINAL:
            self.active.pop(key, None)
            if key in self.records:
                record["terminal"] = True
                self._queue(key, record, _TERMINAL[state])
        elif key in self.records and state in {"waiting", "resumed"}:
            self._queue(key, record, "Waiting for your approval." if state == "waiting" else "Working…")

    async def progress(self, chat_id, meta, content, message_id=None):
        if self.closing:
            return None
        key = str(message_id)[len(PREFIX):] if str(message_id).startswith(PREFIX) else self._key(chat_id, meta)
        record = self.active.get(key)
        if record is None or record["chat_id"] != str(chat_id):
            return None
        # Handles may locate an edit, but cannot override a supplied source route.
        if any(k in meta and meta[k] != record["route"].get(k) for k in _ROUTE):
            return None
        text = " ".join(content.split())[:240]
        if not text:
            return PREFIX + key
        # Progress is plain text, never an opportunity to trigger Slack mentions.
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.records[key] = record
        self._queue(key, record, text)
        return PREFIX + key

    def _queue(self, key, record, text):
        record["desired"] = text
        if key not in self.tasks or self.tasks[key].done():
            task = asyncio.create_task(self._run(key, record))
            self.tasks[key] = task
            task.add_done_callback(lambda done: self.tasks.pop(key, None) if self.tasks.get(key) is done else None)

    async def _validate(self, record):
        if self.identity_id is not None:
            from .slack import validate_connection
            await asyncio.to_thread(validate_connection, self.resource, self.identity_id, record["route"])

    async def _run(self, key, record):
        try:
            while record.get("applied") != record["desired"]:
                if record.get("uncertain"):
                    return
                if record.get("terminal") and not record.get("started"):
                    self.records.pop(key, None)
                    self._save()
                    return
                delay = record.get("retry_at", 0) - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._validate(record)
                text = record["desired"]
                record["started"] = True
                record["revision"] += 1
                record["uncertain"] = True
                record.pop("operation_id", None)
                self._save()  # Persist intent before any external side effect.
                route = record["route"]
                if not record.get("message_ts"):
                    result = await asyncio.to_thread(
                        self.resource.send_message, route["connection_id"],
                        conversation_id=route["conversation_id"], thread_ts=route.get("thread_ts"),
                        text=text, idempotency_key=f"hermes:progress:{key}",
                    )
                    if (getattr(result, "status", None) != "sent"
                            or not isinstance(getattr(result, "message_ts", None), str) or not result.message_ts):
                        logger.warning("Slack progress creation is unconfirmed; no automatic resend")
                        return
                    record["message_ts"] = result.message_ts
                else:
                    result = await asyncio.to_thread(
                        self.resource.update_message, route["connection_id"], route["conversation_id"],
                        record["message_ts"], text, idempotency_key=f"hermes:progress:{key}:{record['revision']}",
                    )
                    if getattr(result, "status", None) == "failed":
                        # Definitive rejection is not an unknown side effect.
                        # A later status (especially Stop/completion) may try a
                        # new edit, without spinning on the rejected payload.
                        record.pop("uncertain", None)
                        retry_after = getattr(result, "retry_after", None)
                        if isinstance(retry_after, int) and retry_after > 0:
                            record["retry_at"] = time.time() + retry_after
                        self._save()
                        if record["desired"] != text:
                            continue
                        return
                    if getattr(result, "status", None) != "succeeded":
                        operation_id = getattr(result, "id", None)
                        if isinstance(operation_id, (str, UUID)):
                            record["operation_id"] = str(operation_id)
                        self._save()
                        logger.warning("Slack progress update is unconfirmed; further edits are deferred")
                        return
                record.pop("uncertain", None)
                record["applied"] = text
                if record.get("terminal") and text == record["desired"]:
                    self.records.pop(key, None)
                self._save()
                if not record.get("terminal"):
                    await asyncio.sleep(self.interval)
        except Exception:
            logger.warning("Slack progress unavailable; the agent turn is unaffected")

    async def recover(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("Slack progress state could not be read")
            return
        if not isinstance(data, dict):
            return
        for key, record in data.items():
            if (not isinstance(record, dict) or not isinstance(record.get("route"), dict)
                    or not isinstance(record.get("chat_id"), str)
                    or key != self._key(record["chat_id"], record["route"])
                    or not isinstance(record.get("revision"), int)):
                continue
            self.records[key] = record
            try:
                await self._validate(record)
                if not record.get("message_ts"):
                    result = await asyncio.to_thread(self.resource.get_action_by_key,
                        record["route"]["connection_id"], f"hermes:progress:{key}")
                    if result.status != "sent" or not isinstance(result.message_ts, str) or not result.message_ts:
                        continue
                    record["message_ts"] = result.message_ts
                elif record.get("uncertain"):
                    if not record.get("operation_id"):
                        continue
                    result = await asyncio.to_thread(self.resource.get_operation,
                        record["route"]["connection_id"], record["operation_id"])
                    if result.status not in {"succeeded", "failed"}:
                        continue
                record.pop("uncertain", None)
                record["terminal"] = True
                self._queue(key, record, "Stopped after reconnecting.")
            except Exception:
                logger.warning("Slack progress cleanup remains unconfirmed; no message was resent")

    async def flush(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
            await asyncio.sleep(0)

    async def close(self):
        for record in list(self.active.values()):
            await self.notify(record["chat_id"], record["route"], "cancelled")
        self.closing = True
        try:
            await asyncio.wait_for(asyncio.shield(self.flush()), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Slack progress cleanup is deferred until reconnect")
