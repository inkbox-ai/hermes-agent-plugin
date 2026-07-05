"""Relay-answer coverage — the child-side primitive that wakes the parent thread.

``inkbox_relay_answer`` is what closes the loop: the follow-up conversation that
was spawned to gather an answer distills it and hands it back to the originating
thread. This file proves the tool's contract end-to-end at the tool boundary,
and proves the genuine relay enqueue that the tool schedules:

* only the child conversation that OWNS an edge (its ``childSessionKey``) may
  relay — a caller with a different session is rejected before any state change;
* an over-cap distilled summary is refused (no silent truncation, no CAS);
* ``satisfied=false`` leaves the edge ``awaiting_reply`` so the brief keeps
  riding future turns until a real answer lands;
* the ``awaiting_reply → answered`` claim is exactly-once: two concurrent relay
  calls for one edge produce exactly ONE enqueue, and the loser no-ops;
* the enqueued parent-facing :class:`MessageEvent` is ``internal=True`` with its
  source rebuilt from the edge's stored ``parentRoute`` and its text carrying the
  attribution (who answered) plus the distilled summary.

Every assertion is deterministic and runs fully offline: the ledger's
``_hermes_home`` seam is redirected at ``tmp_path``, the per-turn caller identity
is stubbed on ``tools._current_session_thread_id``, and the in-process adapter is
a capturing fake. The one test that exercises the real ``relay_edge`` primitive
drives a bare ``InkboxAdapter`` with a monkeypatched ``_enqueue``, the same stub
pattern the sibling adapter tests use — no gateway host required.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
from pathlib import Path

import pytest

import inkbox_lineage as lineage
import tools

# The adapter (and its package-relative ledger) are imported the same way the
# sibling bind tests do, so the real relay_edge primitive runs against a ledger
# object that agrees with its own _hermes_home monkeypatch.
ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod  # noqa: E402
from inkbox_plugin.adapter import InkboxAdapter  # noqa: E402


# Fixed session identities shared across cases: A is the parent thread, the
# child is the follow-up conversation the edge was bound onto at inbound.
PARENT_SK = "email:thread-A"
CHILD_SK = "email:thread-B"


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------
class FakeAdapter:
    """Stand-in in-process adapter that records the relays the tool schedules."""

    def __init__(self):
        self.scheduled = []

    def schedule_relay(self, edge_id):
        # list.append is atomic under the GIL, so the concurrency test can count
        # enqueues without its own lock.
        self.scheduled.append(edge_id)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the tool-side ledger at an isolated tmp hermes-home.

    Args:
        tmp_path (Path): pytest's per-test tmp dir.
        monkeypatch (pytest.MonkeyPatch): patching handle.

    Returns:
        Path: the tmp hermes-home the ledger writes under.
    """
    # `tools.lineage` is the same top-level module object imported here, so this
    # one patch redirects both the tool and the assertions at tmp_path.
    monkeypatch.setattr(lineage, "_hermes_home", lambda: tmp_path)
    return tmp_path


def _as_child(monkeypatch, session=CHILD_SK):
    """Make the relay caller present as ``session`` (the edge's child owner)."""
    # Host-independent: patch the helper directly rather than the env it reads,
    # so the auth check resolves identically offline and against the real host.
    # Auth keys on the session chat id (HERMES_SESSION_CHAT_ID == childSessionKey).
    monkeypatch.setattr(tools, "_current_session_chat_id", lambda: session)


def _seed_awaiting_edge(*, child=CHILD_SK, address="alex@example.com", intent="ask Alex for the Q3 figure"):
    """Persist one edge bound to a child and awaiting that child's reply.

    Mirrors the on-disk shape bind-on-inbound leaves behind: status
    ``awaiting_reply`` with ``childSessionKey`` stamped and the parent's route
    captured, ready for the child to relay an answer.

    Args:
        child (str): the child session key that owns the edge.
        address (str): the recipient address the spin-off reached.
        intent (str): the brief intent line.

    Returns:
        Dict[str, Any]: the persisted edge dict.
    """
    edge = lineage.derive_edge(None, PARENT_SK, {"channel": "email", "address": address}, 0)
    edge["originTurnId"] = "turn-A-1"
    edge["spawnKey"] = lineage.spawn_key(PARENT_SK, edge["recipientKey"], "turn-A-1", 0)
    edge["status"] = lineage.STATUS_AWAITING_REPLY
    edge["childSessionKey"] = child
    edge["parentRoute"] = {
        "chatId": "chat-A",
        "threadId": "email:thread-A",
        "modality": "email",
        "messageId": "turn-A-1",
    }
    edge["brief"]["intent"] = intent
    edge["brief"]["endState"] = "have the number to relay back"
    edge["recipientBinding"]["outboundMessageId"] = "<out-1@inkboxmail.com>"
    lineage._persist(edge)
    return edge


