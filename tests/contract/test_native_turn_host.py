"""New channel queues use real Hermes admission, processing, and native sessions."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.session import SessionStore, build_session_key
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.native_turns import NativeTurns
from inkbox_plugin.slack_activity import SlackActivity


def uid(value):
    return str(UUID(int=value))


@pytest.mark.parametrize("mode,thread,media", [("imessage", None, False), ("imessage", None, True), ("slack", None, False), ("slack", "1234567890.000001", False)])
def test_real_native_host_queue_checkpoint_and_original_reply(tmp_path, monkeypatch, mode, thread, media):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("INKBOX_ALLOWED_USERS", "+15555550101")
        monkeypatch.setenv("INKBOX_ALLOW_ALL_USERS", "false")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allowed_users_env="INKBOX_ALLOWED_USERS", allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "require_signature": False}))
        config = GatewayConfig()
        config.thread_sessions_per_user = False
        store = SessionStore(tmp_path / "sessions", config)
        submissions = []
        gate = asyncio.Event()
        picture = tmp_path / "example.png"
        picture.write_bytes(b"synthetic test picture")
        class Runner(GatewayAuthorizationMixin, GatewayAdapterLifecycleMixin):
            def __init__(self):
                self.session_store, self.config = store, config
                self.adapters = {adapter.platform: adapter}
            def _pairing_store_for(self, source):
                return None
            def _standalone_launch_scope(self):
                return nullcontext()
            def _session_key_for_source(self, source):
                return build_session_key(source)
            async def _handle_message(self, event):
                assert self._is_user_authorized(event.source)
                assert not event.allow_gateway_control
                submissions.append(event)
                await gate.wait()
                return "The answer to " + event.message_id + ("\nMEDIA:" + str(picture) if media else "")
        runner = Runner()
        adapter.gateway_runner = runner
        adapter.set_session_store(store)
        adapter.set_message_handler(runner._primary_message_handler())
        adapter._resolve_contact_full = AsyncMock(return_value=None)
        adapter._identity_id = uid(100)
        adapter._slack_enabled = True
        adapter._imessage_threaded_replies = True
        identity = NS(send_imessage=Mock(return_value=NS(id=uid(80))), upload_imessage_media=Mock(return_value=NS(media_url="https://example.com/picture.png")))
        slack = Mock()
        slack.send_message.return_value = NS(id=uid(81), status="sent")
        for name in ("set_processing_status", "add_reaction", "remove_reaction"):
            getattr(slack, name).return_value = NS(status="succeeded")
        adapter._inkbox = NS(slack=slack, get_identity=lambda _: identity)
        adapter._reply_identity = identity
        adapter._slack_activity = SlackActivity(slack, tmp_path / "activity.json")
        adapter._native_turns = queue = NativeTurns(adapter, tmp_path / "queue")
        queue.quiet_seconds = .01
        queue.max_burst_seconds = .02
        await queue.start()
        def event(number):
            source = adapter.build_source(chat_id="native-conversation", chat_type="group", thread_id="conversation-route",
                user_id="+15555550101", user_id_alt="+15555550101", message_id=uid(number))
            route = {"chat_id": source.chat_id, "message_id": uid(number), "author": source.user_id, "mode": mode,
                "conversation_id": uid(30) if mode == "imessage" else "CEXAMPLE", "imessage_reply_target": uid(number),
                "connection_id": uid(40), "thread_ts": thread, "message_ts": f"1234567891.{number:06}", "source_event_id": uid(number)}
            return MessageEvent(text="Question " + str(number), source=source, message_type=MessageType.TEXT,
                message_id=uid(number), metadata={"inkbox_prepared": True, "inkbox_reply_route": route})
        await queue.accept(event(1))
        for _ in range(200):
            if submissions:
                break
            await asyncio.sleep(.01)
        assert len(submissions) == 1
        await queue.accept(event(2))
        await asyncio.sleep(.05)
        assert len(submissions) == 1
        gate.set()
        for _ in range(300):
            if not queue.tasks:
                break
            await asyncio.sleep(.01)
        assert not queue.tasks
        assert len(submissions) == 2
        rows = next(iter(queue.rows.values()))["turns"]
        assert [turn["state"] for turn in rows] == ["done", "done"]
        assert all(turn["answer"] and turn["sent"] for turn in rows)
        assert rows[0]["session_id"] == rows[1]["session_id"]
        if mode == "imessage":
            assert [call.kwargs["reply_to_message_id"] for call in identity.send_imessage.call_args_list] == ([uid(1), uid(1), uid(2), uid(2)] if media else [uid(1), uid(2)])
            if media:
                assert identity.upload_imessage_media.call_count == 2
                assert all(turn["rendered"]["media_files"] and turn["media_deliveries"] for turn in rows)
        else:
            assert [call.kwargs["thread_ts"] for call in slack.send_message.call_args_list] == [thread, thread]
        await queue.close()
        await adapter._slack_activity.close()
    asyncio.run(run())
