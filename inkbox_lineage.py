"""Durable spawn-edge ledger for spin-off thread lineage.

A *spawn edge* is a per-edge record describing one time the agent, mid-thread
with a parent principal, started a fresh conversation with a recipient to
gather info or delegate. Each edge is its own JSON file under
``<hermes-home>/inkbox_lineage/`` so concurrent writers never lose an update,
generalizing the single-file capsule pattern used for outbound call contexts.

The file on disk is the source of truth; the side-index directories are an
advisory lookup aid rebuilt from the edge files. Every read-modify-write is
serialized with an OS-level advisory lock (``fcntl.flock``) so creation-dedup
and status transitions hold across processes, not just within one event loop.

This module is host-independent: it only touches the hermes-home path (looked
up lazily through :func:`_hermes_home`, the same helper the call-context
capsule uses) and the local filesystem, so it is fully unit-testable.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

# Status lifecycle for an edge. Only ``spawning`` and ``awaiting_reply`` are
# swept to ``abandoned`` on TTL expiry; the terminal states are left alone.
STATUS_SPAWNING = "spawning"
STATUS_DELIVERED = "delivered"
STATUS_AWAITING_REPLY = "awaiting_reply"
STATUS_ANSWERED = "answered"
STATUS_RELAYED = "relayed"
STATUS_STALE_HELD = "stale_held"
STATUS_FAILED = "failed"
STATUS_ABANDONED = "abandoned"

# States a TTL sweep may reap, mapped to the timeout that governs each.
_SWEEPABLE_STATUSES = (STATUS_SPAWNING, STATUS_AWAITING_REPLY)

# Default timeouts (seconds): a short window for a child that was sent to but
# never engaged, and a longer window for one that engaged but never replied.
BIND_TIMEOUT_S = 30 * 60
AWAITING_REPLY_TIMEOUT_S = 48 * 60 * 60

# Field separator for hashing composite keys — a control char that cannot
# appear in a session key, address, or turn id, so the join is unambiguous.
_KEY_SEP = "\x1f"


# ----------------------------------------------------------------------------
# Paths — everything lives under a single hermes-home subdir, mirroring the
# ``inkbox_call_contexts`` layout so the two capsules sit side by side.
# ----------------------------------------------------------------------------
def _hermes_home() -> Path:
    """Return the hermes-home root as a Path.

    Wraps the host's ``get_hermes_home`` in a lazily-imported, monkeypatchable
    seam so unit tests can redirect the whole ledger at a tmp dir without the
    host package installed.

    Returns:
        Path: the hermes-home directory.
    """
    from hermes_cli.config import get_hermes_home

    return Path(get_hermes_home())


def _root() -> Path:
    """Return the ledger root ``<hermes-home>/inkbox_lineage``.

    Returns:
        Path: the ledger root directory (not guaranteed to exist yet).
    """
    return _hermes_home() / "inkbox_lineage"


def _edges_dir() -> Path:
    return _root() / "edges"


def _locks_dir() -> Path:
    return _root() / "locks"


def _index_dir(kind: str, key: str) -> Path:
    """Return the side-index bucket directory for one key.

    The key (a recipient key, session key, edge id, or group id) is hashed so
    the directory name is always filesystem-safe regardless of the ``:``/``@``/
    ``+`` characters real keys contain.

    Args:
        kind (str): index family — ``parent``, ``child``, ``recipient``, or
            ``group``.
        key (str): the raw key to bucket by.

    Returns:
        Path: the bucket directory (not guaranteed to exist yet).
    """
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return _root() / f"by_{kind}" / digest


# ----------------------------------------------------------------------------
# Atomic write — reuses the ONLY atomic-write idiom in the repo (tmp file →
# os.replace), adding a 0600 chmod that idiom lacks so edge records stay
# private on shared hosts.
# ----------------------------------------------------------------------------
def _atomic_write(path: Path, obj: Any) -> None:
    """Write ``obj`` as pretty JSON to ``path`` atomically and 0600.

    A concurrent reader can never observe a half-written file: the payload is
    written to a sibling ``.tmp`` file, made private, then renamed into place.

    Args:
        path (Path): destination file path.
        obj (Any): a JSON-serializable object.

    Returns:
        None
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sibling temp file (matches the identity-state idiom: suffix + ".tmp").
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp_path, 0o600)  # lock down before it becomes visible
    os.replace(tmp_path, path)  # atomic rename


