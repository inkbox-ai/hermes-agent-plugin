import asyncio
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin import realtime as realtime_mod
from inkbox_plugin.realtime import (
    DELETE_POST_CALL_ACTION_TOOL_NAME,
    EDIT_POST_CALL_ACTION_TOOL_NAME,
    HANG_UP_CALL_TOOL_NAME,
    POST_CALL_ACTION_TOOL_NAME,
    RealtimeCallMeta,
    RealtimeConfig,
    _BridgeState,
    _dispatch_tool_call,
    _dispatch_post_call,
    _maybe_send_greeting,
    _openai_to_inkbox_pump,
    _send_session_update,
    build_realtime_greeting,
    build_realtime_instructions,
)


if realtime_mod.aiohttp is None:
    realtime_mod.aiohttp = types.SimpleNamespace(
        WSMsgType=types.SimpleNamespace(
            TEXT=object(),
            CLOSE=object(),
            CLOSED=object(),
            ERROR=object(),
        ),
    )


def _meta(**overrides):
    base = {
        "call_id": "call-123",
        "contact_id": "contact-123",
        "contact_name": "Alex Wilcox",
        "remote_phone_number": "+15555550101",
        "direction": "inbound",
        "agent_identity_email": "agent@inkboxmail.com",
        "agent_identity_phone": "+18005550100",
        "contact_known": True,
    }
    base.update(overrides)
    return RealtimeCallMeta(**base)


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_str(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


class _FakeMsg:
    def __init__(self, data):
        self.type = realtime_mod.aiohttp.WSMsgType.TEXT
        self.data = json.dumps(data)


class _FakeOpenAIWS(_FakeWS):
    def __init__(self, frames):
        super().__init__()
        self._frames = [_FakeMsg(frame) for frame in frames]

    def __aiter__(self):
        self._it = iter(self._frames)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def test_instructions_include_full_contact_and_outbound_context():
    text = build_realtime_instructions(_meta(
        contact_name="Dima Vremenko",
        contact_emails=["dima@example.com"],
        contact_phones=["+15167251294"],
        contact_company="Inkbox",
        contact_notes="Prefers SMS.",
        direction="outbound",
        outbound_purpose="Confirm the 3pm meeting",
        outbound_reason="Follow up on the overdue invoice",
        outbound_scheduled_by="billing workflow",
        outbound_conversation_summary="Customer promised to pay by Friday.",
    ))

    assert "do NOT look them up" in text
    assert "dima@example.com" in text
    assert "+15167251294" in text
    assert "Inkbox" in text
    assert "Prefers SMS." in text
    assert "Follow up on the overdue invoice" in text
    assert "billing workflow" in text
    assert "Customer promised to pay by Friday." in text


def test_session_update_exposes_post_call_edit_and_delete_tools():
    ws = _FakeWS()

    asyncio.run(_send_session_update(
        ws,
        RealtimeConfig(enabled=True, api_key="sk-test"),
        _meta(),
    ))

    tool_names = [tool["name"] for tool in ws.sent[0]["session"]["tools"]]
    assert tool_names == [
        "hermes_agent_consult",
        POST_CALL_ACTION_TOOL_NAME,
        EDIT_POST_CALL_ACTION_TOOL_NAME,
        DELETE_POST_CALL_ACTION_TOOL_NAME,
        HANG_UP_CALL_TOOL_NAME,
    ]

    instructions = ws.sent[0]["session"]["instructions"]
    assert EDIT_POST_CALL_ACTION_TOOL_NAME in instructions
    assert DELETE_POST_CALL_ACTION_TOOL_NAME in instructions
    assert HANG_UP_CALL_TOOL_NAME in instructions


def test_unknown_contact_greeting_does_not_use_raw_phone_as_name():
    greeting = build_realtime_greeting(_meta(
        contact_known=False,
        contact_name="+15551234567",
        remote_phone_number="+15551234567",
    ))

    assert "+15551234567" not in greeting
    assert "there" in greeting


def test_proactive_greeting_fires_once_without_output_modalities():
    ws = _FakeWS()
    state = _BridgeState()

    asyncio.run(_maybe_send_greeting(ws, state, _meta()))
    asyncio.run(_maybe_send_greeting(ws, state, _meta()))

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "response.create"
    assert "instructions" in ws.sent[0]["response"]
    assert "output_modalities" not in ws.sent[0]["response"]
    assert state.greeting_triggered is True


def test_openai_audio_frames_match_inkbox_media_protocol():
    inkbox_ws = _FakeWS()
    openai_ws = _FakeOpenAIWS([
        {"type": "response.output_audio.delta", "delta": "AAAA"},
        {"type": "response.output_audio.done"},
        {"type": "input_audio_buffer.speech_started"},
    ])
    state = _BridgeState()
    state.stream_id = "stream-xyz"

    async def _noop(*_args, **_kwargs):
        return ""

    asyncio.run(_openai_to_inkbox_pump(
        openai_ws=openai_ws,
        inkbox_ws=inkbox_ws,
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop,
    ))

    media = next(frame for frame in inkbox_ws.sent if frame.get("event") == "media")
    assert media["media"]["payload"] == "AAAA"
    assert media["media"]["track"] == "outbound"
    assert media["stream_id"] == "stream-xyz"
    assert {"event": "audio_done", "stream_id": "stream-xyz"} in inkbox_ws.sent
    assert {"event": "clear"} in inkbox_ws.sent


def test_ga_function_call_events_dispatch_once_with_buffered_name():
    state = _BridgeState()
    openai_ws = _FakeOpenAIWS([
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item-1",
                "call_id": "call-tool-1",
                "name": POST_CALL_ACTION_TOOL_NAME,
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-1",
            "delta": '{"act',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-1",
            "delta": 'ion":"Email Dima"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item-1",
            "call_id": "call-tool-1",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call-tool-1",
                "name": POST_CALL_ACTION_TOOL_NAME,
                "arguments": '{"action":"Email Dima"}',
            },
        },
    ])

    async def _noop(*_args, **_kwargs):
        return ""

    asyncio.run(_openai_to_inkbox_pump(
        openai_ws=openai_ws,
        inkbox_ws=_FakeWS(),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop,
    ))

    assert state.post_call_actions == [{"action": "Email Dima", "details": ""}]


