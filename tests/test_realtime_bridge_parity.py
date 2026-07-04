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
    AGENT_CONSULT_TOOL_NAME,
    DELETE_POST_CALL_ACTION_TOOL_NAME,
    EDIT_POST_CALL_ACTION_TOOL_NAME,
    HANG_UP_CALL_TOOL_NAME,
    POST_CALL_ACTION_TOOL_NAME,
    RealtimeBridgeConnectError,
    RealtimeCallMeta,
    RealtimeConfig,
    RealtimeConsultResult,
    _BridgeState,
    _dispatch_tool_call,
    _dispatch_post_call,
    _maybe_send_greeting,
    _openai_to_inkbox_pump,
    _send_session_update,
    build_realtime_greeting,
    build_realtime_instructions,
    open_inkbox_realtime_bridge,
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


class _FakeClientSession:
    def __init__(self, *, ws=None, error=None):
        self.ws = ws or _FakeWS()
        self.error = error
        self.calls = []
        self.closed = False

    async def ws_connect(self, url, *, headers, heartbeat):
        self.calls.append({"url": url, "headers": headers, "heartbeat": heartbeat})
        if self.error:
            raise self.error
        return self.ws

    async def close(self):
        self.closed = True


def test_instructions_include_full_contact_and_outbound_context():
    text = build_realtime_instructions(_meta(
        contact_name="Dima Vremenko",
        contact_emails=["dima@example.com"],
        contact_phones=["+15167251294"],
        contact_company="Inkbox",
        contact_notes="Prefers SMS.",
        direction="outbound",
        outbound_purpose="Confirm the 3pm meeting",
    ))

    assert "do NOT look them up" in text
    assert "dima@example.com" in text
    assert "+15167251294" in text
    assert "Inkbox" in text
    assert "Prefers SMS." in text
    assert "Confirm the 3pm meeting" in text


def test_open_realtime_bridge_preflights_openai_before_inkbox_accept(monkeypatch):
    openai_ws = _FakeWS()
    session = _FakeClientSession(ws=openai_ws)
    monkeypatch.setattr(realtime_mod.aiohttp, "ClientSession", lambda: session, raising=False)

    bridge = asyncio.run(open_inkbox_realtime_bridge(
        config=RealtimeConfig(enabled=True, api_key="sk-test", connect_timeout_s=1),
        meta=_meta(),
    ))

    assert bridge.openai_ws is openai_ws
    assert session.calls[0]["url"].startswith("wss://api.openai.com/v1/realtime?")
    assert session.calls[0]["headers"] == {"Authorization": "Bearer sk-test"}
    assert openai_ws.sent[0]["type"] == "session.update"

    asyncio.run(bridge.close())
    assert openai_ws.closed is True
    assert session.closed is True


def test_open_realtime_bridge_raises_connect_error_and_closes_session(monkeypatch):
    session = _FakeClientSession(error=RuntimeError("boom"))
    monkeypatch.setattr(realtime_mod.aiohttp, "ClientSession", lambda: session, raising=False)

    try:
        asyncio.run(open_inkbox_realtime_bridge(
            config=RealtimeConfig(enabled=True, api_key="sk-test", connect_timeout_s=1),
            meta=_meta(),
        ))
    except RealtimeBridgeConnectError as exc:
        assert "boom" in str(exc.cause)
    else:
        raise AssertionError("expected RealtimeBridgeConnectError")

    assert session.closed is True


