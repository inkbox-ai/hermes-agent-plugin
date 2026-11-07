"""Bounded durable acceptance for supervised webhook delivery."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

MAX_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 4096
RECEIPT_RETENTION = 7 * 86400


class InboxError(ValueError):
    pass


class InboxFull(RuntimeError):
    pass


class ManagedInbox:
    def __init__(self, path: Path, activation_id: str):
        self.activation_id = str(UUID(activation_id))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InboxError("Private inbox directory required")
        self.lock = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.db = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise InboxError("Private inbox file required")
            finally:
                os.close(descriptor)
            self.db = sqlite3.connect(path, timeout=3, isolation_level=None)
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS deliveries (
                    activation TEXT NOT NULL, event_id TEXT NOT NULL, receipt TEXT NOT NULL UNIQUE,
                    digest TEXT NOT NULL, payload TEXT, bytes INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','dispatching','completed','unknown')),
                    received REAL NOT NULL, finished REAL,
                    PRIMARY KEY(activation,event_id)
                );
                CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries(activation,state,received);
            ''')
            # A vanished process cannot establish whether its external effects completed.
            self.db.execute("UPDATE deliveries SET state='unknown' WHERE state='dispatching'")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def accept(self, envelope: dict) -> dict:
        metadata = envelope.get('hosted_delivery')
        if (not isinstance(metadata, dict)
                or set(metadata) != {'version', 'activation_id', 'event_id', 'starts_agent'}
                or type(metadata['version']) is not int or metadata['version'] != 1
                or type(metadata['starts_agent']) is not bool
                or metadata['activation_id'] != self.activation_id
                or not isinstance(metadata['event_id'], str)
                or not re.fullmatch(r'evt_[a-f0-9]{64}', metadata['event_id'])
                or envelope.get('id') != metadata['event_id']):
            raise InboxError('Invalid delivery identity')
        payload = json.dumps(envelope, sort_keys=True, separators=(',', ':'), allow_nan=False)
        size = len(payload.encode())
        if size > 512 * 1024:
            raise InboxError('Delivery exceeds size limit')
        # Rehydrated attachment URLs and source projections can change on retry.
        content = {'event_type': envelope.get('event_type'), 'hosted_delivery': metadata}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        with self.transaction():
            self.db.execute("DELETE FROM deliveries WHERE state='completed' AND finished<?",
                            (time.time() - RECEIPT_RETENTION,))
            row = self.db.execute('SELECT receipt,state,digest FROM deliveries WHERE activation=? AND event_id=?',
                                  (self.activation_id, metadata['event_id'])).fetchone()
            if row is not None:
                if row[2] != digest:
                    raise InboxError('Conflicting delivery content')
                return self.receipt(metadata['event_id'], row[0], row[1])
            count, used = self.db.execute('SELECT count(*),coalesce(sum(bytes),0) FROM deliveries').fetchone()
            if count >= MAX_EVENTS or used + size > MAX_BYTES:
                raise InboxFull('Delivery inbox is full')
            receipt = uuid4().hex
            self.db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?, ?,NULL)',
                            (self.activation_id, metadata['event_id'], receipt, digest, payload, size, 'pending', time.time()))
            return self.receipt(metadata['event_id'], receipt, 'pending')

    def claim(self, *, protocol_only: bool = False) -> tuple[str, dict] | None:
        with self.transaction():
            row = self.db.execute("SELECT event_id,payload FROM deliveries WHERE activation=? AND state='pending' AND json_extract(payload, '$.hosted_delivery.starts_agent')=? ORDER BY received,event_id LIMIT 1",
                                  (self.activation_id, not protocol_only)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE deliveries SET state='dispatching' WHERE activation=? AND event_id=?",
                            (self.activation_id, row[0]))
            return row[0], json.loads(row[1])

    def finish(self, event_id: str, *, completed: bool) -> bool:
        if type(completed) is not bool:
            raise InboxError('Invalid delivery outcome')
        with self.transaction():
            row = self.db.execute("UPDATE deliveries SET state=?,finished=?,payload=CASE WHEN ? THEN NULL ELSE payload END,bytes=CASE WHEN ? THEN 0 ELSE bytes END WHERE activation=? AND event_id=? AND state='dispatching'",
                                  ('completed' if completed else 'unknown', time.time(), completed, completed,
                                   self.activation_id, event_id))
            return row.rowcount == 1

    def summary(self) -> dict:
        counts = dict(self.db.execute('SELECT state,count(*) FROM deliveries WHERE activation=? GROUP BY state',
                                      (self.activation_id,)))
        return {'pending': counts.get('pending', 0), 'active': counts.get('dispatching', 0),
                'unknown': counts.get('unknown', 0)}

    def receipt(self, event_id: str, receipt: str, state: str) -> dict:
        return {'version': 1, 'activation_id': self.activation_id, 'event_id': event_id,
                'receipt_id': receipt, 'state': state if state in {'completed', 'unknown'} else 'accepted'}
