"""Unit tests for the durable spawn-edge ledger (``inkbox_lineage``).

Every ledger invariant is exercised against a tmp-dir hermes-home (the host
``get_hermes_home`` helper is never called — the ``_hermes_home`` seam is
monkeypatched at tmp_path), so no gateway or host package is needed.
"""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

import inkbox_lineage as lineage


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the ledger at an isolated tmp hermes-home for each test."""
    monkeypatch.setattr(lineage, "_hermes_home", lambda: tmp_path)
    return tmp_path


def _recipient(channel="email", address="alex@example.com"):
    return {"channel": channel, "address": address}


def _spawn(parent_edge=None, psk="sess-A", recipient=None, turn="turn-1", call_index=0):
    """Build + CAS-create one edge the way a send tool would."""
    recipient = recipient or _recipient()
    rkey = lineage.recipient_key(recipient["channel"], recipient.get("address") or recipient.get("conversationId"))
    sk = lineage.spawn_key(psk, rkey, turn, call_index)

    def builder():
        edge = lineage.derive_edge(parent_edge, psk, recipient, call_index)
        edge["originTurnId"] = turn
        edge["spawnKey"] = sk
        return edge

    return lineage.create_edge_cas(sk, builder), sk


# ---------------------------------------------------------------------------
# Atomic + private writes
# ---------------------------------------------------------------------------
def test_atomic_write_is_private_and_json(home):
    path = home / "inkbox_lineage" / "edges" / "x.json"
    lineage._atomic_write(path, {"a": 1})

    assert json.loads(path.read_text()) == {"a": 1}
    # 0600: owner read/write only, no group/other bits.
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    # The temp sibling must not linger after the atomic replace.
    assert not path.with_suffix(path.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# Tolerant reads
# ---------------------------------------------------------------------------
def test_read_edge_is_tolerant(home):
    assert lineage._read_edge("missing") is None  # absent file

    edges_dir = home / "inkbox_lineage" / "edges"
    edges_dir.mkdir(parents=True)
    (edges_dir / "garbage.json").write_text("{not json")
    assert lineage._read_edge("garbage") is None  # unparseable

    (edges_dir / "arr.json").write_text("[1, 2, 3]")
    assert lineage._read_edge("arr") is None  # valid JSON but not an object

    (edges_dir / "ok.json").write_text('{"edgeId": "ok"}')
    assert lineage._read_edge("ok") == {"edgeId": "ok"}


# ---------------------------------------------------------------------------
# spawn_key determinism + non-collision
# ---------------------------------------------------------------------------
def test_spawn_key_deterministic_and_distinct():
    a = lineage.spawn_key("sess-A", "email:x@y.com", "turn-1", 0)
    b = lineage.spawn_key("sess-A", "email:x@y.com", "turn-1", 0)
    assert a == b  # deterministic
    assert len(a) == 64  # sha256 hex

    # Any changed component yields a different key.
    assert a != lineage.spawn_key("sess-B", "email:x@y.com", "turn-1", 0)
    assert a != lineage.spawn_key("sess-A", "sms:+15551234", "turn-1", 0)
    assert a != lineage.spawn_key("sess-A", "email:x@y.com", "turn-2", 0)
    assert a != lineage.spawn_key("sess-A", "email:x@y.com", "turn-1", 1)

    # No cross-field bleed: a delimiter injection can't forge another key.
    assert lineage.spawn_key("a", "b", "c", 0) != lineage.spawn_key("a\x1fb", "c", "", 0)


def test_recipient_key_normalization():
    # Email is case-folded so variants collapse; channel is lower-cased.
    assert lineage.recipient_key("Email", "Alex@Example.com") == "email:alex@example.com"
    # Phone numbers collapse to the trailing 10 digits so the format the agent
    # typed at spawn and the inbound webhook's format bind to the same edge.
    assert lineage.recipient_key("sms", " +1 (555) 123-4567 ") == "sms:5551234567"
    assert lineage.recipient_key("sms", "+15551234567") == lineage.recipient_key("sms", "5551234567")
    # Same human, two channels → two distinct keys.
    assert lineage.recipient_key("email", "a@b.com") != lineage.recipient_key("sms", "a@b.com")


# ---------------------------------------------------------------------------
# derive_edge: root + child + multi-hop rules
# ---------------------------------------------------------------------------
def test_derive_edge_root(home):
    edge = lineage.derive_edge(None, "sess-A", _recipient(), 0)

    # A root: root id is its own id, no parent, empty ancestry.
    assert edge["rootEdgeId"] == edge["edgeId"]
    assert edge["parentEdgeId"] is None
    assert edge["ancestry"] == []
    assert edge["ancestrySessionKeys"] == ["sess-A"]
    assert edge["parentSessionKey"] == "sess-A"
    assert edge["childSessionKey"] is None
    assert edge["status"] == lineage.STATUS_SPAWNING
    assert edge["recipientKey"] == "email:alex@example.com"
    assert edge["channelChild"] == "email"
    # ttlAt defaults to the bind window ahead of creation.
    assert edge["ttlAt"] == pytest.approx(edge["createdAt"] + lineage.BIND_TIMEOUT_S)


def test_derive_edge_child_and_multihop(home):
    a = lineage.derive_edge(None, "sess-A", _recipient(address="b@x.com"), 0)
    # B, acting under edge A, spawns to C.
    b = lineage.derive_edge(a, "sess-B", _recipient(address="c@x.com"), 0)

    assert b["parentEdgeId"] == a["edgeId"]
    assert b["rootEdgeId"] == a["rootEdgeId"] == a["edgeId"]
    assert b["ancestry"] == [a["edgeId"]]
    assert b["ancestrySessionKeys"] == ["sess-A", "sess-B"]
    assert b["channelParent"] == a["channelChild"]

    # C, acting under edge B, spawns to D — chain survives, root constant.
    c = lineage.derive_edge(b, "sess-C", _recipient(address="d@x.com"), 0)
    assert c["rootEdgeId"] == a["edgeId"]
    assert c["parentEdgeId"] == b["edgeId"]
    assert c["ancestry"] == [a["edgeId"], b["edgeId"]]
    assert c["ancestrySessionKeys"] == ["sess-A", "sess-B", "sess-C"]
    assert len(c["ancestry"]) == 2  # depth is derived, not stored


def test_ancestry_cycle(home):
    a = lineage.derive_edge(None, "sess-A", _recipient(address="b@x.com"), 0)
    b = lineage.derive_edge(a, "sess-B", _recipient(address="c@x.com"), 0)

    # Spawning back to A or B (already in the chain) is a cycle.
    assert lineage.ancestry_cycle("sess-A", b) is True
    assert lineage.ancestry_cycle("sess-B", b) is True
    # A fresh recipient is not.
    assert lineage.ancestry_cycle("sess-D", b) is False


# ---------------------------------------------------------------------------
# create_edge_cas idempotency
# ---------------------------------------------------------------------------
def test_create_edge_cas_idempotent(home):
    first, sk = _spawn()
    second, _ = _spawn()  # identical inputs → identical spawn key

    assert first["edgeId"] == second["edgeId"]  # reused, not re-created
    assert first["spawnKey"] == sk

    # Exactly one edge file on disk.
    edges = list((home / "inkbox_lineage" / "edges").glob("*.json"))
    assert len(edges) == 1

    # A different call index is a different spawn → a second edge.
    other, _ = _spawn(call_index=1)
    assert other["edgeId"] != first["edgeId"]
    assert len(list((home / "inkbox_lineage" / "edges").glob("*.json"))) == 2


# ---------------------------------------------------------------------------
# cas_status: transitions + double-relay guard
# ---------------------------------------------------------------------------
def test_cas_status_transitions_and_rejects_wrong_state(home):
    edge, _ = _spawn()
    eid = edge["edgeId"]

    # Wrong expected-state is rejected without mutating.
    assert lineage.cas_status(eid, lineage.STATUS_ANSWERED, lineage.STATUS_RELAYED) is False
    assert lineage._read_edge(eid)["status"] == lineage.STATUS_SPAWNING

    # Walk the happy path.
    assert lineage.cas_status(eid, lineage.STATUS_SPAWNING, lineage.STATUS_DELIVERED)
    assert lineage.cas_status(eid, lineage.STATUS_DELIVERED, lineage.STATUS_AWAITING_REPLY)
    assert lineage.cas_status(eid, lineage.STATUS_AWAITING_REPLY, lineage.STATUS_ANSWERED)

    # First relay wins; the second is rejected (exactly-once).
    relayed = lineage.cas_status(eid, lineage.STATUS_ANSWERED, lineage.STATUS_RELAYED)
    assert relayed and relayed["status"] == lineage.STATUS_RELAYED
    assert lineage.cas_status(eid, lineage.STATUS_ANSWERED, lineage.STATUS_RELAYED) is False

    # Missing edge → False.
    assert lineage.cas_status("nope", lineage.STATUS_ANSWERED, lineage.STATUS_RELAYED) is False


def test_cas_status_applies_mutate(home):
    edge, _ = _spawn()
    eid = edge["edgeId"]

    def bind(e):
        e["childSessionKey"] = "sess-child"

    updated = lineage.cas_status(eid, lineage.STATUS_SPAWNING, lineage.STATUS_DELIVERED, mutate=bind)
    assert updated["childSessionKey"] == "sess-child"
    assert lineage._read_edge(eid)["childSessionKey"] == "sess-child"
    # updatedAt advanced past createdAt-era value.
    assert updated["updatedAt"] >= updated["createdAt"]


# ---------------------------------------------------------------------------
# Index round-trips
# ---------------------------------------------------------------------------
def test_index_round_trips(home):
    a = lineage.derive_edge(None, "sess-A", _recipient(address="b@x.com"), 0)
    a["spawnKey"] = "sk-a"
    a["groupId"] = "grp-1"
    lineage._persist(a)

    b = lineage.derive_edge(a, "sess-B", _recipient(address="c@x.com"), 0)
    b["spawnKey"] = "sk-b"
    b["groupId"] = "grp-1"
    lineage._persist(b)

    # by recipient
    rk = lineage.recipient_key("email", "b@x.com")
    assert [e["edgeId"] for e in lineage.index_edges("recipient", rk)] == [a["edgeId"]]

    # by parent (b's parent is a)
    assert [e["edgeId"] for e in lineage.index_edges("parent", a["edgeId"])] == [b["edgeId"]]

    # by group (both edges share grp-1)
    grp_ids = {e["edgeId"] for e in lineage.index_edges("group", "grp-1")}
    assert grp_ids == {a["edgeId"], b["edgeId"]}

    # by child — only appears once childSessionKey is stamped via cas_status.
    assert lineage.index_edges("child", "sess-child") == []
    lineage.cas_status(a["edgeId"], lineage.STATUS_SPAWNING, lineage.STATUS_DELIVERED,
                       mutate=lambda e: e.__setitem__("childSessionKey", "sess-child"))
    assert [e["edgeId"] for e in lineage.index_edges("child", "sess-child")] == [a["edgeId"]]

    # Unknown key → empty, not error.
    assert lineage.index_edges("recipient", "email:nobody@x.com") == []


# ---------------------------------------------------------------------------
# TTL sweep
# ---------------------------------------------------------------------------
def test_sweep_ttl_abandons_only_expired_sweepable(home):
    # Expired spawning edge.
    stale, _ = _spawn(recipient=_recipient(address="stale@x.com"))
    lineage._persist({**lineage._read_edge(stale["edgeId"]), "ttlAt": 1.0})

    # Expired awaiting_reply edge.
    waiting, _ = _spawn(recipient=_recipient(address="wait@x.com"), turn="turn-w")
    lineage.cas_status(waiting["edgeId"], lineage.STATUS_SPAWNING, lineage.STATUS_DELIVERED)
    lineage.cas_status(waiting["edgeId"], lineage.STATUS_DELIVERED, lineage.STATUS_AWAITING_REPLY)
    lineage._persist({**lineage._read_edge(waiting["edgeId"]), "ttlAt": 1.0})

    # Fresh spawning edge (far-future ttl) — must survive.
    fresh, _ = _spawn(recipient=_recipient(address="fresh@x.com"), turn="turn-f")

    # Expired but terminal (relayed) — not sweepable.
    done, _ = _spawn(recipient=_recipient(address="done@x.com"), turn="turn-d")
    lineage.cas_status(done["edgeId"], lineage.STATUS_SPAWNING, lineage.STATUS_DELIVERED)
    lineage.cas_status(done["edgeId"], lineage.STATUS_DELIVERED, lineage.STATUS_AWAITING_REPLY)
    lineage.cas_status(done["edgeId"], lineage.STATUS_AWAITING_REPLY, lineage.STATUS_ANSWERED)
    lineage.cas_status(done["edgeId"], lineage.STATUS_ANSWERED, lineage.STATUS_RELAYED)
    lineage._persist({**lineage._read_edge(done["edgeId"]), "ttlAt": 1.0})

    abandoned = set(lineage.sweep_ttl(now=1000.0))

    assert abandoned == {stale["edgeId"], waiting["edgeId"]}
    assert lineage._read_edge(stale["edgeId"])["status"] == lineage.STATUS_ABANDONED
    assert lineage._read_edge(waiting["edgeId"])["status"] == lineage.STATUS_ABANDONED
    assert lineage._read_edge(fresh["edgeId"])["status"] == lineage.STATUS_SPAWNING
    assert lineage._read_edge(done["edgeId"])["status"] == lineage.STATUS_RELAYED

    # Idempotent: a second sweep reaps nothing new.
    assert lineage.sweep_ttl(now=1000.0) == []


def test_sweep_ttl_no_ledger(home):
    # No edges dir yet — sweep is a no-op, not an error.
    assert lineage.sweep_ttl() == []


# ---------------------------------------------------------------------------
# Multi-worker race: the lock proves exactly ONE edge is created.
# ---------------------------------------------------------------------------
def test_create_edge_cas_race_creates_exactly_one_edge(home):
    psk, turn, ci = "sess-A", "turn-race", 0
    recipient = _recipient(address="race@x.com")
    rkey = lineage.recipient_key(recipient["channel"], recipient["address"])
    sk = lineage.spawn_key(psk, rkey, turn, ci)

    n = 16
    barrier = threading.Barrier(n)
    results: list = []
    results_lock = threading.Lock()

    def worker():
        # Each thread builds its own candidate edge (fresh uuid); the flock in
        # create_edge_cas must let only one candidate win.
        def builder():
            edge = lineage.derive_edge(None, psk, recipient, ci)
            edge["originTurnId"] = turn
            return edge

        barrier.wait()  # maximize simultaneous contention
        edge = lineage.create_edge_cas(sk, builder)
        with results_lock:
            results.append(edge["edgeId"])

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every worker observed the same single edge id...
    assert len(results) == n
    assert len(set(results)) == 1
    # ...and exactly one edge file exists on disk.
    edge_files = list((home / "inkbox_lineage" / "edges").glob("*.json"))
    assert len(edge_files) == 1
    # ...and the recipient index has a single marker.
    winner = set(results).pop()
    assert [e["edgeId"] for e in lineage.index_edges("recipient", rkey)] == [winner]
