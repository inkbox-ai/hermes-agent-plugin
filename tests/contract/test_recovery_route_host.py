"""Recovery routes must not escape their turn through Hermes's real queue drain."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionStore
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.conversation import reply_route


@pytest.mark.parametrize("following_sink", ["voice", "post_call", "hosted"])
def test_real_native_drain_clears_recovery_route_for_unrouted_turn(tmp_path, monkeypatch, following_sink):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
                                               check_fn=lambda: True))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "require_signature": False}))
        adapter.set_session_store(SessionStore(tmp_path / "sessions", GatewayConfig()))
        identity = NS(send_text=Mock(return_value=NS(id="synthetic-sent", delivery_status="queued")))
        adapter._inkbox = NS(get_identity=lambda _: identity)
        adapter._reply_identity = identity
        source = adapter.build_source(chat_id="contact-123", chat_type="dm", user_id="contact-123",
                                      thread_id=None, message_id="voice-followup")
        followup = MessageEvent(text="Unrouted voice turn", source=source, message_type=MessageType.TEXT,
                                message_id="voice-followup", raw_message={"synthetic": "call_ended"})
        enqueue = adapter._enqueue
        adapter._enqueue = capture = AsyncMock()
        await adapter._note_outbound_delivery_failure(
            mode="sms", chat_id="contact-123", thread_id=None, conversation_id=None,
            target="+15555550101", failed_body="Synthetic", error_code="40002",
            error_detail="Temporary rejection", stage="delivery_failed",
        )
        recovery = capture.await_args.args[0]
        adapter._enqueue = enqueue
        assert adapter._event_session_key(recovery) == adapter._event_session_key(followup)
        started, release = asyncio.Event(), asyncio.Event()
        observed = []
        ws = AsyncMock()

        async def model(event):
            observed.append((event.message_id, reply_route.get()))
            if event is recovery:
                started.set()
                await release.wait()
                return "Safe SMS retry"
            if following_sink == "voice":
                adapter._active_call_ws["contact-123"] = ws
            elif following_sink == "post_call":
                import time
                adapter._voice_recently_closed["contact-123"] = time.time()
                adapter._last_inbound_modality.pop("contact-123", None)
            else:
                adapter._hosted_post_call_active_chats["contact-123"] = 1
            return "Legitimate voice reply"

        adapter.set_message_handler(model)
        await (await enqueue(recovery))
        await asyncio.wait_for(started.wait(), 3)
        await (await enqueue(followup))
        key = adapter._event_session_key(recovery)
        assert adapter._pending_messages.get(key) is not None
        release.set()
        for _ in range(300):
            if len(observed) == 2 and not adapter._session_tasks:
                break
            await asyncio.sleep(.01)
        assert not adapter._session_tasks
        assert [item[0] for item in observed] == [recovery.message_id, followup.message_id]
        assert observed[0][1]["mode"] == "sms"
        assert observed[1][1] is None
        assert identity.send_text.call_count == 1
        assert identity.send_text.call_args.kwargs == {"to": "+15555550101", "text": "Safe SMS retry"}
        assert ws.send_str.await_count == (2 if following_sink == "voice" else 0)

    asyncio.run(run())