def test_edit_and_delete_post_call_actions_by_index():
    state = _BridgeState()
    state.post_call_actions = [
        {"action": "Email Dima", "details": ""},
        {"action": "Text Alex", "details": "old wording"},
    ]
    openai_ws = _FakeWS()

    async def _noop(*_args, **_kwargs):
        return ""

    asyncio.run(_dispatch_tool_call(
        openai_ws=openai_ws,
        call_id="edit-call",
        name=EDIT_POST_CALL_ACTION_TOOL_NAME,
        arguments_json=json.dumps({
            "action_index": 2,
            "action": "Send SMS to Alex",
            "details": "new wording",
        }),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop,
    ))

    assert state.post_call_actions[1] == {
        "action": "Send SMS to Alex",
        "details": "new wording",
    }

    asyncio.run(_dispatch_tool_call(
        openai_ws=openai_ws,
        call_id="delete-call",
        name=DELETE_POST_CALL_ACTION_TOOL_NAME,
        arguments_json=json.dumps({"action_index": 1}),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop,
    ))

    assert state.post_call_actions == [{
        "action": "Send SMS to Alex",
        "details": "new wording",
    }]

    outputs = [
        json.loads(frame["item"]["output"])
        for frame in openai_ws.sent
        if frame["type"] == "conversation.item.create"
    ]
    assert outputs[0]["status"] == "updated"
    assert outputs[0]["action_index"] == 2
    assert outputs[1]["status"] == "deleted"
    assert outputs[1]["action_count"] == 1


def test_hangup_tool_sends_hangup_frame_and_closes_sockets():
    state = _BridgeState()
    state.stream_id = "stream-123"
    openai_ws = _FakeWS()
    inkbox_ws = _FakeWS()

    async def _noop(*_args, **_kwargs):
        return ""

    asyncio.run(_dispatch_tool_call(
        openai_ws=openai_ws,
        call_id="hangup-call",
        name=HANG_UP_CALL_TOOL_NAME,
        arguments_json=json.dumps({"reason": "caller said goodbye"}),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop,
        inkbox_ws=inkbox_ws,
    ))

    tool_results = [
        frame for frame in openai_ws.sent
        if frame["type"] == "conversation.item.create"
    ]
    assert len(tool_results) == 1
    assert json.loads(tool_results[0]["item"]["output"])["status"] == "hangup_requested"
    assert not any(frame["type"] == "response.create" for frame in openai_ws.sent)
    assert inkbox_ws.sent == [{
        "event": "hangup",
        "reason": "caller said goodbye",
        "stream_id": "stream-123",
    }]
    assert state.closed is True
    assert inkbox_ws.closed is True
    assert openai_ws.closed is True


def test_post_call_dispatch_runs_exactly_one_path():
    state = _BridgeState()
    state.post_call_actions = [{"action": "Email Dima", "details": ""}]
    calls = {"actions": 0, "ended": 0}

    async def _actions(*_args):
        calls["actions"] += 1

    async def _ended(*_args):
        calls["ended"] += 1

    asyncio.run(_dispatch_post_call(state, _meta(), _actions, _ended))
    assert calls == {"actions": 1, "ended": 0}

    state = _BridgeState()
    asyncio.run(_dispatch_post_call(state, _meta(), _actions, _ended))
    assert calls == {"actions": 1, "ended": 1}


def test_adapter_realtime_call_ended_enqueues_reflection():
    adapter = adapter_mod.InkboxAdapter.__new__(adapter_mod.InkboxAdapter)
    events = []

    async def _enqueue(event):
        events.append(event)

    adapter._enqueue = _enqueue

    asyncio.run(adapter._realtime_call_ended(
        _meta(),
        [("caller", "Can you email me after this call?")],
    ))

    assert len(events) == 1
    assert "[call_ended]" in events[0].text
    assert "Can you email me after this call?" in events[0].text
    assert events[0].raw_message["event"] == "realtime_call_ended"
