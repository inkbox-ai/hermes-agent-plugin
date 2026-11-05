"""Hosted completion context survives official durable admission, not just direct calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("gateway.platforms.webhook_ingress")
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from gateway.turn_context import TurnContext
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.host_fencing import observe_native_workers


@pytest.mark.parametrize("sms_required", [False, True])
def test_hosted_completion_retains_call_identity_through_real_authority(tmp_path, monkeypatch, sms_required):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("INKBOX_ALLOWED_USERS", "contact-voice")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allowed_users_env="INKBOX_ALLOWED_USERS", allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        adapter = InkboxAdapter(PlatformConfig(enabled=True, typing_indicator=False,
            extra={"identity": "sample-agent", "require_signature": False}))
        adapter._base_url = "https://example.test"
        adapter._identity_id = "00000000-0000-0000-0000-000000000001"
        adapter._inkbox = SimpleNamespace(get_identity=lambda _: SimpleNamespace(list_transcripts=lambda _: [
            SimpleNamespace(party="local", text="After we hang up, send me one SMS containing release-ready.")
        ] if sms_required else []))
        adapter._resolve_channel_overrides = lambda *_: (None, None)
        adapter._resolve_contact_full = AsyncMock(return_value=None)
        runner = GatewayRunner(GatewayConfig())
        runner.adapters = {adapter.platform: adapter}
        authority = await initialize_session_authority(runner, profile_id="default", instance_id="sample-instance")
        runner._wire_adapter_handlers(adapter)
        adapter.gateway_runner = runner
        observe_native_workers(runner)
        observed = []
        async def model(event, source, session_key, run_generation):
            observed.append(adapter._hosted_call_processing_data(event))
            if sms_required:
                assert event.raw_message["_inkbox_hosted_sms_required"] is True
                assert "release-ready" in " ".join(event.raw_message["_inkbox_hosted_sms_context"])
                if len(observed) == 2:
                    assert "[hosted_post_call_sms_correction]" in event.text
                    assert event.raw_message["_inkbox_hosted_reconciliation_attempt"] == 2
            state = runner._session_state(session_key)
            state.turn.event = event
            context = TurnContext(source=source, session_key=session_key, session_id=session_key,
                                  run_generation=run_generation, inbound_message_id=event.message_id)
            worker = runner._run_agent_start_turn_worker(context, lambda: "[SILENT]")
            answer = await asyncio.shield(worker.executor_task)
            execution_result.get()["result"] = {"final_response": answer, "messages": []}
            return answer
        monkeypatch.setattr(runner, "_handle_message_with_agent", model)
        envelope = {"id": "voice-event", "event_type": "call.ended", "data": {
            "call": {"id": "voice-call", "mode": "hosted_agent", "direction": "outbound",
                     "status": "completed", "remote_phone_number": "+15555550101"},
            "contacts": [{"id": "contact-voice", "name": "Synthetic caller"}],
            "outcome": "completed", "post_call_action_items": [],
            "transcript": {"entries": [{"party": "remote", "text": "After we hang up, send me one SMS containing release-ready." if sms_required else "Thank you"}]}}}
        try:
            response = await adapter._on_call_ended(envelope)
            assert response.status == 200
            for _ in range(300):
                if observed and not adapter._background_tasks and not adapter._session_tasks:
                    break
                await asyncio.sleep(.01)
            assert observed == [("voice-call", "voice-event")] * (2 if sms_required else 1)
            receipt = adapter._read_hosted_call_registry()["voice-call"]
            assert receipt["state"] == ("failed" if sms_required else "completed")
            if sms_required:
                assert receipt["retryable"] is False
            await adapter._on_call_ended(envelope)
            assert observed == [("voice-call", "voice-event")] * (2 if sms_required else 1)
        finally:
            for task in list(adapter._background_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
            from gateway.session_runtime_workers import mutation_tasks
            await asyncio.gather(*mutation_tasks(authority))
            authority.db.close()
    asyncio.run(run())


@pytest.mark.parametrize("uncertain", [None, "prior_delivery", "same_explicit", "revoked"])
def test_companion_initialization_retains_private_context_through_authority(tmp_path, monkeypatch, uncertain):
    from unittest.mock import Mock
    from uuid import UUID

    from inkbox_plugin.companion import CompanionReceiver

    def uid(n):
        return str(UUID(int=n))

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("INKBOX_ALLOWED_USERS", "owner@example.test")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allowed_users_env="INKBOX_ALLOWED_USERS", allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        adapter = InkboxAdapter(PlatformConfig(enabled=True, typing_indicator=False,
            extra={"identity": "sample-agent", "require_signature": False}))
        adapter._base_url = "https://example.test"
        adapter._identity_id = uid(100)
        meta = {"channel": "mail", "phase": "initialization", "scope_id": uid(10),
                "activation_id": uid(20), "conversation_id": uid(30), "sequence": 3}
        envelope = {"event_type": "message.received", "companion": meta, "data": {"message": {
            "id": uid(3), "direction": "inbound", "sender_access": "direct", "from_address": "owner@example.test",
            "thread_id": uid(30), "body_text": "Hello group", "created_at": "2026-01-01T00:00:00Z"}}}
        context = {"channel": "mail", "conversation_id": uid(30), "reply_to_message_id": uid(3),
                   "to": ["owner@example.test"], "cc": []}
        snapshot = {key: meta[key] for key in ("scope_id", "activation_id", "conversation_id", "channel")}
        snapshot.update(entries=[{"id": uid(3), "author": "owner@example.test", "text": "Hello group",
            "occurred_at": "2026-01-01T00:00:00Z", "historical": False, "is_trigger": True,
            "attachments": []}], text="Hello group", reply_context=context, notices=[])
        identity = SimpleNamespace(reply_all_email=Mock(return_value=SimpleNamespace(id=uid(80))))
        adapter._inkbox = SimpleNamespace(get_identity=lambda _: identity,
            companion=SimpleNamespace(load_initialization=Mock(return_value=snapshot),
                                      activation_messages=Mock(return_value=snapshot)))
        adapter._resolve_channel_overrides = lambda *_: (None, None)
        adapter._resolve_contact_full = AsyncMock(return_value=None)
        runner = GatewayRunner(GatewayConfig())
        runner.adapters = {adapter.platform: adapter}
        authority = await initialize_session_authority(runner, profile_id="default", instance_id="sample-instance")
        runner._wire_adapter_handlers(adapter)
        adapter.gateway_runner = runner
        observe_native_workers(runner)
        receiver = adapter._companion = CompanionReceiver(adapter, tmp_path / "companion", 128_000)
        receiver.completion_timeout = 2
        recovery_checked = []
        if uncertain:
            from copy import deepcopy

            from inkbox_plugin import native_envelope
            bind = native_envelope.bind_execution
            def recovered_bind(current, event):
                # At recovered claim time no physical owner remains, but an
                # external delivery is still unresolved on this or another turn.
                row = next(iter(receiver.rows.values()))
                active = receiver.active.pop(str(event.source.chat_id))
                if uncertain == "prior_delivery":
                    prior = deepcopy(active)
                    prior.update(id="prior-unrelated-turn", state="completed", result_ready=True,
                                 delivery={"state": "uncertain", "body": "Prior unanswered receipt"})
                    prior.pop("native_admission", None)
                    row["turns"].insert(0, prior)
                elif uncertain == "same_explicit":
                    active["explicit_delivery_state"] = "uncertain"
                else:
                    row["state"] = "revoked"
                receiver._save(row)
                try:
                    return bind(current, event)
                except RuntimeError as exc:
                    recovery_checked.append(str(exc))
                    raise
            monkeypatch.setattr(native_envelope, "bind_execution", recovered_bind)
        observed = []
        async def model(event, source, session_key, run_generation):
            observed.append((event.metadata.get("companion"), event.raw_message))
            state = runner._session_state(session_key)
            state.turn.event = event
            context = TurnContext(source=source, session_key=session_key, session_id=session_key,
                                  run_generation=run_generation, inbound_message_id=event.message_id)
            worker = runner._run_agent_start_turn_worker(context, lambda: "Synthetic group answer")
            answer = await asyncio.shield(worker.executor_task)
            execution_result.get()["result"] = {"final_response": answer, "messages": []}
            return answer
        monkeypatch.setattr(runner, "_handle_message_with_agent", model)
        try:
            await receiver.start()
            await receiver.accept(envelope)
            for _ in range(300):
                if (observed or recovery_checked) and not receiver.tasks and not adapter._session_tasks:
                    break
                await asyncio.sleep(.01)
            if uncertain:
                expected_error = {
                    "prior_delivery": "Companion replay conflicts with another unresolved turn",
                    "same_explicit": "Companion execution ownership is unresolved",
                    "revoked": "Companion execution is not active",
                }[uncertain]
                assert recovery_checked == [expected_error]
                assert observed == []
                identity.reply_all_email.assert_not_called()
                row = next(iter(receiver.rows.values()))
                if uncertain == "prior_delivery":
                    prior = next(turn for turn in row["turns"] if turn["id"] == "prior-unrelated-turn")
                    assert prior["delivery"] == {"state": "uncertain", "body": "Prior unanswered receipt"}
                elif uncertain == "same_explicit":
                    assert row["turns"][0]["explicit_delivery_state"] == "uncertain"
                else:
                    assert row["state"] in {"paused", "revoked"}
                assert not receiver.active
            else:
                assert len(observed) == 1
                assert observed[0][0]["reply_context"] == context
                assert observed[0][1]["_inkbox_companion_turn"]
                identity.reply_all_email.assert_called_once_with(uid(3), body_text="Synthetic group answer")
        finally:
            await receiver.close()
            from gateway.session_runtime_workers import mutation_tasks
            await asyncio.gather(*mutation_tasks(authority))
            authority.db.close()
    asyncio.run(run())


def test_plain_voice_turn_keeps_official_authority_path(tmp_path, monkeypatch):
    from gateway.platforms.base import MessageEvent, SendResult

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("INKBOX_ALLOWED_USERS", "voice-contact")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allowed_users_env="INKBOX_ALLOWED_USERS", allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        adapter = InkboxAdapter(PlatformConfig(enabled=True, typing_indicator=False,
            extra={"identity": "sample-agent", "require_signature": False}))
        adapter._base_url = "https://example.test"
        adapter._identity_id = "00000000-0000-0000-0000-000000000001"
        runner = GatewayRunner(GatewayConfig())
        runner.adapters = {adapter.platform: adapter}
        authority = await initialize_session_authority(runner, profile_id="default", instance_id="sample-instance")
        runner._wire_adapter_handlers(adapter)
        adapter.gateway_runner = runner
        observe_native_workers(runner)
        observed = []
        async def model(event, source, session_key, run_generation):
            observed.append(event)
            assert event.source.chat_topic == "voice_call"
            assert not event.metadata and not event.internal
            state = runner._session_state(session_key)
            state.turn.event = event
            context = TurnContext(source=source, session_key=session_key, session_id=session_key,
                                  run_generation=run_generation, inbound_message_id=event.message_id)
            worker = runner._run_agent_start_turn_worker(context, lambda: "Synthetic voice answer")
            answer = await asyncio.shield(worker.executor_task)
            execution_result.get()["result"] = {"final_response": answer, "messages": []}
            return answer
        monkeypatch.setattr(runner, "_handle_message_with_agent", model)
        delivered = AsyncMock(return_value=SendResult(success=True, message_id="voice-reply"))
        monkeypatch.setattr(adapter, "send", delivered)
        source = adapter.build_source(chat_id="voice-contact", chat_type="dm", user_id="voice-contact",
            chat_topic="voice_call", thread_id="call:plain-call", message_id="plain-voice-turn")
        event = MessageEvent(text="Synthetic voice question", source=source, message_id="plain-voice-turn",
            raw_message={"event": "realtime_call_ended", "transcript": []})
        try:
            await (await adapter._enqueue(event))
            for _ in range(300):
                if observed and delivered.await_count:
                    break
                await asyncio.sleep(.01)
            assert len(observed) == 1
            delivered.assert_awaited_once()
            assert delivered.await_args.kwargs["chat_id"] == "voice-contact"
            assert delivered.await_args.kwargs["content"] == "Synthetic voice answer"
        finally:
            from gateway.session_runtime_workers import mutation_tasks
            await asyncio.gather(*mutation_tasks(authority))
            authority.db.close()
    asyncio.run(run())
