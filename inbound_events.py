"""Queue and handle ``inbound.received`` events in local delivery mode."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import time
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)

RETRY_DELAYS = (10, 30, 120, 600)
SCRIPT_TIMEOUT = 120
RETENTION = 7 * 86400


def dedupe_key(data: dict) -> str:
    # delivery_id is stable across retries and new on each replay.
    return str(data.get("delivery_id") or data.get("event_id") or "")


def local_delivery() -> bool:
    return os.getenv("INKBOX_DELIVERY_MODE", "").strip().lower() == "local"


async def run_script(command: str, data: dict) -> tuple[bool, str, str]:
    """Run a script handler with the event as JSON on stdin."""
    request = data.get("request") or {}
    stdin = json.dumps({
        "source": (data.get("source") or {}).get("name"),
        "event_id": data.get("event_id"),
        "headers": request.get("headers") or {},
        "body": request.get("body") or "",
        "body_encoding": request.get("body_encoding") or "utf8",
    }).encode()
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", command, cwd=Path.home(), start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), SCRIPT_TIMEOUT)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return False, "", f"timed out after {SCRIPT_TIMEOUT} s"
    return proc.returncode == 0, out.decode(errors="replace"), err.decode(errors="replace")


class InboundEvents:
    """Durable, deduplicated queue processed by one background worker."""

    def __init__(self, path: Path, run_agent, report_failure):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (delivery_key TEXT PRIMARY KEY, payload TEXT,"
            " attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL,"
            " state TEXT NOT NULL CHECK(state IN ('pending','done','failed')), received REAL NOT NULL)"
        )
        self.run_agent = run_agent
        self.report_failure = report_failure
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.loop())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.db.close()

    def accept(self, data: dict) -> bool:
        """Store a new event; False when its delivery was already received."""
        now = time.time()
        self.db.execute("DELETE FROM events WHERE state!='pending' AND received<?", (now - RETENTION,))
        added = self.db.execute(
            "INSERT OR IGNORE INTO events(delivery_key,payload,due,state,received) VALUES(?,?,?,'pending',?)",
            (dedupe_key(data), json.dumps(data), now, now),
        ).rowcount
        if added:
            self.wake.set()
        return bool(added)

    async def loop(self) -> None:
        while True:
            self.wake.clear()
            row = self.db.execute(
                "SELECT delivery_key,payload,attempts,due FROM events WHERE state='pending' ORDER BY due LIMIT 1"
            ).fetchone()
            wait = None if row is None else row[3] - time.time()
            if wait is None or wait > 0:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), wait)
                continue
            try:
                await self.process(row[0], json.loads(row[1]), row[2])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Inkbox] Inbound delivery %s could not be handled", row[0])
                self.finish(row[0], "failed")

    async def process(self, key: str, data: dict, attempts: int) -> None:
        handler = data.get("handler") or {}
        body = None
        if handler.get("type") == "script":
            ok, out, err = await run_script(str(handler.get("command") or ""), data)
            if not ok:
                attempts += 1
                if attempts <= len(RETRY_DELAYS):
                    self.db.execute("UPDATE events SET attempts=?,due=? WHERE delivery_key=?",
                                    (attempts, time.time() + RETRY_DELAYS[attempts - 1], key))
                    return
                self.finish(key, "failed")
                await asyncio.to_thread(self.report_failure, data, (err.strip() or "script failed")[-500:])
                return
            if not out.strip():
                self.finish(key, "done")
                return
            body = out
        await self.run_agent(data, body)
        self.finish(key, "done")

    def finish(self, key: str, state: str) -> None:
        self.db.execute("UPDATE events SET state=?,payload=NULL WHERE delivery_key=?", (state, key))
