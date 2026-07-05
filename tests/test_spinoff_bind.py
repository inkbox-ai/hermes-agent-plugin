"""Bind-on-inbound coverage for the spin-off lineage layer.

When a child principal replies to an outbound the agent spawned, the matching
``delivered`` spawn edge must flip to ``awaiting_reply`` and the follow-up turn
must carry the ``[Spawned thread]`` brief in its ``channel_prompt`` — across all
three text channels (email In-Reply-To, iMessage conversation id, SMS
from-number candidate set). Just as important: a self/echo inbound must NEVER
bind, and an inbound that matches nothing must leave today's flow byte-for-byte
unchanged (no edge mutation, no brief).

Edges are seeded straight into a tmp-dir ledger via ``adapter_mod.lineage`` (the
exact module object the bind hooks call, so the ``_hermes_home`` monkeypatch and
the seeds always agree), and the three inbound handlers are driven on a bare
``object.__new__(InkboxAdapter)`` with the same stubs the sibling adapter tests
use — no gateway host required.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import InkboxAdapter

# The ledger module the adapter actually binds against (package-relative import),
# not a second top-level copy — so seeds and monkeypatches hit one object.
lineage = adapter_mod.lineage


# ---------------------------------------------------------------------------
# Fixtures + seeding helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Redirect the ledger at a tmp hermes-home and stub the web response.

    Args:
        tmp_path (Path): pytest's per-test tmp dir.
        monkeypatch (pytest.MonkeyPatch): patching handle.

    Returns:
        Path: the tmp hermes-home root the ledger writes under.
    """
    monkeypatch.setattr(lineage, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    return tmp_path


def _seed_edge(
    *,
    channel,
    address=None,
    conversation_id=None,
    outbound_id=None,
    bind_until=None,
    status=None,
    intent="ask Alex for the Q3 revenue figure",
    end_state="have the number to relay back",
    psk="sess-A",
    turn="turn-1",
    call_index=0,
):
    """Persist one open spawn edge the way a send tool would have.

    Args:
        channel (str): child channel — ``email``/``sms``/``imessage``.
        address (Optional[str]): recipient address/number, if bound by address.
        conversation_id (Optional[str]): recipient conversation id (iMessage).
        outbound_id (Optional[str]): the outbound message id A's send produced.
        bind_until (Optional[float]): bind-window deadline (epoch seconds).
        status (Optional[str]): starting status (defaults to ``delivered``).
        intent (str): brief intent line.
        end_state (str): brief done-when line.
        psk (str): parent session key.
        turn (str): origin turn id.
        call_index (int): within-turn spawn index.

    Returns:
        Dict[str, Any]: the persisted edge dict.
    """
    recipient = {"channel": channel}
    if address:
        recipient["address"] = address
    if conversation_id:
        recipient["conversationId"] = conversation_id

    edge = lineage.derive_edge(None, psk, recipient, call_index)
    edge["originTurnId"] = turn
    edge["spawnKey"] = lineage.spawn_key(psk, edge["recipientKey"], turn, call_index)
    edge["status"] = status or lineage.STATUS_DELIVERED
    edge["brief"]["intent"] = intent
    edge["brief"]["endState"] = end_state
    edge["recipientBinding"]["outboundMessageId"] = outbound_id
    edge["recipientBinding"]["bindWindowUntil"] = bind_until
    lineage._persist(edge)
    return edge


def _status(edge):
    """Re-read an edge's current status from disk."""
    fresh = lineage._read_edge(edge["edgeId"])
    return fresh.get("status") if fresh else None


# ---------------------------------------------------------------------------
# Adapter builders — bare instances wired with the minimal stubs each handler
# touches, mirroring the sibling channel tests.
# ---------------------------------------------------------------------------
def _email_adapter(events):
    adapter = object.__new__(InkboxAdapter)
    adapter._identity_handle = "agent"
    adapter._identity_id = None
    adapter._identity_email_addresses = {"agent@inkboxmail.com"}
    adapter._identity_email_addresses_loaded = True
    adapter._inkbox = None
    adapter._last_inbound_email = {}
    adapter._last_inbound_modality = {}
    adapter._resolve_channel_overrides = lambda *_a, **_k: (None, None)

    async def _resolve_contact_full(**_kwargs):
        return None

    async def _enqueue(event):
        events.append(event)

    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue
    return adapter


def _text_adapter(events, *, monkeypatch, imessage=False):
    """Shared SMS/iMessage adapter stub (both enqueue via the text batcher)."""
    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)

    class _FakeInkbox:
        def get_identity(self, _handle):
            # Direct (non-group) resolution: no matching conversation summary.
            return types.SimpleNamespace(list_text_conversations=lambda **_k: [])

    adapter = object.__new__(InkboxAdapter)
    adapter._inkbox = _FakeInkbox()
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._resolve_channel_overrides = lambda *_a, **_k: (None, None)
    adapter._start_imessage_typing = lambda *_a, **_k: None

    async def _resolve_contact_full(**_kwargs):
        return None

    async def _enqueue_sms_text_event(event):
        events.append(event)

    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue_sms_text_event = _enqueue_sms_text_event
    if imessage:
        adapter._last_inbound_imessage = {}
    else:
        adapter._last_inbound_sms = {}
    return adapter