def _read_edge(edge_id: str) -> Optional[Dict[str, Any]]:
    """Read one edge record from disk, tolerantly.

    Args:
        edge_id (str): the edge's id.

    Returns:
        Optional[Dict[str, Any]]: the edge dict, or ``None`` if the file is
        absent, unreadable, malformed, or not a JSON object.
    """
    path = _edges_dir() / f"{edge_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None  # tolerant: a torn/garbage file reads as "no edge"
    if not isinstance(data, dict):
        return None
    return data


# ----------------------------------------------------------------------------
# Key derivation
# ----------------------------------------------------------------------------
def recipient_key(channel: str, address_or_conv: str) -> str:
    """Normalize a child-side destination into a stable key.

    The same human reached on two channels yields two different keys — a
    spin-off is per-channel by design.

    Args:
        channel (str): the child channel (e.g. ``email``, ``sms``,
            ``imessage``).
        address_or_conv (str): the address or conversation id on that channel.

    Returns:
        str: ``"<channel>:<address_or_conv>"`` normalized (email addresses are
        lower-cased so case variants collapse).
    """
    chan = (channel or "").strip().lower()
    addr = (address_or_conv or "").strip()
    if chan == "email":
        addr = addr.lower()
    return f"{chan}:{addr}"


def spawn_key(
    parent_session_key: str,
    recipient_key_value: str,
    origin_turn_id: str,
    call_index: int,
) -> str:
    """Compute the idempotency key for a spawn.

    Two redelivered sends of the same parent turn to the same recipient at the
    same within-turn index hash to the same key, so creation can dedup them.

    Args:
        parent_session_key (str): session that is spawning the edge.
        recipient_key_value (str): normalized recipient key (see
            :func:`recipient_key`).
        origin_turn_id (str): the parent turn/context id that triggered it.
        call_index (int): within-turn spawn sequence number.

    Returns:
        str: a sha256 hex digest.
    """
    parts = [
        str(parent_session_key or ""),
        str(recipient_key_value or ""),
        str(origin_turn_id or ""),
        str(int(call_index)),
    ]
    return hashlib.sha256(_KEY_SEP.join(parts).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Edge derivation — the "roots need no special-casing" rule. A root spawn is
# just the case where the acting session had no parent edge.
# ----------------------------------------------------------------------------
def derive_edge(
    parent_edge: Optional[Dict[str, Any]],
    parent_session_key: str,
    recipient: Dict[str, Any],
    call_index: int,
) -> Dict[str, Any]:
    """Build a fresh edge skeleton from its lineage context.

    Populates the identity/ancestry/recipient fields deterministically; the
    caller fills the spawn-specific fields (``brief``, ``parentRoute``,
    ``originTurnId``, ``spawnKey``) before persisting. When ``parent_edge`` is
    ``None`` this is a family root: ``rootEdgeId`` is the new edge's own id and
    ``ancestry`` is empty — no branch needed beyond the ``??`` fallbacks.

    Args:
        parent_edge (Optional[Dict[str, Any]]): the edge the acting session was
            itself operating under, or ``None`` for a root spawn.
        parent_session_key (str): the acting (spawning) session's key.
        recipient (Dict[str, Any]): ``{"channel": str, "address"?: str,
            "conversationId"?: str}`` describing the child destination.
        call_index (int): within-turn spawn sequence number.

    Returns:
        Dict[str, Any]: a full edge dict with every schema key present.
    """
    now = time.time()
    edge_id = str(uuid.uuid4())

    # Lineage: ancestry is the parent's chain plus the parent edge itself;
    # a root spawn falls through to the empty/self-referential defaults.
    root_edge_id = parent_edge["rootEdgeId"] if parent_edge else edge_id
    parent_edge_id = parent_edge["edgeId"] if parent_edge else None
    if parent_edge:
        ancestry = list(parent_edge.get("ancestry") or []) + [parent_edge["edgeId"]]
        ancestry_sks = list(parent_edge.get("ancestrySessionKeys") or []) + [parent_session_key]
    else:
        ancestry = []
        ancestry_sks = [parent_session_key]

    channel_child = (recipient.get("channel") or "").strip().lower()
    address = recipient.get("address")
    conversation_id = recipient.get("conversationId")
    rkey = recipient_key(channel_child, address or conversation_id or "")

    return {
        "edgeId": edge_id,
        "rootEdgeId": root_edge_id,
        "parentEdgeId": parent_edge_id,
        "ancestry": ancestry,
        "ancestrySessionKeys": ancestry_sks,
        "parentSessionKey": parent_session_key,
        "childSessionKey": None,
        # Routing/identity of the parent — filled in by the caller at spawn.
        "parentRoute": {},
        "parentContact": {},
        "parentReplyTarget": {},
        "channelParent": (parent_edge or {}).get("channelChild"),
        "channelChild": channel_child,
        "groupId": None,
        "brief": {
            "intent": "",
            "endState": "",
            "constraints": [],
            "facts": [],
            "disclose_identity": False,
        },
        "recipientBinding": {
            "channel": channel_child,
            "address": address,
            "conversationId": conversation_id,
            "outboundMessageId": None,
            "bindWindowUntil": None,
        },
        "result": None,
        "originTurnId": None,
        "callIndex": int(call_index),
        "spawnKey": None,
        "recipientKey": rkey,
        "status": STATUS_SPAWNING,
        "createdAt": now,
        "updatedAt": now,
        # Bind deadline: reaped by sweep_ttl if the child never engages.
        "ttlAt": now + BIND_TIMEOUT_S,
    }


def ancestry_cycle(recipient_session_key: str, edge: Dict[str, Any]) -> bool:
    """Report whether spawning to ``recipient_session_key`` would loop.

    A cycle occurs when the recipient's session is already somewhere in the
    edge's ancestry (or is the acting session itself) — i.e. A→B→A.

    Args:
        recipient_session_key (str): the child recipient's session key.
        edge (Dict[str, Any]): the acting parent edge whose ancestry to check.

    Returns:
        bool: ``True`` if the spawn would revisit an ancestor session.
    """
    seen = set(edge.get("ancestrySessionKeys") or [])
    parent_sk = edge.get("parentSessionKey")
    if parent_sk is not None:
        seen.add(parent_sk)
    return recipient_session_key in seen


# ----------------------------------------------------------------------------
# Locking — OS-level advisory locks so dedup and CAS hold across processes.
# ----------------------------------------------------------------------------
@contextmanager
def _lock(name: str) -> Iterator[None]:
    """Hold an exclusive advisory lock named ``name`` for the block.

    Backed by ``fcntl.flock(LOCK_EX)`` on a lockfile under ``locks/``; the
    lockfile is never unlinked so there is no unlink/create race. flock is
    exclusive between distinct open file descriptions, so it serializes
    concurrent threads and concurrent processes alike.

    Args:
        name (str): a filesystem-safe lock name (a hex digest or uuid).

    Yields:
        None
    """
    locks_dir = _locks_dir()
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until we own the lock
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ----------------------------------------------------------------------------
# Persistence + indexing
# ----------------------------------------------------------------------------
def _index_edge(edge: Dict[str, Any]) -> None:
    """(Re)create every side-index marker for ``edge``.

    Markers are empty files named by ``edgeId`` inside the per-key bucket dir,
    so indexing is append-only and idempotent — re-indexing after a status
    mutation (e.g. once ``childSessionKey`` appears) just adds the newly-known
    markers and never rewrites a shared list.

    Args:
        edge (Dict[str, Any]): the edge to index.

    Returns:
        None
    """
    edge_id = edge["edgeId"]

    def _touch(kind: str, key: Optional[str]) -> None:
        if key is None:
            return
        bucket = _index_dir(kind, str(key))
        bucket.mkdir(parents=True, exist_ok=True)
        (bucket / edge_id).touch()

    _touch("recipient", edge.get("recipientKey"))
    _touch("parent", edge.get("parentEdgeId"))
    _touch("child", edge.get("childSessionKey"))
    _touch("group", edge.get("groupId"))


def _persist(edge: Dict[str, Any]) -> Dict[str, Any]:
    """Write an edge file and (re)index it.

    Args:
        edge (Dict[str, Any]): the edge to persist.

    Returns:
        Dict[str, Any]: the same edge dict, for convenience.
    """
    _atomic_write(_edges_dir() / f"{edge['edgeId']}.json", edge)
    _index_edge(edge)
    return edge


def index_edges(by: str, key: str) -> List[Dict[str, Any]]:
    """Return all edges filed under one index key.

    Args:
        by (str): index family — ``parent``, ``child``, ``recipient``, or
            ``group``.
        key (str): the raw key to look up.

    Returns:
        List[Dict[str, Any]]: the matching edge dicts (missing/torn markers are
        skipped tolerantly); empty if the bucket does not exist.
    """
    bucket = _index_dir(by, key)
    if not bucket.exists():
        return []
    edges: List[Dict[str, Any]] = []
    for marker in bucket.iterdir():
        edge = _read_edge(marker.name)
        if edge is not None:
            edges.append(edge)
    return edges


def create_edge_cas(spawn_key_value: str, builder: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Create an edge for ``spawn_key_value`` exactly once.

    Under the per-spawn-key lock, look for an already-persisted edge carrying
    this spawn key (scanning the recipient index of the candidate the builder
    produces); reuse it if present, otherwise write and index the candidate.
    Two concurrent identical sends therefore serialize and the second reuses
    the first's edge.

    Args:
        spawn_key_value (str): the spawn's idempotency key (see
            :func:`spawn_key`).
        builder (Callable[[], Dict[str, Any]]): produces a fully-built
            candidate edge (only persisted if no dup exists).

    Returns:
        Dict[str, Any]: the winning edge — freshly created or the pre-existing
        duplicate.
    """
    with _lock(spawn_key_value):
        candidate = builder()
        # Pin the spawn key so lookup and record agree even if the builder
        # forgot to stamp it.
        candidate["spawnKey"] = spawn_key_value
        # Dedup: any existing edge to this recipient with the same spawn key
        # wins (identical redelivered send).
        for existing in index_edges("recipient", candidate["recipientKey"]):
            if existing.get("spawnKey") == spawn_key_value:
                return existing
        return _persist(candidate)


def cas_status(
    edge_id: str,
    expected: str,
    new: str,
    mutate: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Any:
    """Compare-and-swap an edge's status, once-only.

    Under the per-edge lock, the edge is re-read from disk (never a cached
    copy); the swap proceeds only if its current status equals ``expected``.
    This makes transitions such as ``answered → relayed`` genuinely once-only,
    so a double-relay attempt is rejected.

    Args:
        edge_id (str): the edge to transition.
        expected (str): the status the edge must currently hold.
        new (str): the status to set.
        mutate (Optional[Callable[[Dict[str, Any]], None]]): optional in-place
            edit applied to the edge before it is written (e.g. to stamp
            ``childSessionKey`` or ``result``).

    Returns:
        Any: the updated edge dict on success; ``False`` if the edge is missing
        or its status did not match ``expected``.
    """
    with _lock(edge_id):
        edge = _read_edge(edge_id)
        if edge is None:
            return False
        if edge.get("status") != expected:
            return False  # lost the race / wrong state — reject
        if mutate is not None:
            mutate(edge)
        edge["status"] = new
        edge["updatedAt"] = time.time()
        return _persist(edge)


def sweep_ttl(now: Optional[float] = None) -> List[str]:
    """Opportunistically abandon edges whose TTL has elapsed.

    Only ``spawning`` (sent but never engaged) and ``awaiting_reply`` (engaged
    but never replied) edges are eligible; each is CAS'd to ``abandoned`` so a
    concurrent legitimate transition still wins the race.

    Args:
        now (Optional[float]): the reference epoch time; defaults to
            ``time.time()`` (injectable for tests).

    Returns:
        List[str]: the edge ids that were abandoned this sweep.
    """
    ref = time.time() if now is None else now
    edges_dir = _edges_dir()
    if not edges_dir.exists():
        return []

    abandoned: List[str] = []
    for path in edges_dir.glob("*.json"):
        edge = _read_edge(path.stem)
        if edge is None:
            continue
        status = edge.get("status")
        ttl_at = edge.get("ttlAt")
        if status not in _SWEEPABLE_STATUSES or ttl_at is None:
            continue
        if ref < ttl_at:
            continue  # not yet expired
        # CAS from the observed status so a real transition mid-sweep wins.
        if cas_status(edge["edgeId"], status, STATUS_ABANDONED):
            abandoned.append(edge["edgeId"])
    return abandoned