def test_session_update_exposes_post_call_edit_and_delete_tools():
    ws = _FakeWS()

    asyncio.run(_send_session_update(
        ws,
        RealtimeConfig(enabled=True, api_key="sk-test"),
        _meta(),
    ))

    tool_names = [tool["name"] for tool in ws.sent[0]["session"]["tools"]]
    assert tool_names == [
        "consult_agent",
        POST_CALL_ACTION_TOOL_NAME,
        EDIT_POST_CALL_ACTION_TOOL_NAME,
        DELETE_POST_CALL_ACTION_TOOL_NAME,
        HANG_UP_CALL_TOOL_NAME,
        "inkbox_lookup_contact",
        "inkbox_list_contacts",
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


def test_realtime_transcripts_are_mirrored_into_inkbox():
    # Raw-media realtime means Inkbox runs no STT/TTS, so the platform records
    # no transcript on its own. The pump must mirror each finalized turn back as
    # a `transcript` event so it lands in the Inkbox call record.
    inkbox_ws = _FakeWS()
    openai_ws = _FakeOpenAIWS([
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "hey can you check the build"},
        {"type": "response.output_audio_transcript.done",
         "transcript": "sure, the build is green"},
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

    transcripts = [f for f in inkbox_ws.sent if f.get("event") == "transcript"]
    assert transcripts == [
        {"event": "transcript", "party": "remote", "text": "hey can you check the build", "is_final": True},
        {"event": "transcript", "party": "local", "text": "sure, the build is green", "is_final": True},
    ]
    # And still collected in-memory for consult context / post-call reflection.
    assert state.transcript == [
        ("caller", "hey can you check the build"),
        ("agent", "sure, the build is green"),
    ]


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


def test_agent_consult_does_not_block_audio_pump():
    # The consult tool runs the full main agent loop and can take seconds. It
    # must be dispatched off the read loop so audio (and barge-in) keep flowing
    # while the agent thinks — otherwise the caller hears dead air. Under the old
    # inline-await behavior this test would deadlock: the pump would never read
    # past the consult frame to forward the audio delta.

    async def _run():
        release = asyncio.Event()

        async def _slow_consult(*_args, **_kwargs):
            await release.wait()  # hold the consult open like a slow agent turn
            return "Your balance is forty two dollars."

        inkbox_ws = _FakeWS()
        openai_ws = _FakeOpenAIWS([
            # Model invokes the consult tool...
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "item-c",
                    "call_id": "consult-1",
                    "name": AGENT_CONSULT_TOOL_NAME,
                },
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "item-c",
                "call_id": "consult-1",
                "arguments": '{"query":"what is my balance"}',
            },
            # ...and audio arrives WHILE the consult is still running. The pump
            # must forward this without waiting for the consult to finish.
            {"type": "response.output_audio.delta", "delta": "ONEMOMENT"},
        ])
        state = _BridgeState()
        state.stream_id = "stream-c"

        await _openai_to_inkbox_pump(
            openai_ws=openai_ws,
            inkbox_ws=inkbox_ws,
            state=state,
            config=RealtimeConfig(enabled=True, api_key="sk-test"),
            meta=_meta(),
            on_agent_consult=_slow_consult,
        )

        # Audio that arrived during the consult reached Inkbox even though the
        # consult has not returned yet.
        assert any(
            frame.get("event") == "media"
            and frame["media"]["payload"] == "ONEMOMENT"
            for frame in inkbox_ws.sent
        )
        # The consult was handed off to a background task, not awaited inline.
        assert len(state.consult_tasks) == 1
        # The slow consult result has NOT been submitted to OpenAI yet.
        assert [
            frame for frame in openai_ws.sent
            if frame["type"] == "conversation.item.create"
        ] == []

        # Release the consult and let the background task finish; its result is
        # then recorded for post-call follow-up.
        release.set()
        for task in list(state.consult_tasks):
            await task
        assert state.consult_results
        assert state.consult_results[0].result == "Your balance is forty two dollars."

    asyncio.run(_run())


def test_inflight_consult_is_cancelled_when_call_tears_down():
    # If the call ends with a consult still running, the bridge cancels the
    # background task instead of leaking it.
    async def _run():
        state = _BridgeState()
        never = asyncio.Event()

        async def _hang():
            await never.wait()

        task = asyncio.create_task(_hang())
        state.consult_tasks.add(task)

        from inkbox_plugin.realtime import _cancel_consult_tasks
        await _cancel_consult_tasks(state)

        assert task.cancelled()
        assert state.consult_tasks == set()

    asyncio.run(_run())


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


async def _noop_consult(*_args, **_kwargs):
    return ""


