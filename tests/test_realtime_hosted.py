"""Tests for the hosted realtime voice path (config resolution + supervisor WS)."""

import asyncio
import json
import sys
import types
from pathlib import Path

from aiohttp import WSMsgType


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.realtime import MODE_HOSTED, MODE_LOCAL, RealtimeCallMeta, RealtimeConfig


def _clear_realtime_env(monkeypatch):
    for name in (
        "INKBOX_REALTIME_ENABLED",
        "INKBOX_REALTIME_API_KEY",
        "OPENAI_API_KEY",
        "INKBOX_REALTIME_MODE",
        "INKBOX_REALTIME_HOSTED",
        "INKBOX_REALTIME_VOICE",
        "INKBOX_REALTIME_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


# ── config resolution ────────────────────────────────────────────────────────


def test_default_mode_is_local(monkeypatch):
    _clear_realtime_env(monkeypatch)
    cfg = adapter_mod._resolve_realtime_config({})
    assert cfg.mode == MODE_LOCAL
    assert cfg.hosted is False


def test_hosted_enabled_without_local_credential(monkeypatch):
    _clear_realtime_env(monkeypatch)
    # Hosted hands the voice leg to the platform, so no local key is required.
    cfg = adapter_mod._resolve_realtime_config({"realtime": {"mode": "hosted"}})
    assert cfg.mode == MODE_HOSTED
    assert cfg.hosted is True
    assert cfg.enabled is True
    assert cfg.api_key == ""


def test_hosted_via_env_mode(monkeypatch):
    _clear_realtime_env(monkeypatch)
    monkeypatch.setenv("INKBOX_REALTIME_MODE", "hosted")
    cfg = adapter_mod._resolve_realtime_config({})
    assert cfg.hosted is True
    assert cfg.enabled is True


def test_hosted_via_env_boolean(monkeypatch):
    _clear_realtime_env(monkeypatch)
    monkeypatch.setenv("INKBOX_REALTIME_HOSTED", "true")
    cfg = adapter_mod._resolve_realtime_config({})
    assert cfg.mode == MODE_HOSTED
    assert cfg.enabled is True


def test_explicit_mode_wins_over_hosted_flag(monkeypatch):
    _clear_realtime_env(monkeypatch)
    cfg = adapter_mod._resolve_realtime_config(
        {"realtime": {"mode": "local", "hosted": True}}
    )
    assert cfg.mode == MODE_LOCAL


def test_hosted_can_be_disabled(monkeypatch):
    _clear_realtime_env(monkeypatch)
    monkeypatch.setenv("INKBOX_REALTIME_MODE", "hosted")
    monkeypatch.setenv("INKBOX_REALTIME_ENABLED", "false")
    cfg = adapter_mod._resolve_realtime_config({})
    assert cfg.hosted is True
    assert cfg.enabled is False


def test_unknown_mode_falls_back_to_local(monkeypatch):
    _clear_realtime_env(monkeypatch)
    cfg = adapter_mod._resolve_realtime_config({"realtime": {"mode": "bogus"}})
    assert cfg.mode == MODE_LOCAL


def test_hosted_carries_voice_and_model(monkeypatch):
    _clear_realtime_env(monkeypatch)
    cfg = adapter_mod._resolve_realtime_config(
        {"realtime": {"mode": "hosted", "voice": "river", "model": "voice-x"}}
    )
    assert cfg.voice == "river"
    assert cfg.model == "voice-x"


# ── supervisor WS fakes ──────────────────────────────────────────────────────


class _FakeWS:
    """Minimal call WS: async-iterates observe frames, captures intervene ones."""

    def __init__(self, messages=None):
        self._messages = list(messages or [])
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration

    async def send_str(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


def _text_msg(**frame):
    """Wrap a wire frame dict as a TEXT WS message."""
    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(frame))


def _channel(ws):
    return adapter_mod._HostedSupervisorChannel(ws)


def _adapter():
    a = object.__new__(InkboxAdapter)
    a._identity_handle = "acme"
    a._identity_id = "identity-123"
    a._inkbox = None
    a._contact_cache = {}
    a._realtime_config = RealtimeConfig(enabled=True, mode=MODE_HOSTED, voice="cedar", model="voice-x")
    a.config = types.SimpleNamespace(extra={})
    return a


def _call_meta(**overrides):
    fields = dict(
        call_id="call-1",
        contact_id="c-1",
        contact_name="Bob",
        remote_phone_number="+15550001111",
        direction="inbound",
        agent_identity_handle="acme",
        contact_known=True,
    )
    fields.update(overrides)
    return RealtimeCallMeta(**fields)


# ── intervene channel frames ─────────────────────────────────────────────────


def test_channel_answer_consult_frame():
    ws = _FakeWS()
    asyncio.run(_channel(ws).answer_consult("c-9", "at 3pm"))
    assert json.loads(ws.sent[0]) == {
        "event": "consult.answer", "consult_id": "c-9", "answer": "at 3pm",
    }


def test_channel_answer_consult_with_instructions():
    ws = _FakeWS()
    asyncio.run(_channel(ws).answer_consult("c-9", "at 3pm", instructions="be brief"))
    assert json.loads(ws.sent[0]) == {
        "event": "consult.answer", "consult_id": "c-9", "answer": "at 3pm",
        "instructions": "be brief",
    }


def test_channel_inject_and_update_instructions():
    ws = _FakeWS()
    asyncio.run(_channel(ws).inject("hold on", mode="context"))
    asyncio.run(_channel(ws).update_instructions("stay on topic"))
    assert json.loads(ws.sent[0]) == {
        "event": "inject", "mode": "context", "text": "hold on",
    }
    assert json.loads(ws.sent[1]) == {
        "event": "update_instructions", "instructions": "stay on topic",
    }


def test_channel_hang_up():
    ws = _FakeWS()
    asyncio.run(_channel(ws).hang_up(reason="done"))
    assert json.loads(ws.sent[0]) == {"event": "hang_up", "reason": "done"}


# ── meta from WS-resolved context ────────────────────────────────────────────


def test_call_meta_from_ws_context():
    a = _adapter()
    meta = a._hosted_call_meta_from({
        "call_id": "call-9",
        "contact_id": "c-9",
        "contact_name": "Bob",
        "remote_phone_number": "+15550001111",
        "direction": "Outbound",
        "contact": {"emails": ["bob@x.com"], "company": "Acme"},
    })
    assert meta.call_id == "call-9"
    assert meta.direction == "outbound"
    assert meta.contact_known is True
    assert meta.contact_emails == ["bob@x.com"]
    assert meta.contact_company == "Acme"


# ── consult over the supervisor WS ───────────────────────────────────────────


def test_consult_requested_answers_via_main_agent():
    a = _adapter()
    seen = {}

    async def fake_consult(meta, query, transcript, *_a, **_kw):
        seen["meta"] = meta
        seen["query"] = query
        seen["transcript"] = transcript
        return "The meeting is at 3pm."

    a._realtime_agent_consult = fake_consult
    ws = _FakeWS()
    event = {
        "event": "consult.requested",
        "consult_id": "consult-9",
        "query": "when is the meeting?",
        "transcript_tail": [{"speaker": "remote", "text": "hi"}],
    }

    asyncio.run(a._hosted_dispatch_event(_channel(ws), event, _call_meta()))

    assert json.loads(ws.sent[0]) == {
        "event": "consult.answer", "consult_id": "consult-9",
        "answer": "The meeting is at 3pm.",
    }
    assert seen["query"] == "when is the meeting?"
    assert seen["transcript"] == [("remote", "hi")]
    # The consult runs against the WS-resolved caller context.
    assert seen["meta"].contact_name == "Bob"


def test_consult_failure_pushes_graceful_answer():
    a = _adapter()

    async def boom(*_a, **_kw):
        raise RuntimeError("agent unreachable")

    a._realtime_agent_consult = boom
    ws = _FakeWS()
    event = {
        "event": "consult.requested",
        "consult_id": "consult-9",
        "query": "anything?",
        "transcript_tail": [],
    }

    asyncio.run(a._hosted_dispatch_event(_channel(ws), event, _call_meta()))

    frame = json.loads(ws.sent[0])
    assert frame["consult_id"] == "consult-9"
    assert "follow up" in frame["answer"].lower()


def test_consult_missing_fields_is_noop():
    a = _adapter()
    ws = _FakeWS()
    event = {"event": "consult.requested", "consult_id": "", "query": ""}
    asyncio.run(a._hosted_dispatch_event(_channel(ws), event, _call_meta()))
    assert ws.sent == []


# ── post-call handling ───────────────────────────────────────────────────────


def test_call_ended_with_actions_enqueues_post_call_turn():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    event = {
        "event": "call.ended",
        "reason": "completed",
        "post_call_actions": [{"action": "email bob the quote", "details": "re: pricing"}],
        "transcript": [{"speaker": "remote", "text": "thanks, bye"}],
    }

    asyncio.run(a._hosted_dispatch_event(_channel(_FakeWS()), event, _call_meta()))

    assert len(captured) == 1
    assert "email bob the quote" in captured[0].text
    assert "re: pricing" in captured[0].text


def test_call_ended_without_actions_enqueues_reflection():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    event = {
        "event": "call.ended",
        "reason": "completed",
        "post_call_actions": [],
        "transcript": [{"speaker": "remote", "text": "bye"}],
    }

    asyncio.run(a._hosted_dispatch_event(_channel(_FakeWS()), event, _call_meta()))

    assert len(captured) == 1
    assert "[call_ended]" in captured[0].text


def test_call_ended_action_dict_details_are_json_not_repr():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    event = {
        "event": "call.ended",
        "post_call_actions": [
            {"action": "look it up", "details": {"query": "order status"}},
        ],
        "transcript": [],
    }

    asyncio.run(a._hosted_dispatch_event(_channel(_FakeWS()), event, _call_meta()))

    assert len(captured) == 1
    # Dict details render as JSON, never a Python repr with single quotes.
    assert '{"query": "order status"}' in captured[0].text
    assert "{'query'" not in captured[0].text


# ── call.ended lifecycle webhook ─────────────────────────────────────────────


def _call_ended_envelope(**call_overrides):
    """Build a ``call.ended`` webhook envelope with an inline transcript."""
    call = {
        "id": "call-web-1",
        "origin": "dedicated_number",
        "remote_phone_number": "+15550002222",
        "direction": "inbound",
        "status": "completed",
        "use_inkbox_agent": True,
    }
    call.update(call_overrides)
    return {
        "event_type": "call.ended",
        "data": {
            "call": call,
            "contacts": [{"id": "c-web-1", "name": "Dana"}],
            "agent_identities": [],
            "transcript": {
                "entries": [
                    {"party": "remote", "text": "hi there", "ts_ms": 0},
                    {"marker": "abridged", "omitted_turns": 3, "omitted_ms": 9000},
                    {"party": "local", "text": "all set", "ts_ms": 12000},
                ],
                "abridged": True,
                "url": "https://example.test/api/v1/phone/calls/call-web-1/transcripts",
            },
            "transcript_url": "https://example.test/api/v1/phone/calls/call-web-1/transcripts",
        },
    }


def test_on_call_ended_webhook_reflects_with_inline_transcript():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue

    resp = asyncio.run(a._on_call_ended(_call_ended_envelope()))

    assert resp.status == 200
    assert len(captured) == 1
    body = captured[0].text
    assert "[call_ended]" in body
    # Inline transcript turns thread through; the abridged marker is dropped.
    assert "remote: hi there" in body
    assert "local: all set" in body
    assert "abridged" not in body


def test_on_call_ended_webhook_dedupes_redeliveries():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    envelope = _call_ended_envelope()

    # First delivery reflects; a webhook re-delivery for the same call_id is
    # dropped by the claim but still 200s.
    asyncio.run(a._on_call_ended(envelope))
    asyncio.run(a._on_call_ended(envelope))

    assert len(captured) == 1


def test_on_call_ended_webhook_defers_to_supervised_ws():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    envelope = _call_ended_envelope()
    # A live supervisor WS owns this call — the richer WS frame reflects it, so
    # the transcript-only webhook defers (no enqueue) but still 200s.
    a._mark_supervised_call("call-web-1")

    resp = asyncio.run(a._on_call_ended(envelope))

    assert resp.status == 200
    assert captured == []


def test_on_call_ended_webhook_reflects_after_supervisor_closed():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    envelope = _call_ended_envelope()
    # Supervisor opened then closed (e.g. abnormal end) without reflecting; the
    # webhook is now the only handover and must reflect.
    a._mark_supervised_call("call-web-1")
    a._clear_supervised_call("call-web-1")

    asyncio.run(a._on_call_ended(envelope))

    assert len(captured) == 1


def test_on_call_ended_webhook_without_inline_transcript():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    envelope = _call_ended_envelope(use_inkbox_agent=False)
    envelope["data"].pop("transcript", None)  # non-hosted: no inline transcript

    resp = asyncio.run(a._on_call_ended(envelope))

    assert resp.status == 200
    assert len(captured) == 1
    assert "[call_ended]" in captured[0].text


def test_webhook_call_transcript_skips_markers():
    turns = InkboxAdapter._webhook_call_transcript({
        "entries": [
            {"party": "remote", "text": "one"},
            {"marker": "abridged", "omitted_turns": 2},
            {"party": "local", "text": "two"},
            {"party": "remote", "text": ""},  # empty text dropped
        ],
    })
    assert turns == [("remote", "one"), ("local", "two")]


def test_webhook_call_transcript_handles_missing_block():
    assert InkboxAdapter._webhook_call_transcript(None) == []
    assert InkboxAdapter._webhook_call_transcript({}) == []


def test_call_lifecycle_webhook_url_is_distinct_from_base():
    # The call-lifecycle sub must live on its own URL so it never collides with
    # the identity's iMessage sub (one active subscription per identity+url).
    base = "https://tunnel.example.test/webhook"
    call_url = adapter_mod._call_lifecycle_webhook_url(base)
    assert call_url != base
    assert call_url.startswith(base)
    # Idempotent shape so current/previous URLs line up during reconcile.
    assert adapter_mod._call_lifecycle_webhook_url(base + "/") == call_url


# ── supervisor pump over the one call WS ─────────────────────────────────────


def test_run_hosted_supervisor_drains_frames_and_answers_consult():
    a = _adapter()

    async def fake_consult(meta, query, transcript, *_a, **_kw):
        return "answer"

    a._realtime_agent_consult = fake_consult
    ws = _FakeWS([
        _text_msg(event="call.started", call_id="call-1", direction="inbound"),
        _text_msg(event="transcript", party="remote", text="hi", is_final=True),
        _text_msg(
            event="consult.requested", consult_id="c-1", query="q",
            transcript_tail=[],
        ),
    ])

    meta = {
        "call_id": "call-1",
        "contact_id": "c-1",
        "contact_name": "Bob",
        "remote_phone_number": "+15550001111",
        "direction": "inbound",
    }
    asyncio.run(a._run_hosted_supervisor(ws, meta))

    # The consult answer rode back down the same call WS.
    assert json.loads(ws.sent[0]) == {
        "event": "consult.answer", "consult_id": "c-1", "answer": "answer",
    }


def test_run_hosted_supervisor_ignores_non_text_frames():
    a = _adapter()
    ws = _FakeWS([
        types.SimpleNamespace(type=WSMsgType.BINARY, data=b"\x00"),
        types.SimpleNamespace(type=WSMsgType.TEXT, data="not-json"),
    ])
    # Neither a binary frame nor malformed JSON should raise out of the pump.
    asyncio.run(a._run_hosted_supervisor(ws, {"call_id": "call-1"}))
    assert ws.sent == []