def _status(edge):
    """Re-read an edge's current status from disk."""
    fresh = lineage._read_edge(edge["edgeId"])
    return fresh.get("status") if fresh else None


# ---------------------------------------------------------------------------
# Caller authorization — only the owning child may relay
# ---------------------------------------------------------------------------
def test_relay_from_wrong_conversation_is_rejected_without_state_change(home, monkeypatch):
    edge = _seed_awaiting_edge()
    # A DIFFERENT conversation tries to relay this child's edge.
    _as_child(monkeypatch, session="email:thread-STRANGER")
    fake = FakeAdapter()
    monkeypatch.setattr(tools, "_active_adapter", lambda: fake)

    result = json.loads(
        tools.inkbox_relay_answer({"edge_id": edge["edgeId"], "summary": "4.2M", "satisfied": True})
    )

    # Rejected as unauthorized, and NOTHING moved: no CAS, no result, no relay.
    assert "error" in result
    assert "authorized" in result["error"].lower()
    assert _status(edge) == lineage.STATUS_AWAITING_REPLY
    assert lineage._read_edge(edge["edgeId"])["result"] is None
    assert fake.scheduled == []


# ---------------------------------------------------------------------------
# Distillation cap — an over-long summary is refused, not truncated
# ---------------------------------------------------------------------------
def test_over_cap_summary_is_rejected_without_state_change(home, monkeypatch):
    edge = _seed_awaiting_edge()
    _as_child(monkeypatch)
    fake = FakeAdapter()
    monkeypatch.setattr(tools, "_active_adapter", lambda: fake)

    # One character past the hard cap.
    oversized = "x" * (tools.RELAY_SUMMARY_MAX_CHARS + 1)
    result = json.loads(
        tools.inkbox_relay_answer({"edge_id": edge["edgeId"], "summary": oversized, "satisfied": True})
    )

    assert result.get("error_code") == "relay_summary_too_long"
    # The edge is untouched — still awaiting a (condensed) real answer.
    assert _status(edge) == lineage.STATUS_AWAITING_REPLY
    assert lineage._read_edge(edge["edgeId"])["result"] is None
    assert fake.scheduled == []


# ---------------------------------------------------------------------------
# Not-yet-satisfied — the edge stays open, no relay
# ---------------------------------------------------------------------------
def test_not_satisfied_keeps_edge_awaiting_reply(home, monkeypatch):
    edge = _seed_awaiting_edge()
    _as_child(monkeypatch)
    fake = FakeAdapter()
    monkeypatch.setattr(tools, "_active_adapter", lambda: fake)

    result = json.loads(
        tools.inkbox_relay_answer({"edge_id": edge["edgeId"], "satisfied": False})
    )

    # Acknowledged but not relayed; the brief keeps riding future turns.
    assert result["ok"] is True
    assert result["relayed"] is False
    assert result["status"] == lineage.STATUS_AWAITING_REPLY
    assert _status(edge) == lineage.STATUS_AWAITING_REPLY
    assert lineage._read_edge(edge["edgeId"])["result"] is None
    assert fake.scheduled == []


