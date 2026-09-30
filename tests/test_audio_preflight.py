import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_realtime_bridge_parity import _FakeClientSession, _FakeOpenAIWS, _meta
from inkbox_plugin import adapter as adapter_mod, realtime


@pytest.mark.parametrize("failure", ["missing_module", "ratecv", "ulaw2lin", "lin2ulaw"])
def test_audio_failure_is_detected_before_opening_a_session(monkeypatch, failure):
    if failure == "missing_module":
        monkeypatch.setitem(sys.modules, "audioop", None)
        expected = ModuleNotFoundError
    else:
        import audioop

        monkeypatch.setattr(audioop, failure, Mock(side_effect=RuntimeError("conversion unavailable")))
        expected = RuntimeError
    session_factory = Mock()
    monkeypatch.setattr(realtime.aiohttp, "ClientSession", session_factory)

    with pytest.raises(realtime.RealtimeBridgeConnectError) as error:
        asyncio.run(realtime.open_inkbox_realtime_bridge(
            config=realtime.RealtimeConfig(enabled=True, api_key="test-key"),
            meta=_meta(contact_name="Sample Caller"),
        ))

    assert isinstance(error.value.cause, expected)
    session_factory.assert_not_called()


def test_audio_preflight_leaves_live_converters_unused(monkeypatch):
    session = _FakeClientSession()
    monkeypatch.setattr(realtime.aiohttp, "ClientSession", lambda: session)

    async def run():
        bridge = await realtime.open_inkbox_realtime_bridge(
            config=realtime.RealtimeConfig(enabled=True, api_key="test-key"),
            meta=_meta(contact_name="Sample Caller"),
        )
        try:
            for converter in (bridge.state.audio.inbound, bridge.state.audio.outbound):
                assert converter._state is None
                assert converter._pending == b""
        finally:
            await bridge.close()

    asyncio.run(run())
    assert session.closed


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("fallback", [True, False])
def test_missing_audio_dependency_respects_call_fallback(monkeypatch, direction, fallback):
    class CallSocket(_FakeOpenAIWS):
        def __init__(self):
            super().__init__([
                {"event": "start"},
                {"event": "transcript", "is_final": True, "text": "Hello", "turn_id": "turn-1"},
                {"event": "stop"},
            ])
            self.headers = {}
            self.prepared = False

        async def prepare(self, request):
            self.prepared = True

        async def __anext__(self):
            await asyncio.sleep(0)
            return await super().__anext__()

    socket = CallSocket()
    monkeypatch.setattr(adapter_mod.web, "WebSocketResponse", lambda: socket)
    monkeypatch.setitem(sys.modules, "audioop", None)
    session_factory = Mock()
    monkeypatch.setattr(realtime.aiohttp, "ClientSession", session_factory)
    adapter = adapter_mod.InkboxAdapter.__new__(adapter_mod.InkboxAdapter)
    adapter.platform = SimpleNamespace(value="inkbox")
    adapter._require_signature = False
    adapter._call_ws_meta = {hash("call-123"): {
        "call_id": "call-123", "contact_id": "contact-123", "direction": direction,
    }}
    adapter._inkbox = None
    adapter._identity_handle = "sample-agent"
    adapter._realtime_config = realtime.RealtimeConfig(
        enabled=True, api_key="test-key", fallback_to_inkbox_stt_tts=fallback,
    )
    adapter._active_call_ws = {}
    adapter._last_inbound_modality = {}
    adapter._voice_recently_closed = {}
    adapter._resolve_channel_overrides = lambda *args: (None, None)
    adapter._enqueue = AsyncMock()
    request = SimpleNamespace(query={"call_id": "call-123"}, headers={})

    result = asyncio.run(adapter._handle_call_ws(request))

    session_factory.assert_not_called()
    assert adapter._active_call_ws == {}
    assert adapter._last_inbound_modality == {}
    if fallback:
        assert result is socket
        assert socket.prepared
        assert socket.headers == {
            "x-use-inkbox-text-to-speech": "true",
            "x-use-inkbox-speech-to-text": "true",
        }
        events = [call.args[0] for call in adapter._enqueue.await_args_list]
        assert any(event.raw_message.get("event") == "transcript" for event in events)
        if direction == "inbound":
            assert any(frame.get("delta") == "Hi there, how can I help?" for frame in socket.sent)
        else:
            assert any(event.raw_message.get("synthetic") == "outbound_call_opening" for event in events)
    else:
        assert result.status == 503
        assert not socket.prepared
        assert socket.sent == []
        adapter._enqueue.assert_not_awaited()
