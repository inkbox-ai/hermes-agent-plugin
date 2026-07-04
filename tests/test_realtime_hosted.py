"""Tests for the hosted realtime voice path (config resolution + control channel)."""

import asyncio
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.realtime import MODE_HOSTED, MODE_LOCAL, RealtimeConfig


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


# ── control-channel fakes ────────────────────────────────────────────────────


class _FakeControlSession:
    def __init__(self, events=None):
        self._events = list(events or [])
        self.answers = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        raise StopAsyncIteration

    async def answer_consult(self, consult_id, answer, instructions=None):
        self.answers.append((consult_id, answer, instructions))

    async def close(self):
        self.closed = True


class _FakeHostedRealtime:
    def __init__(self):
        self.config_calls = []

    def set_config(self, **kwargs):
        self.config_calls.append(kwargs)


class _FakeRealtime:
    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.connects = []

    async def connect(self, **kwargs):
        self.connects.append(kwargs)
        if self._sessions:
            return self._sessions.pop(0)
        # No further sessions — surface as "no control channel" to end the loop.
        raise AttributeError("no more sessions")


class _FakePhone:
    def __init__(self, sessions):
        self.hosted_realtime = _FakeHostedRealtime()
        self.realtime = _FakeRealtime(sessions)


class _FakeInkbox:
    def __init__(self, sessions=None):
        self.phone = _FakePhone(sessions or [])


def _event(kind, **fields):
    return types.SimpleNamespace(event=kind, **fields)


def _adapter():
    a = object.__new__(InkboxAdapter)
    a._identity_handle = "acme"
    a._identity_id = "identity-123"
    a._inkbox = None
    a._realtime_control_task = None
    a._realtime_control_session = None
    a._hosted_call_meta = {}
    a._contact_cache = {}
    a._realtime_config = RealtimeConfig(enabled=True, mode=MODE_HOSTED, voice="cedar", model="voice-x")
    a.config = types.SimpleNamespace(extra={})
    return a


# ── control-channel consult answer ───────────────────────────────────────────


def test_consult_requested_answers_via_main_agent():
    a = _adapter()
    seen = {}

    async def fake_consult(meta, query, transcript, *_a, **_kw):
        seen["meta"] = meta
        seen["query"] = query
        seen["transcript"] = transcript
        return "The meeting is at 3pm."

    a._realtime_agent_consult = fake_consult
    session = _FakeControlSession()
    event = _event(
        "consult.requested",
        call_id="call-1",
        consult_id="consult-9",
        query="when is the meeting?",
        transcript_tail=[{"speaker": "remote", "text": "hi"}],
    )

    asyncio.run(a._hosted_dispatch_event(session, event))

    assert session.answers == [("consult-9", "The meeting is at 3pm.", None)]
    assert seen["query"] == "when is the meeting?"
    assert seen["transcript"] == [("remote", "hi")]


def test_consult_failure_pushes_graceful_answer():
    a = _adapter()

    async def boom(*_a, **_kw):
        raise RuntimeError("agent unreachable")

    a._realtime_agent_consult = boom
    session = _FakeControlSession()
    event = _event(
        "consult.requested",
        call_id="call-1",
        consult_id="consult-9",
        query="anything?",
        transcript_tail=[],
    )

    asyncio.run(a._hosted_dispatch_event(session, event))

    assert len(session.answers) == 1
    consult_id, answer, _instr = session.answers[0]
    assert consult_id == "consult-9"
    assert "follow up" in answer.lower()


def test_consult_uses_cached_call_meta():
    a = _adapter()
    a._hosted_call_meta["call-1"] = types.SimpleNamespace(
        call_id="call-1", contact_name="Bob", remote_phone_number="+15550001111",
        direction="inbound", contact_known=True,
    )
    captured = {}

    async def fake_consult(meta, query, transcript, *_a, **_kw):
        captured["meta"] = meta
        return "ok"

    a._realtime_agent_consult = fake_consult
    session = _FakeControlSession()
    event = _event(
        "consult.requested", call_id="call-1", consult_id="c-1", query="q",
        transcript_tail=[],
    )

    asyncio.run(a._hosted_dispatch_event(session, event))

    assert captured["meta"].contact_name == "Bob"


# ── post-call handling ───────────────────────────────────────────────────────


def test_call_ended_with_actions_enqueues_post_call_turn():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    event = _event(
        "call.ended",
        call_id="call-1",
        reason="completed",
        post_call_actions=[{"action": "email bob the quote", "details": "re: pricing"}],
        transcript=[{"speaker": "remote", "text": "thanks, bye"}],
    )

    asyncio.run(a._hosted_dispatch_event(None, event))

    assert len(captured) == 1
    assert "email bob the quote" in captured[0].text
    assert "re: pricing" in captured[0].text


def test_call_ended_without_actions_enqueues_reflection():
    a = _adapter()
    captured = []

    async def fake_enqueue(event):
        captured.append(event)

    a._enqueue = fake_enqueue
    event = _event(
        "call.ended",
        call_id="call-1",
        reason="completed",
        post_call_actions=[],
        transcript=[{"speaker": "remote", "text": "bye"}],
    )

    asyncio.run(a._hosted_dispatch_event(None, event))

    assert len(captured) == 1
    assert "[call_ended]" in captured[0].text


def test_call_ended_pops_cached_meta():
    a = _adapter()
    a._hosted_call_meta["call-1"] = types.SimpleNamespace(
        call_id="call-1", contact_id="c", contact_name="Bob",
        remote_phone_number=None, direction="inbound",
    )

    async def fake_enqueue(event):
        pass

    a._enqueue = fake_enqueue
    event = _event(
        "call.ended", call_id="call-1", reason="done",
        post_call_actions=[], transcript=[],
    )

    asyncio.run(a._hosted_dispatch_event(None, event))

    assert "call-1" not in a._hosted_call_meta


# ── start + subscription loop ────────────────────────────────────────────────


def test_start_hosted_realtime_enables_config_and_subscribes():
    a = _adapter()

    async def fake_consult(meta, query, transcript, *_a, **_kw):
        return "answer"

    a._realtime_agent_consult = fake_consult
    session = _FakeControlSession([
        _event(
            "consult.requested", call_id="call-1", consult_id="c-1",
            query="q", transcript_tail=[],
        ),
    ])
    a._inkbox = _FakeInkbox([session])

    async def scenario():
        await a._start_hosted_realtime()
        # The subscription task connects, drains the one session, then exits the
        # reconnect loop when the fake client reports no further sessions.
        await a._realtime_control_task

    asyncio.run(scenario())

    hosted = a._inkbox.phone.hosted_realtime
    assert hosted.config_calls[0]["enabled"] is True
    assert hosted.config_calls[0]["agent_identity_id"] == "identity-123"
    assert a._inkbox.phone.realtime.connects[0]["agent_identity_id"] == "identity-123"
    assert session.answers == [("c-1", "answer", None)]


def test_start_hosted_realtime_without_config_surface_is_safe():
    a = _adapter()

    class _NoSurface:
        phone = types.SimpleNamespace()

    a._inkbox = _NoSurface()
    # Missing hosted_realtime attribute must not raise out of start.
    asyncio.run(a._start_hosted_realtime())
    assert a._realtime_control_task is None