def _mail_envelope(from_address, *, in_reply_to=None):
    message = {
        "id": "mail-in-1",
        "mailbox_id": "mailbox-1",
        "thread_id": "thread-1",
        "message_id": "<mail-in-1@example.com>",
        "from_address": from_address,
        "to_addresses": ["agent@inkboxmail.com"],
        "subject": "Re: Q3 numbers",
        "snippet": "The Q3 figure is 4.2M.",
        "direction": "inbound",
        "status": "received",
        "created_at": "2026-05-21T00:00:00Z",
    }
    if in_reply_to is not None:
        message["in_reply_to"] = in_reply_to
    return {
        "event_type": "message.received",
        "timestamp": "2026-05-21T00:00:00Z",
        "data": {"message": message, "contacts": [], "agent_identities": []},
    }


def _sms_envelope(remote, *, direction="inbound", conversation_id="conv-spawn"):
    return {
        "event_type": "text.received",
        "data": {
            "text_message": {
                "id": "txt-in-1",
                "direction": direction,
                "remote_phone_number": remote,
                "local_phone_number": "+15555550100",
                "conversation_id": conversation_id,
                "text": "The Q3 figure is 4.2M.",
            },
        },
    }


def _imessage_envelope(remote, *, direction="inbound", conversation_id="imconv-spawn"):
    return {
        "event_type": "imessage.received",
        "data": {
            "message": {
                "id": "im-in-1",
                "direction": direction,
                "remote_number": remote,
                "conversation_id": conversation_id,
                "content": "The Q3 figure is 4.2M.",
            },
        },
    }


# ---------------------------------------------------------------------------
# EMAIL — In-Reply-To header match
# ---------------------------------------------------------------------------
def test_email_inbound_binds_edge_by_in_reply_to(ledger):
    # Bind window already expired, so ONLY the In-Reply-To header can match —
    # proving the header path (not the fallback window) did the binding.
    edge = _seed_edge(
        channel="email",
        address="alex@spawn.test",
        outbound_id="<out-spawn-1@inkboxmail.com>",
        bind_until=1.0,
    )

    events = []
    adapter = _email_adapter(events)

    response = asyncio.run(
        adapter._on_mail_received(
            _mail_envelope("alex@spawn.test", in_reply_to="<out-spawn-1@inkboxmail.com>")
        )
    )

    assert response.status == 200
    # Edge advanced under CAS and remembers the child session it landed on.
    assert _status(edge) == lineage.STATUS_AWAITING_REPLY
    bound = lineage._read_edge(edge["edgeId"])
    assert bound["childSessionKey"] == "email:thread-1"
    # The follow-up turn was briefed.
    assert len(events) == 1
    prompt = events[0].channel_prompt
    assert prompt is not None
    assert "[Spawned thread]" in prompt
    assert edge["edgeId"] in prompt
    assert "ask Alex for the Q3 revenue figure" in prompt


def test_email_inbound_no_match_leaves_edge_and_flow_untouched(ledger):
    # Edge is to Alex; the reply is from a stranger with no reply header.
    edge = _seed_edge(channel="email", address="alex@spawn.test")

    events = []
    adapter = _email_adapter(events)

    response = asyncio.run(adapter._on_mail_received(_mail_envelope("stranger@elsewhere.test")))

    assert response.status == 200
    # Untouched: still delivered, no child stamped.
    assert _status(edge) == lineage.STATUS_DELIVERED
    assert lineage._read_edge(edge["edgeId"])["childSessionKey"] is None
    # Today's flow is preserved: the turn is enqueued with no injected brief.
    assert len(events) == 1
    assert events[0].channel_prompt is None


def test_email_self_echo_does_not_bind(ledger):
    # An inbound from the agent's own mailbox never wakes the agent, so even a
    # matching edge to that address must not bind.
    edge = _seed_edge(channel="email", address="agent@inkboxmail.com")

    events = []
    adapter = _email_adapter(events)

    response = asyncio.run(
        adapter._on_mail_received(
            _mail_envelope("agent@inkboxmail.com", in_reply_to="<whatever@inkboxmail.com>")
        )
    )

    assert response.status == 200
    assert events == []  # self-mail guard fired before any bind
    assert _status(edge) == lineage.STATUS_DELIVERED


