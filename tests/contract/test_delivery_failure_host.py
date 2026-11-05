"""Delivery-failure follow-ups traverse the real host admission and send path."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("gateway.platforms.webhook_ingress")

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from gateway.turn_context import TurnContext
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.conversation import reply_route
from inkbox_plugin.host_fencing import observe_native_workers


@pytest.mark.parametrize("mode", ["sms", "email", "imessage"])
@pytest.mark.parametrize("authorized", [True, False])
def test_delivery_failure_reaches_model_and_original_destination(tmp_path, monkeypatch, mode, authorized):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("INKBOX_ALLOWED_USERS", "sample-contact" if authorized else "unrelated-contact")
        monkeypatch.setenv("INKBOX_ALLOW_ALL_USERS", "false")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allowed_users_env="INKBOX_ALLOWED_USERS", allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        adapter = InkboxAdapter(PlatformConfig(enabled=True, typing_indicator=False, extra={"identity": "sample-agent"}))
        adapter._base_url, adapter._identity_id = "https://example.test", "sample-identity"
        adapter._resolve_contact_full = AsyncMock(return_value={"id": "sample-contact", "name": "Synthetic contact"})
        adapter._resolve_channel_overrides = lambda *args: (None, None)
        runner = GatewayRunner(GatewayConfig())
        runner.adapters = {adapter.platform: adapter}
        authority = await initialize_session_authority(runner, profile_id="default", instance_id="synthetic-instance")
        runner._wire_adapter_handlers(adapter)
        adapter.gateway_runner = runner
        observe_native_workers(runner)
        observed, sent = [], []
        target = "recipient@example.test" if mode == "email" else "+15555550101"

        async def model(event, source, session_key, run_generation):
            observed.append(event)
            assert "NOT delivered" in event.text
            assert event.metadata["inkbox_prepared"]
            assert not event.allow_gateway_control
            assert reply_route.get()["inkbox_recovery_target"]["target"] == target
            state = runner._session_state(session_key)
            state.turn.event = event
            context = TurnContext(source=source, session_key=session_key, session_id=session_key,
                                  run_generation=run_generation, inbound_message_id=event.message_id)
            worker = runner._run_agent_start_turn_worker(context, lambda: "Corrected reply")
            response = await asyncio.shield(worker.executor_task)
            execution_result.get()["result"] = {"final_response": response, "messages": []}
            return response

        async def send(chat_id, content, **kwargs):
            sent.append((chat_id, content, deepcopy(reply_route.get())))
            return SendResult(success=True, message_id="synthetic-followup")

        monkeypatch.setattr(runner, "_handle_message_with_agent", model)
        monkeypatch.setattr(adapter, "send", send)
        envelope = {"id": "failure-event", "event_type": "text.delivery_failed", "data": {"text_message": {
            "id": "failed-message", "direction": "outbound", "remote_phone_number": target,
            "conversation_id": "sample-conversation", "text": "Undelivered reply", "error_code": "40002",
            "error_detail": "Temporary delivery failure", "delivery_status": "delivery_failed"}}}
        try:
            if mode == "sms":
                assert (await adapter._on_text_lifecycle(envelope)).status == 200
            else:
                await adapter._note_outbound_delivery_failure(mode=mode, chat_id="sample-contact", thread_id=None,
                    conversation_id="sample-conversation", target=target, failed_body="Undelivered reply",
                    error_code="40002", error_detail="Temporary delivery failure", stage="delivery_failed")
            for _ in range(300):
                if (sent or not authorized) and not adapter._background_tasks:
                    break
                await asyncio.sleep(.01)
            if not authorized:
                assert observed == sent == []
                return
            assert len(observed) == len(sent) == 1
            assert sent[0][:2] == ("sample-contact", "Corrected reply")
            assert sent[0][2]["mode"] == mode
            assert sent[0][2]["inkbox_recovery_target"]["target"] == target
            assert not adapter._inkbox_receipt_tasks
            if mode == "sms":
                assert (await adapter._on_text_lifecycle(envelope)).status == 200
                assert len(observed) == len(sent) == 1
        finally:
            tasks = list(adapter._background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            from gateway.session_runtime_workers import mutation_tasks
            await asyncio.gather(*mutation_tasks(authority))
            authority.db.close()
    asyncio.run(run())
