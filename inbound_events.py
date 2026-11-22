"""Queue and handle ``inbound.received`` events in local delivery mode."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import tempfile
import time
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)

RETRY_DELAYS = (10, 30, 120, 600)
MAX_RUNS = len(RETRY_DELAYS) + 1
SCRIPT_TIMEOUT = 120
STDOUT_LIMIT = 64 * 1024
STDERR_TAIL = 4 * 1024
REPORT_MAX_DELAY = 3600
RETENTION = 7 * 86400
SWEEP_INTERVAL = 3600
MAX_PENDING_EVENTS = 100
MAX_PENDING_BYTES = 64 * 1024 * 1024


class InboundFull(Exception):
    """The local queue is full; the delivery should be retried later."""


def local_delivery() -> bool:
    return os.getenv("INKBOX_DELIVERY_MODE", "").strip().lower() == "local"


def request_bytes(request: dict) -> bytes:
    """The request body as received, decoded by its ``body_encoding``."""
    body = str(request.get("body") or "")
    if request.get("body_encoding") == "base64":
        with suppress(binascii.Error):
            return base64.b64decode(body, validate=True)
    return body.encode("utf-8")


def events_dir() -> Path:
    # Under the home directory so a sandboxed agent can read its event files.
    return Path.home() / ".inkbox" / "inbound" / "events"


def write_event_files(events: Path, data: dict, body: bytes) -> Path:
    """Write ``body`` and ``request.json`` to ``events/<delivery_id>``, replacing any earlier copy."""
    delivery_id = str(data.get("delivery_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", delivery_id):
        raise ValueError("invalid delivery id")
    request = data.get("request") or {}
    source = data.get("source") or {}
    meta = {
        "event_id": data.get("event_id"), "delivery_id": delivery_id, "received_at": data.get("received_at"),
        "source": {"id": source.get("id"), "name": source.get("name")},
        "signature_verified": data.get("signature_verified") is True,
        **{k: request.get(k) for k in ("method", "query", "headers", "content_type")},
        "body_encoding": request.get("body_encoding") or "utf8",
        "body_bytes": len(request_bytes(request)),
    }
    events.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=events))
    try:
        for name, content in (("body", body), ("request.json", json.dumps(meta, indent=2).encode() + b"\n")):
            fd = os.open(tmp / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        target = events / delivery_id
        shutil.rmtree(target, ignore_errors=True)
        os.rename(tmp, target)
    except BaseException:
        with suppress(Exception):
            shutil.rmtree(tmp)
        raise
    return target


def sweep_event_files(events: Path) -> None:
    """Delete event directories older than the retention period; errors are ignored."""
    cutoff = time.time() - RETENTION
    with suppress(OSError):
        for entry in events.iterdir():
            with suppress(OSError):
                if entry.lstat().st_mtime < cutoff:
                    shutil.rmtree(entry)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
        # One byte past the limit shows whether the output was truncated.
        _, out, err, _ = await asyncio.wait_for(asyncio.gather(
            _feed(proc.stdin, stdin), _drain(proc.stdout, STDOUT_LIMIT + 1),
            _drain(proc.stderr, STDERR_TAIL, tail=True), proc.wait(),
        ), SCRIPT_TIMEOUT)
    except asyncio.TimeoutError:
        _kill(proc)
        await proc.wait()
        return False, "", f"timed out after {SCRIPT_TIMEOUT} s"
    except BaseException:
        _kill(proc)
        raise
    stdout = out[:STDOUT_LIMIT].decode(errors="ignore") + ("\n(truncated)" if len(out) > STDOUT_LIMIT else "")
    return proc.returncode == 0, stdout, err.decode(errors="replace")


def _kill(proc) -> None:
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


class InboundEvents:
    """Durable queue processed by one background worker, deduplicated on ``delivery_id``.

    ``pending`` events run their handler; ``reporting`` events retry the failure
    report without running the handler again. An agent turn never runs twice.
    """

    def __init__(self, path: Path, run_agent, report_failure):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        # WAL with FULL sync makes each commit durable before the delivery is acknowledged.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS inbound_events (delivery_id TEXT PRIMARY KEY, payload TEXT,"
            " attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, error TEXT, failed_at REAL,"
            " state TEXT NOT NULL CHECK(state IN ('pending','reporting','done','failed')), received REAL NOT NULL)"
        )
        _fsync_dir(path.parent)
        self.run_agent = run_agent
        self.report_failure = report_failure
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.turn_running = False
        self.sweeper: asyncio.Task | None = None

    def start(self) -> None:
        # Pending failure reports are retried right away on every start.
        self.db.execute("UPDATE inbound_events SET due=? WHERE state='reporting'", (time.time(),))
        self.task = asyncio.create_task(self.loop())
        self.sweeper = asyncio.create_task(self.sweep())

    async def close(self) -> None:
        for task in (self.task, self.sweeper):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.task = self.sweeper = None
        self.db.close()

    def pending(self) -> int:
        """Events still to handle, including script retry waits; pending failure reports are not counted."""
        return self.db.execute(
            "SELECT count(*) FROM inbound_events WHERE state='pending'").fetchone()[0] + self.turn_running

    def accept(self, data: dict) -> bool:
        """Store a new event; False when its delivery was already received.

        Raises ``InboundFull`` when the backlog is at its cap, before anything is stored.
        """
        now = time.time()
        self.db.execute("DELETE FROM inbound_events WHERE state IN ('done','failed') AND received<?",
                        (now - RETENTION,))
        if self.db.execute("SELECT 1 FROM inbound_events WHERE delivery_id=?", (data["delivery_id"],)).fetchone():
            return False
        payload = json.dumps(data)
        count, used = self.db.execute(
            "SELECT count(*), coalesce(sum(length(CAST(payload AS BLOB))),0) FROM inbound_events"
            " WHERE state IN ('pending','reporting')").fetchone()
        if count >= MAX_PENDING_EVENTS or used + len(payload.encode()) > MAX_PENDING_BYTES:
            raise InboundFull("inbound queue is full")
        self.db.execute(
            "INSERT INTO inbound_events(delivery_id,payload,due,state,received) VALUES(?,?,?,'pending',?)",
            (data["delivery_id"], payload, now, now),
        )
        self.wake.set()
        return True

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

    async def sweep(self) -> None:
        while True:
            sweep_event_files(events_dir())
            await asyncio.sleep(SWEEP_INTERVAL)

    async def process(self, key: str, data: dict, attempts: int) -> None:
        handler = data.get("handler") or {}
        body = None
        if handler.get("type") == "script":
            if attempts >= MAX_RUNS:
                # The last run was cut off by a restart; it is not run again.
                error = self.db.execute("SELECT error FROM inbound_events WHERE delivery_id=?", (key,)).fetchone()[0]
                self.fail(key, error or "script run was interrupted by a restart")
                return
            attempts += 1
            # Counted before running so a run cut off by a restart still uses up the attempt.
            self.db.execute("UPDATE inbound_events SET attempts=? WHERE delivery_id=?", (attempts, key))
            ok, out, err = await run_script(str(handler.get("command") or ""), data)
            if not ok:
                error = (err.strip() or "script failed")[-500:]
                if attempts < MAX_RUNS:
                    self.db.execute("UPDATE inbound_events SET due=?,error=? WHERE delivery_id=?",
                                    (time.time() + RETRY_DELAYS[attempts - 1], error, key))
                    return
                self.fail(key, error)
                return
            if not out.strip():
                self.finish(key, "done")
                return
            body = out
        # Recorded as failed first so a turn interrupted by a restart is reported, not repeated.
        self.fail(key, "agent turn was interrupted by a restart")
        self.turn_running = True
        try:
            await self.run_agent(data, body)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail(key, (str(error) or f"agent turn failed ({type(error).__name__})")[:500])
            return
        finally:
            self.turn_running = False
        self.finish(key, "done")

    def fail(self, key: str, error: str) -> None:
        """Persist the failure; the report is sent and retried from the queue."""
        now = time.time()
        self.db.execute("UPDATE inbound_events SET state='reporting',error=?,attempts=0,due=?,failed_at=?"
                        " WHERE delivery_id=?", (error, now, now, key))
        self.wake.set()

    async def report(self, key: str, data: dict, attempts: int) -> None:
        error = self.db.execute("SELECT error FROM inbound_events WHERE delivery_id=?", (key,)).fetchone()[0]
        try:
            await asyncio.to_thread(self.report_failure, data, error)
        except Exception as exc:
            logger.warning("[Inkbox] Reporting inbound delivery %s failed (%s)", key, type(exc).__name__)
            self.db.execute("UPDATE inbound_events SET attempts=?,due=? WHERE delivery_id=?",
                            (attempts + 1, time.time() + min(30 * 2 ** attempts, REPORT_MAX_DELAY), key))
            return
        self.finish(key, "failed")

    def finish(self, key: str, state: str) -> None:
        self.db.execute("UPDATE inbound_events SET state=?,payload=NULL WHERE delivery_id=?", (state, key))