# ---------------------------------------------------------------------------
# iMESSAGE — conversation-id match
# ---------------------------------------------------------------------------
def test_imessage_inbound_binds_edge_by_conversation_id(ledger, monkeypatch):
    edge = _seed_edge(
        channel="imessage",
        conversation_id="imconv-spawn",
        intent="confirm the vendor pickup window",
    )

    events = []
    adapter = _text_adapter(events, monkeypatch=monkeypatch, imessage=True)

    response = asyncio.run(
        adapter._on_imessage_received(_imessage_envelope("+15555550188", conversation_id="imconv-spawn"))
    )

    assert response.status == 200
    assert _status(edge) == lineage.STATUS_AWAITING_REPLY
    assert lineage._read_edge(edge["edgeId"])["childSessionKey"] is not None
    assert len(events) == 1
    prompt = events[0].channel_prompt
    assert prompt is not None
    assert "[Spawned thread]" in prompt
    assert edge["edgeId"] in prompt
    assert "confirm the vendor pickup window" in prompt


def test_imessage_inbound_no_match_leaves_edge_and_flow_untouched(ledger, monkeypatch):
    # Edge is on a different conversation; this inbound must not touch it.
    edge = _seed_edge(channel="imessage", conversation_id="imconv-other")

    events = []
    adapter = _text_adapter(events, monkeypatch=monkeypatch, imessage=True)

    response = asyncio.run(
        adapter._on_imessage_received(_imessage_envelope("+15555550188", conversation_id="imconv-spawn"))
    )

    assert response.status == 200
    assert _status(edge) == lineage.STATUS_DELIVERED
    assert len(events) == 1
    assert events[0].channel_prompt is None


def test_imessage_self_echo_does_not_bind(ledger, monkeypatch):
    # Outbound-direction echoes must be dropped before the bind hook runs.
    edge = _seed_edge(channel="imessage", conversation_id="imconv-spawn")

    events = []
    adapter = _text_adapter(events, monkeypatch=monkeypatch, imessage=True)

    response = asyncio.run(
        adapter._on_imessage_received(
            _imessage_envelope("+15555550188", direction="outbound", conversation_id="imconv-spawn")
        )
    )

    assert response.status == 200
    assert events == []
    assert _status(edge) == lineage.STATUS_DELIVERED


# ---------------------------------------------------------------------------
# SMS — from-number candidate set (two open edges to one number both surface)
# ---------------------------------------------------------------------------
def test_sms_inbound_binds_full_candidate_set(ledger, monkeypatch):
    # Two open edges to the SAME number (SMS has no threading headers, so a
    # reply cannot self-identify) — both must surface and both must advance.
    edge_a = _seed_edge(
        channel="sms",
        address="+15555550188",
        intent="ask for the Q3 revenue figure",
        call_index=0,
        turn="turn-a",
    )
    edge_b = _seed_edge(
        channel="sms",
        address="+15555550188",
        intent="ask when the report ships",
        call_index=1,
        turn="turn-b",
    )

    events = []
    adapter = _text_adapter(events, monkeypatch=monkeypatch, imessage=False)

    response = asyncio.run(adapter._on_text_received(_sms_envelope("+15555550188")))

    assert response.status == 200
    # Both edges advanced under their own CAS.
    assert _status(edge_a) == lineage.STATUS_AWAITING_REPLY
    assert _status(edge_b) == lineage.STATUS_AWAITING_REPLY
    # Both briefs are surfaced to the follow-up turn.
    assert len(events) == 1
    prompt = events[0].channel_prompt
    assert prompt is not None
    assert "[Spawned thread]" in prompt
    assert edge_a["edgeId"] in prompt
    assert edge_b["edgeId"] in prompt
    assert "ask for the Q3 revenue figure" in prompt
    assert "ask when the report ships" in prompt


def test_sms_inbound_no_match_leaves_edge_and_flow_untouched(ledger, monkeypatch):
    # Edge is to a different number; a reply from someone else must not bind.
    edge = _seed_edge(channel="sms", address="+15555550111")

    events = []
    adapter = _text_adapter(events, monkeypatch=monkeypatch, imessage=False)

    response = asyncio.run(adapter._on_text_received(_sms_envelope("+15555550188")))

    assert response.status == 200
    assert _status(edge) == lineage.STATUS_DELIVERED
    assert len(events) == 1
    assert events[0].channel_prompt is None


def test_sms_self_echo_does_not_bind(ledger, monkeypatch):
    # The direction guard drops the agent's own outbound echo before binding.
    edge = _seed_edge(channel="sms", address="+15555550188")

    events = []
    adapter = _text_adapter(events, monkeypatch=monkeypatch, imessage=False)

    response = asyncio.run(
        adapter._on_text_received(_sms_envelope("+15555550188", direction="outbound"))
    )

    assert response.status == 200
    assert events == []
    assert _status(edge) == lineage.STATUS_DELIVERED