def _dispatch_hangup(state, openai_ws, inkbox_ws):
    """Run one hang_up_call dispatch against the given state/sockets."""
    asyncio.run(_dispatch_tool_call(
        openai_ws=openai_ws,
        call_id="hangup-call",
        name=HANG_UP_CALL_TOOL_NAME,
        arguments_json=json.dumps({"reason": "caller said goodbye"}),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop_consult,
        inkbox_ws=inkbox_ws,
    ))


def test_hangup_first_call_arms_and_requests_goodbye():
    # First hang_up_call must NOT drop the line — it arms the hangup and asks
    # the model to say goodbye, keeping both sockets open.
    state = _BridgeState()
    state.stream_id = "stream-123"
    openai_ws = _FakeWS()
    inkbox_ws = _FakeWS()

    _dispatch_hangup(state, openai_ws, inkbox_ws)

    tool_results = [
        frame for frame in openai_ws.sent
        if frame["type"] == "conversation.item.create"
    ]
    assert len(tool_results) == 1
    assert json.loads(tool_results[0]["item"]["output"])["status"] == "confirm_goodbye"
    # Default create_response=True so the model speaks the goodbye.
    assert any(frame["type"] == "response.create" for frame in openai_ws.sent)
    # Armed, but nothing torn down yet.
    assert state.hangup_armed_at is not None
    assert inkbox_ws.sent == []
    assert state.closed is False
    assert inkbox_ws.closed is False
    assert openai_ws.closed is False


def test_hangup_second_call_sleeps_then_sends_frame_and_closes_sockets(monkeypatch):
    # Second hang_up_call within the confirm window performs the real hangup.
    state = _BridgeState()
    state.stream_id = "stream-123"
    openai_ws = _FakeWS()
    inkbox_ws = _FakeWS()
    sleeps = []

    async def _fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(realtime_mod.asyncio, "sleep", _fake_sleep)

    _dispatch_hangup(state, openai_ws, inkbox_ws)  # arm
    _dispatch_hangup(state, openai_ws, inkbox_ws)  # confirm → real hangup

    tool_results = [
        frame for frame in openai_ws.sent
        if frame["type"] == "conversation.item.create"
    ]
    # One result per call; the second is the actual hangup.
    assert len(tool_results) == 2
    assert json.loads(tool_results[-1]["item"]["output"])["status"] == "hangup_requested"
    assert inkbox_ws.sent == [{
        "event": "stop",
        "reason": "caller said goodbye",
        "stream_id": "stream-123",
    }]
    assert sleeps == [realtime_mod.HANGUP_CLOSE_DELAY_S]
    assert state.closed is True
    assert inkbox_ws.closed is True
    assert openai_ws.closed is True


def test_post_call_dispatch_runs_exactly_one_path():
    state = _BridgeState()
    state.post_call_actions = [{"action": "Email Dima", "details": ""}]
    state.consult_results = [
        RealtimeConsultResult(
            id="consult-1",
            request="Send SMS now",
            result="SMS queued",
            created_at=1.0,
            dedupe_key="sms:+15555550101:generic",
        )
    ]
    calls = {"actions": 0, "ended": 0}
    seen = {}

    async def _actions(meta, actions, transcript, consult_results):
        calls["actions"] += 1
        seen["consult_results"] = consult_results

    async def _ended(*_args):
        calls["ended"] += 1

    asyncio.run(_dispatch_post_call(state, _meta(), _actions, _ended))
    assert calls == {"actions": 1, "ended": 0}
    assert seen["consult_results"][0].result == "SMS queued"

    state = _BridgeState()
    asyncio.run(_dispatch_post_call(state, _meta(), _actions, _ended))
    assert calls == {"actions": 1, "ended": 1}


