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
STDOUT_LIMIT = 64 * 1024
STDERR_TAIL = 4 * 1024
REPORT_WINDOW = 86400
RETENTION = 7 * 86400


def local_delivery() -> bool:
    return os.getenv("INKBOX_DELIVERY_MODE", "").strip().lower() == "local"


async def _drain(stream, limit: int, *, tail: bool = False) -> bytes:
    kept = bytearray()
    while chunk := await stream.read(65536):
        kept += chunk
        if len(kept) > limit:
            if tail:
                del kept[:-limit]
            else:
                del kept[limit:]
    return bytes(kept)


async def _feed(stdin, data: bytes) -> None:
    with suppress(BrokenPipeError, ConnectionResetError):
        stdin.write(data)
        await stdin.drain()
    stdin.close()


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
        # One byte past the limit lets the agent prompt mark the output as truncated.
        _, out, err, _ = await asyncio.wait_for(asyncio.gather(
            _feed(proc.stdin, stdin), _drain(proc.stdout, STDOUT_LIMIT + 1),
            _drain(proc.stderr, STDERR_TAIL, tail=True), proc.wait(),
        ), SCRIPT_TIMEOUT)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return False, "", f"timed out after {SCRIPT_TIMEOUT} s"
    return proc.returncode == 0, out.decode(errors="replace"), err.decode(errors="replace")


class InboundEvents:
    """Durable queue processed by one background worker, deduplicated on ``delivery_id``.

    ``pending`` events run their handler; ``reporting`` events retry the failure
    report without running the handler again.
    """

    def __init__(self, path: Path, run_agent, report_failure):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS inbound_events (delivery_id TEXT PRIMARY KEY, payload TEXT,"
            " attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, error TEXT, failed_at REAL,"
            " state TEXT NOT NULL CHECK(state IN ('pending','reporting','done','failed')), received REAL NOT NULL)"
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

    def pending(self) -> int:
        """Unfinished events, including those waiting out a retry delay."""
        return self.db.execute(
            "SELECT count(*) FROM inbound_events WHERE state IN ('pending','reporting')").fetchone()[0]

    def accept(self, data: dict) -> bool:
        """Store a new event; False when its delivery was already received."""
        now = time.time()
        self.db.execute("DELETE FROM inbound_events WHERE state IN ('done','failed') AND received<?",
                        (now - RETENTION,))
        added = self.db.execute(
            "INSERT OR IGNORE INTO inbound_events(delivery_id,payload,due,state,received)"
            " VALUES(?,?,?,'pending',?)",
            (data["delivery_id"], json.dumps(data), now, now),
        ).rowcount
        if added:
            self.wake.set()
        return bool(added)

    async def loop(self) -> None:
        while True:
            self.wake.clear()
            row = self.db.execute(
                "SELECT delivery_id,payload,attempts,due,state FROM inbound_events"
                " WHERE state IN ('pending','reporting') ORDER BY due LIMIT 1"
            ).fetchone()
            wait = None if row is None else row[3] - time.time()
            if wait is None or wait > 0:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), wait)
                continue
            key, data, attempts = row[0], json.loads(row[1]), row[2]
            try:
                if row[4] == "reporting":
                    await self.report(key, data, attempts)
                else:
                    await self.process(key, data, attempts)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("[Inkbox] Inbound delivery %s could not be handled", key)
                if row[4] == "pending":
                    self.fail(key, f"handler error ({type(error).__name__})")
                else:
                    self.finish(key, "failed")

    async def process(self, key: str, data: dict, attempts: int) -> None:
        handler = data.get("handler") or {}
        body = None
        if handler.get("type") == "script":
            ok, out, err = await run_script(str(handler.get("command") or ""), data)
            if not ok:
                attempts += 1
                if attempts <= len(RETRY_DELAYS):
                    self.db.execute("UPDATE inbound_events SET attempts=?,due=? WHERE delivery_id=?",
                                    (attempts, time.time() + RETRY_DELAYS[attempts - 1], key))
                    return
                self.fail(key, (err.strip() or "script failed")[-500:])
                return
            if not out.strip():
                self.finish(key, "done")
                return
            body = out
        try:
            await self.run_agent(data, body)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail(key, (str(error) or f"agent turn failed ({type(error).__name__})")[:500])
            return
        self.finish(key, "done")

    def fail(self, key: str, error: str) -> None:
        """Persist the failure; the report is sent and retried from the queue."""
        now = time.time()
        self.db.execute("UPDATE inbound_events SET state='reporting',error=?,attempts=0,due=?,failed_at=?"
                        " WHERE delivery_id=?", (error, now, now, key))
        self.wake.set()

    async def report(self, key: str, data: dict, attempts: int) -> None:
        error, failed_at = self.db.execute(
            "SELECT error,failed_at FROM inbound_events WHERE delivery_id=?", (key,)).fetchone()
        try:
            await asyncio.to_thread(self.report_failure, data, error)
        except Exception as exc:
            if time.time() - failed_at >= REPORT_WINDOW:
                logger.warning("[Inkbox] Gave up reporting inbound delivery %s (%s)", key, type(exc).__name__)
                self.finish(key, "failed")
                return
            self.db.execute("UPDATE inbound_events SET attempts=?,due=? WHERE delivery_id=?",
                            (attempts + 1, time.time() + min(30 * 2 ** attempts, 3600), key))
            return
        self.finish(key, "failed")

    def finish(self, key: str, state: str) -> None:
        self.db.execute("UPDATE inbound_events SET state=?,payload=NULL WHERE delivery_id=?", (state, key))
