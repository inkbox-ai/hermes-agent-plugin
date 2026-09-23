"""Exercise Companion inputs through the real Hermes background processor."""

import asyncio
import copy
import json
import sys
import types
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

pytest.importorskip("hermes_cli.plugins")

ROOT = Path(__file__).resolve().parents[2]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.session import SessionStore, build_session_key
from inkbox.companion import CompanionResource
from inkbox_plugin import adapter as adapter_module
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.companion import CompanionReceiver


@pytest.mark.parametrize("channel", ["mail", "phone", "imessage"])
@pytest.mark.parametrize("scenario", ["complete", "close", "sponsor_denied", "delivery_failure"])
def test_real_host_companion_lifecycle(tmp_path, monkeypatch, channel, scenario):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        sponsor = "sponsor@example.com" if channel == "mail" else "+15555550101"
        monkeypatch.setenv("GATEWAY_ALLOWED_USERS", sponsor)
        monkeypatch.delenv("INKBOX_ALLOWED_USERS", raising=False)
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "false")
        monkeypatch.setenv("INKBOX_ALLOW_ALL_USERS", "false")
        platform_registry.register(PlatformEntry(
            name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True,
            allowed_users_env="INKBOX_ALLOWED_USERS", allow_all_env="INKBOX_ALLOW_ALL_USERS",
        ))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "require_signature": False}))
        config = GatewayConfig()
        config.thread_sessions_per_user = False
        store = SessionStore(tmp_path / "sessions", config)
        submissions = []
        gate = asyncio.Event()
        cancelled = asyncio.Event()
        finish_cancellation = asyncio.Event()

        class Runner(GatewayAuthorizationMixin, GatewayAdapterLifecycleMixin):
            def __init__(self):
                self.session_store = store
                self.config = config
                self.adapters = {adapter.platform: adapter}

            def _pairing_store_for(self, source):
                return None

            def _standalone_launch_scope(self):
                return nullcontext()

            def _session_key_for_source(self, source):
                return build_session_key(source)

            async def _handle_message(self, incoming):
                assert self._is_user_authorized(incoming.source)
                assert incoming.get_command() is None
                submissions.append(incoming)
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await finish_cancellation.wait()
                return "[SILENT]" if scenario == "complete" else "Group reply"

        runner = Runner()
        adapter.gateway_runner = runner
        adapter.set_session_store(store)
        adapter.set_message_handler(runner._primary_message_handler())
        assert getattr(adapter._message_handler, "__self__", None) is None
        adapter._resolve_contact_full = AsyncMock(return_value=None)
        adapter._resolve_channel_overrides = lambda *_args: (None, None)
        adapter._identity_id = str(UUID(int=100))
        fixture = json.loads((ROOT / "tests" / "fixtures" / "companion-v1.json").read_text())
        assert fixture["version"] == 1
        pages = fixture["pages"]
        for page in pages:
            page["channel"] = page["reply_context"]["channel"] = channel
            if channel != "mail":
                page["reply_context"] = {"channel": channel, "conversation_id": page["conversation_id"]}
                for entry in page["items"]:
                    entry["author"] = {"fred@example.com": "+15555550102", "nancy@example.com": "+15555550103",
                                       "sponsor@example.com": sponsor}[entry["author"]]
        meta = {key: pages[0][key] for key in ("channel", "conversation_id", "scope_id", "activation_id")}

        def delivery(number):
            item = {"id": pages[1]["items"][-1]["id"] if number == 3 else str(UUID(int=number)),
                    "direction": "inbound", "sender_access": "direct", "created_at": "2026-09-01T10:03:00Z"}
            author = sponsor if number == 3 else "fred@example.com" if channel == "mail" else "+15555550102"
            if channel == "mail":
                item.update(from_address=author, thread_id=meta["conversation_id"], body="Live group message")
            elif channel == "phone":
                item.update(sender_phone_number=author, conversation_id=meta["conversation_id"], text="Live group message")
            else:
                item.update(sender_number=author, remote_number=None, conversation_id=meta["conversation_id"], content="Live group message")
            return {
                "event_type": {"mail": "message.received", "phone": "text.received", "imessage": "imessage.received"}[channel],
                "data": {"text_message" if channel == "phone" else "message": item},
                "companion": {**meta, "sequence": number, "phase": "initialization" if number == 3 else "live"},
            }

        incoming = delivery(3)

        def get(_path, params):
            cursor = params.get("cursor")
            return copy.deepcopy(pages[0] if cursor is None else pages[1])

        identity = types.SimpleNamespace(**{name: Mock(return_value=types.SimpleNamespace(id=str(UUID(int=800))))
                                           for name in ("reply_all_email", "send_text", "send_imessage")})
        adapter._inkbox = types.SimpleNamespace(
            companion=CompanionResource(types.SimpleNamespace(get=get)), get_identity=Mock(return_value=identity),
        )
        receiver = CompanionReceiver(adapter, tmp_path / "receipts")
        adapter._companion = receiver
        enqueue = adapter._enqueue
        count = Mock()

        async def counted(item):
            count(item)
            return await enqueue(item)

        adapter._enqueue = counted
        await receiver.accept(incoming)
        for _ in range(300):
            if submissions:
                break
            await asyncio.sleep(0.01)
        assert len(submissions) == 1
        await receiver.accept(incoming)
        await receiver.accept(delivery(4))
        assert len(submissions) == 1 and count.call_count == 1
        if scenario == "close":
            original = submissions[0]
            session_key = receiver._session_key(original)
            native_task = adapter._session_tasks[session_key]
            checkpoint = next(receiver.root.glob("*.json"))
            submitted = checkpoint.read_bytes()
            closing = asyncio.create_task(receiver.close())
            await asyncio.wait_for(cancelled.wait(), 2)
            recovered = CompanionReceiver(adapter, receiver.root)
            with pytest.raises(RuntimeError, match="Another Companion receiver"):
                await recovered.start()
            assert not closing.done() and not native_task.done()
            assert checkpoint.read_bytes() == submitted
            finish_cancellation.set()
            await closing
            assert native_task.done() and session_key not in adapter._session_tasks
            assert checkpoint.read_bytes() == submitted
            adapter._companion = recovered
            await recovered.start()
            row = next(iter(recovered.rows.values()))
            assert row["state"] == "paused" and row["turns"][0]["state"] == "submitted"
            assert row["turns"][1]["state"] == "pending"
            paused = checkpoint.read_bytes()
            receiver.processing(original, "success")
            await adapter.on_processing_start(original)
            await adapter.on_processing_complete(original, "success")
            assert not (await receiver.send(original.source.chat_id, "Stale reply", original.message_id)).success
            assert not (await adapter.send(original.source.chat_id, "Stale reply", reply_to=original.message_id)).success
            await asyncio.sleep(0)
            assert checkpoint.read_bytes() == paused
            assert len(submissions) == 1 and count.call_count == 1
            for method in (identity.reply_all_email, identity.send_text, identity.send_imessage):
                method.assert_not_called()
            await recovered.close()
            return
        if scenario == "sponsor_denied":
            monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "other@example.com")
        gate.set()
        for _ in range(300):
            if not receiver.tasks:
                break
            await asyncio.sleep(0.01)
        if scenario == "sponsor_denied":
            assert len(submissions) == 1 and count.call_count == 1
            for method in (identity.reply_all_email, identity.send_text, identity.send_imessage):
                method.assert_not_called()
            row = next(iter(receiver.rows.values()))
            assert row["state"] == "paused" and row["turns"][1]["state"] == "pending"
            await receiver.close()
            return
        assert len(submissions) == 2 and count.call_count == 2
        assert submissions[0].source.chat_id == submissions[1].source.chat_id
        assert submissions[0].source.thread_id.endswith(meta["scope_id"])
        assert "YES" in submissions[0].text and "/clear" in submissions[0].text
        assert submissions[1].source.role_authorized is True
        row = next(iter(receiver.rows.values()))
        assert row["state"] == "initialized"
        assert row["host_session_id"] == store.get_or_create_session(submissions[0].source).session_id
        if scenario == "delivery_failure":
            failed = {"id": str(UUID(int=800)), "direction": "outbound", "status": "delivery_failed"}
            if channel == "mail":
                failed.update(thread_id=meta["conversation_id"], to_addresses=[sponsor], snippet="Failed group output")
            elif channel == "phone":
                failed.update(conversation_id=meta["conversation_id"], remote_phone_number=sponsor, text="Failed group output")
            else:
                failed.update(conversation_id=meta["conversation_id"], remote_number=sponsor, content="Failed group output")
            event_types = {"mail": ["message.bounced", "message.failed"], "phone": ["text.delivery_failed"],
                           "imessage": ["imessage.delivery_failed"]}[channel]
            provider = types.SimpleNamespace(name="inkbox", verify=Mock(return_value=False))
            monkeypatch.setattr(adapter_module, "match_provider", lambda _headers: provider)
            adapter._resolve_contact_full.reset_mock()
            adapter._resolve_contact_full.side_effect = AssertionError("Unexpected contact routing")
            adapter._note_outbound_delivery_failure = AsyncMock(side_effect=AssertionError("Unexpected ordinary recovery"))
            runner.session_store.get_or_create_session = Mock(wraps=store.get_or_create_session)
            checkpoint = next(receiver.root.glob("*.json"))
            before = checkpoint.read_bytes()
            send_calls = [method.call_count for method in (identity.reply_all_email, identity.send_text, identity.send_imessage)]
            for event_type in event_types:
                failure = {"event_type": event_type, "data": {"text_message" if channel == "phone" else "message": failed}}
                request = types.SimpleNamespace(read=AsyncMock(return_value=json.dumps(failure).encode()),
                                                headers={}, url="https://example.com/webhook")
                if not provider.verify.return_value:
                    assert (await adapter._handle_webhook(request)).status == 401
                    assert checkpoint.read_bytes() == before
                    provider.verify.return_value = True
                assert (await adapter._handle_webhook(request)).status == 200
                diagnostic = row["delivery_failures"][failed["id"]]
                assert diagnostic["event_type"] == event_type and diagnostic["conversation_id"] == meta["conversation_id"]
                assert "no automatic recovery" in diagnostic["message"]
            saved = checkpoint.read_bytes()
            assert b"Failed group output" not in saved
            assert len(submissions) == 2 and count.call_count == 2
            adapter._resolve_contact_full.assert_not_called()
            adapter._note_outbound_delivery_failure.assert_not_called()
            runner.session_store.get_or_create_session.assert_not_called()
            await receiver.close()
            recovered = CompanionReceiver(adapter, receiver.root)
            adapter._companion = recovered
            await recovered.start()
            assert next(iter(recovered.rows.values()))["delivery_failures"] == row["delivery_failures"]
            assert (await adapter._handle_webhook(request)).status == 200
            await asyncio.sleep(0)
            assert checkpoint.read_bytes() == saved
            assert len(submissions) == 2 and count.call_count == 2
            assert [method.call_count for method in (identity.reply_all_email, identity.send_text, identity.send_imessage)] == send_calls
            adapter._resolve_contact_full.assert_not_called()
            adapter._note_outbound_delivery_failure.assert_not_called()
            runner.session_store.get_or_create_session.assert_not_called()
            await recovered.close()
            return
        await receiver.close()
        runner.session_store = SessionStore(tmp_path / "sessions", config)
        adapter.set_session_store(runner.session_store)
        recovered = CompanionReceiver(adapter, tmp_path / "receipts")
        adapter._companion = recovered
        await recovered.start()
        await recovered.accept(delivery(5))
        for _ in range(300):
            if not recovered.tasks:
                break
            await asyncio.sleep(0.01)
        assert len(submissions) == 3 and count.call_count == 3
        assert submissions[2].source.chat_id == submissions[0].source.chat_id
        await recovered.close()
        for task in list(adapter._background_tasks):
            task.cancel()
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    asyncio.run(run())