def test_agent_consult_records_result_and_guides_post_call_cleanup():
    state = _BridgeState()
    state.post_call_actions = [{"action": "Text Alex after call", "details": ""}]
    openai_ws = _FakeWS()
    seen = {}

    async def _consult(meta, query, transcript, post_call_actions, consult_results):
        seen["query"] = query
        seen["post_call_actions"] = post_call_actions
        seen["consult_results"] = consult_results
        return "SMS sent to Alex."

    asyncio.run(_dispatch_tool_call(
        openai_ws=openai_ws,
        call_id="consult-call",
        name="consult_agent",
        arguments_json=json.dumps({"query": 'Send SMS to +15555550101 "hello alex" now'}),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_consult,
    ))

    assert seen["query"].startswith("Send SMS")
    assert seen["post_call_actions"] == [{"action": "Text Alex after call", "details": ""}]
    assert seen["consult_results"] == []
    assert len(state.consult_results) == 1
    assert state.consult_results[0].result == "SMS sent to Alex."
    outputs = [
        json.loads(frame["item"]["output"])
        for frame in openai_ws.sent
        if frame["type"] == "conversation.item.create"
    ]
    assert outputs[-1]["status"] == "ok"
    assert "post_call_action_guidance" in outputs[-1]


def test_agent_consult_dedupes_completed_same_sms_request():
    state = _BridgeState()
    state.consult_results = [
        RealtimeConsultResult(
            id="consult-original",
            request='Send SMS to +15555550101 "hello alex"',
            result="SMS sent to Alex.",
            created_at=1.0,
            dedupe_key="sms:+15555550101:hello alex",
        )
    ]
    openai_ws = _FakeWS()
    called = False

    async def _consult(*_args):
        nonlocal called
        called = True
        return "should not run"

    asyncio.run(_dispatch_tool_call(
        openai_ws=openai_ws,
        call_id="consult-dupe",
        name="consult_agent",
        arguments_json=json.dumps({"query": 'Please text +15555550101 "hello alex"'}),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_consult,
    ))

    outputs = [
        json.loads(frame["item"]["output"])
        for frame in openai_ws.sent
        if frame["type"] == "conversation.item.create"
    ]
    assert called is False
    assert outputs[-1]["status"] == "already_handled"
    assert outputs[-1]["existing_tool_call_id"] == "consult-original"


def test_agent_consult_allows_explicit_repeat_sms_request():
    state = _BridgeState()
    state.consult_results = [
        RealtimeConsultResult(
            id="consult-original",
            request='Send SMS to +15555550101 "hello alex"',
            result="SMS sent to Alex.",
            created_at=1.0,
            dedupe_key="sms:+15555550101:hello alex",
        )
    ]
    openai_ws = _FakeWS()

    async def _consult(*_args):
        return "Second SMS sent."

    asyncio.run(_dispatch_tool_call(
        openai_ws=openai_ws,
        call_id="consult-repeat",
        name="consult_agent",
        arguments_json=json.dumps({"query": 'Send another text to +15555550101 "hello alex"'}),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_consult,
    ))

    assert len(state.consult_results) == 2
    assert state.consult_results[-1].result == "Second SMS sent."


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
    # Channel-override resolution normalizes auto_skill to a deduped list.
    assert events[0].auto_skill == ["inkbox:inkbox-call-review"]


def test_adapter_realtime_post_call_actions_enqueues_without_auto_skill():
    adapter = adapter_mod.InkboxAdapter.__new__(adapter_mod.InkboxAdapter)
    events = []

    async def _enqueue(event):
        events.append(event)

    adapter._enqueue = _enqueue

    asyncio.run(adapter._realtime_post_call_actions(
        _meta(),
        [{"action": "Email Dima after the call", "details": "Keep it short."}],
        [
            ("caller", "Can you email me after this call?"),
            ("agent", "I queued it for after the call."),
        ],
        [
            RealtimeConsultResult(
                id="consult-1",
                request="Send the email now",
                result="Email sent to Dima.",
                created_at=1.0,
            )
        ],
    ))

    assert len(events) == 1
    assert "voice_post_call_actions" in events[0].text
    assert "Email Dima after the call" in events[0].text
    assert "execute only the actions that are still needed" in events[0].text
    assert "In-call Hermes consult results" in events[0].text
    assert "Email sent to Dima." in events[0].text
    assert "Full live-call transcript" in events[0].text
    assert "I queued it for after the call." in events[0].text
    assert events[0].raw_message["event"] == "realtime_post_call_actions"
    assert events[0].raw_message["consult_results"][0]["result"] == "Email sent to Dima."
    assert getattr(events[0], "auto_skill", None) is None