# ---------------------------------------------------------------------------
# Exactly-once — two concurrent relays for one edge → one enqueue
# ---------------------------------------------------------------------------
def test_concurrent_relays_enqueue_exactly_once(home, monkeypatch):
    edge = _seed_awaiting_edge()
    _as_child(monkeypatch)
    fake = FakeAdapter()
    monkeypatch.setattr(tools, "_active_adapter", lambda: fake)

    # Two threads race the same relay; the fcntl-backed CAS must let exactly one
    # win awaiting_reply→answered and schedule the single enqueue.
    barrier = threading.Barrier(2)
    results = []

    def _relay():
        barrier.wait()  # release both threads into the CAS at once
        results.append(
            json.loads(
                tools.inkbox_relay_answer(
                    {"edge_id": edge["edgeId"], "summary": "the answer is 42", "satisfied": True}
                )
            )
        )

    threads = [threading.Thread(target=_relay) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one winner (relayed) and one loser (no-op), and ONE enqueue total.
    winners = [r for r in results if r.get("relayed") is True]
    losers = [r for r in results if r.get("ok") is False]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    assert "not awaiting a reply" in losers[0]["error"].lower()
    assert losers[0]["status"] != lineage.STATUS_AWAITING_REPLY
    assert fake.scheduled == [edge["edgeId"]]
    assert _status(edge) == lineage.STATUS_RELAYED


# ---------------------------------------------------------------------------
# The genuine relay enqueue — internal wake with a source rebuilt from
# parentRoute and text carrying attribution + the distilled summary.
# ---------------------------------------------------------------------------
@pytest.fixture
def adapter_ledger(tmp_path, monkeypatch):
    """Redirect the adapter-side ledger at a tmp hermes-home.

    Args:
        tmp_path (Path): pytest's per-test tmp dir.
        monkeypatch (pytest.MonkeyPatch): patching handle.

    Returns:
        Path: the tmp hermes-home the adapter's relay_edge writes under.
    """
    # The adapter binds against its package-relative ledger object; patch THAT
    # one so seeds and relay_edge share a single ledger under tmp_path.
    monkeypatch.setattr(adapter_mod.lineage, "_hermes_home", lambda: tmp_path)
    return tmp_path


def _seed_answered_edge(store):
    """Persist an edge in ``answered`` with a distilled result, ready to relay."""
    edge = store.derive_edge(None, PARENT_SK, {"channel": "email", "address": "alex@example.com"}, 0)
    edge["status"] = store.STATUS_ANSWERED
    edge["childSessionKey"] = CHILD_SK
    edge["parentRoute"] = {
        "chatId": "chat-A",
        "threadId": "email:thread-A",
        "modality": "email",
        "messageId": "turn-A-1",
    }
    edge["brief"]["intent"] = "ask Alex for the Q3 revenue figure"
    edge["recipientBinding"]["address"] = "alex@example.com"
    edge["result"] = {
        "summary": "Q3 revenue was 4.2M",
        "fields": [],
        "status": "answered",
        "attribution": "Relayed answer from your spin-off to a…@example.com",
    }
    store._persist(edge)
    return edge


def _relay_adapter(events):
    """A bare adapter wired with only what relay_edge touches (capturing enqueue)."""
    adapter = object.__new__(InkboxAdapter)
    adapter._last_inbound_modality = {}

    async def _enqueue(event):
        events.append(event)

    adapter._enqueue = _enqueue
    return adapter


def test_relay_edge_enqueues_internal_wake_from_parent_route(adapter_ledger):
    store = adapter_mod.lineage
    edge = _seed_answered_edge(store)

    events = []
    adapter = _relay_adapter(events)
    asyncio.run(adapter.relay_edge(edge["edgeId"]))

    # Exactly-once claim consumed: the edge is now relayed.
    assert store._read_edge(edge["edgeId"])["status"] == store.STATUS_RELAYED

    # One internal wake enqueued back to the parent thread.
    assert len(events) == 1
    event = events[0]
    assert event.internal is True
    assert event.message_type == adapter_mod.MessageType.TEXT
    assert event.message_id == f"relay:{edge['edgeId']}"

    # Source rebuilt from the stored parentRoute (chat_id / thread_id), so the
    # wake lands on A's conversation without any host session stamp.
    assert event.source.chat_id == "chat-A"
    assert event.source.thread_id == "email:thread-A"
    # Modality was promoted onto the routing map so send() picks A's channel.
    assert adapter._last_inbound_modality.get("chat-A") == "email"

    # Text carries the attribution (who answered) plus the distilled summary.
    assert "alex@example.com" in event.text
    assert "Q3 revenue was 4.2M" in event.text
    assert "ask Alex for the Q3 revenue figure" in event.text


def test_relay_edge_is_exactly_once_across_two_calls(adapter_ledger):
    # A second relay of the same edge (e.g. fast path + durable drain) must find
    # it already out of `answered` and enqueue nothing.
    store = adapter_mod.lineage
    edge = _seed_answered_edge(store)

    events = []
    adapter = _relay_adapter(events)
    asyncio.run(adapter.relay_edge(edge["edgeId"]))
    asyncio.run(adapter.relay_edge(edge["edgeId"]))

    assert len(events) == 1  # the answered→relayed CAS gated the duplicate
    assert store._read_edge(edge["edgeId"])["status"] == store.STATUS_RELAYED


def test_relay_authorized_tolerates_identity_shapes():
    """Caller-auth matches the same identity across host/bind id shapes.

    The host session-thread id and the bind-stamped childSessionKey can carry
    the same identity differently (a channel prefix, or a differently formatted
    phone number), so authorization matches on the core identity — while a truly
    unrelated caller is still rejected.
    """
    # Bare id stamped at bind; host prefixes the same id with the channel.
    edge = {"childSessionKey": "contact-abc", "recipientKey": "sms:5551234567"}
    assert tools._relay_authorized("contact-abc", edge)          # exact
    assert tools._relay_authorized("sms:contact-abc", edge)      # prefix-stripped
    # Phone-keyed session: the recipient number is the one replying.
    assert tools._relay_authorized("sms:+1 (555) 123-4567", edge)
    # Unrelated conversation is refused.
    assert not tools._relay_authorized("sms:contact-xyz", edge)
    assert not tools._relay_authorized("", edge)
