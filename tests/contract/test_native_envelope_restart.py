"""Fresh authority/adapter recovery of durable Inkbox producer admissions."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("gateway.platforms.webhook_ingress")

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.run_runtime import recover_gateway_native_sessions
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from gateway.turn_context import TurnContext
from hermes_state_runtime import claim_session_input, get_session_admission
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.conversation import reply_route
from inkbox_plugin.native_envelope import uninstall
from inkbox_plugin.native_turns import NativeTurns


@pytest.mark.parametrize("scenario", ["queued", "unknown", "uncertain_send", "other_uncertain_send"])
def test_fresh_runtime_recovers_only_proven_queued_input(tmp_path, monkeypatch, scenario):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # This fixture exercises native recovery, not the unrelated hosted HTTP transport.
        from gateway import session_hosted_service
        monkeypatch.setattr(session_hosted_service, "ensure_hosted_service", AsyncMock())
        monkeypatch.setenv("INKBOX_ALLOW_ALL_USERS", "true")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        executed, sent, completed = [], [], []

        async def make(instance):
            adapter = InkboxAdapter(PlatformConfig(enabled=True, typing_indicator=False, extra={"identity": "sample-agent"}))
            adapter._identity_id, adapter._base_url = "sample-identity", "https://example.test"
            adapter._resolve_contact_full = AsyncMock(return_value=None)
            runner = GatewayRunner(GatewayConfig())
            runner.adapters = {adapter.platform: adapter}
            authority = await initialize_session_authority(runner, profile_id="default", instance_id=instance)
            runner._wire_adapter_handlers(adapter)
            adapter.gateway_runner = runner
            adapter._native_turns = queue = NativeTurns(adapter, tmp_path / "native")
            queue.quiet_seconds, queue.max_burst_seconds, queue.completion_timeout = .001, .002, 3
            on_complete = adapter.on_processing_complete
            async def finish(event, outcome):
                completed.append((instance, event.message_id, str(getattr(outcome, "value", outcome))))
                await on_complete(event, outcome)
            adapter.on_processing_complete = finish
            async def send(chat_id, content, **kwargs):
                assert reply_route.get()["conversation_id"] == "sample-conversation"
                sent.append((instance, chat_id, content))
                return SendResult(success=True, message_id="synthetic-reply")
            adapter.send = send
            async def model(event, source, session_key, run_generation):
                assert instance == "second"
                assert reply_route.get()["conversation_id"] == "sample-conversation"
                executed.append(event.message_id)
                state = runner._session_state(session_key)
                state.turn.event = event
                agent = SimpleNamespace(_gateway_turn_process_task_id="synthetic-worker",
                    _gateway_turn_process_baseline=frozenset(), interrupt=lambda *_args, **_kw: None)
                state.turn.agent = agent
                context = TurnContext(source=source, session_key=session_key,
                    session_id=runner.session_store.get_or_create_session(source).session_id,
                    run_generation=run_generation, inbound_message_id=event.message_id, agent_holder=[agent])
                worker = runner._run_agent_start_turn_worker(context, lambda: "Recovered answer")
                answer = await asyncio.shield(worker.executor_task)
                execution_result.get()["result"] = {"final_response": answer, "messages": []}
                return answer
            runner._handle_message_with_agent = model
            await queue.start()
            return runner, authority, adapter, queue

        async def close(runner, authority, adapter, queue):
            await queue.close()
            tasks = list(getattr(adapter, "_inkbox_receipt_tasks", {}).values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            from gateway.session_runtime_workers import mutation_tasks
            await asyncio.gather(*mutation_tasks(authority))
            uninstall(adapter, runner)
            authority.db.close()

        first = await make("first")
        _runner, authority, adapter, queue = first
        monkeypatch.setattr(authority, "_schedule", lambda *_args: None)
        source = adapter.build_source(chat_id="sample-chat", user_id="sample-sender", chat_type="dm")
        event = MessageEvent(text="Original question", source=source, message_id="original-event",
            metadata={"inkbox_prepared": True, "inkbox_reply_route": {
                "mode": "whatsapp", "chat_id": source.chat_id, "message_id": "original-event",
                "conversation_id": "sample-conversation", "author": "sample-sender", "raw_text": "Original question",
                "identity_id": adapter._identity_id, "base_url": adapter._base_url}})
        try:
            await queue.accept(event)
            for _ in range(300):
                rows = list(queue.rows.values())
                if rows and rows[0]["turns"] and rows[0]["turns"][0].get("native_admission", {}).get("receipt"):
                    break
                await asyncio.sleep(.01)
            row = next(iter(queue.rows.values()))
            turn = row["turns"][0]
            receipt = turn["native_admission"]["receipt"]
            admission_id = receipt["id"]
            assert get_session_admission(authority.db, admission_id=admission_id)["status"] == "queued"
            if scenario == "unknown":
                claimed = claim_session_input(authority.db, epoch=authority.epoch, session_id=receipt["session_id"])
                assert claimed["status"] == "started"
            elif scenario == "other_uncertain_send":
                prior = copy.deepcopy(turn)
                prior.update(id="older-event", state="uncertain", explicit_delivery_state="uncertain")
                prior.pop("native_admission", None)
                row["turns"].insert(0, prior)
                queue._save(row)
            elif scenario == "uncertain_send":
                turn["explicit_delivery_state"] = "uncertain"
                queue._save(row)
            assert not executed and not sent
        finally:
            await close(*first)

        second = await make("second")
        runner2, authority2, _adapter2, queue2 = second
        try:
            assert not getattr(authority2, "producer_turns", {})
            await recover_gateway_native_sessions(runner2)
            for _ in range(300):
                ledger = get_session_admission(authority2.db, admission_id=admission_id)
                if scenario == "unknown" or (scenario == "queued" and any(item[0] == "second" for item in completed)) or (scenario in {"uncertain_send", "other_uncertain_send"} and ledger["status"] == "terminal"):
                    break
                await asyncio.sleep(.01)
            await asyncio.sleep(.05)
            saved_row = next(iter(queue2.rows.values()))
            saved = next(turn for turn in saved_row["turns"] if turn["id"] == "original-event")
            if scenario == "queued":
                assert executed == ["original-event"]
                assert sent == [("second", "sample-chat", "Recovered answer")]
                assert [item for item in completed if item[0] == "second"] == [("second", "original-event", "success")]
                assert saved["state"] == "done" and saved.get("worker_completion")
                await recover_gateway_native_sessions(runner2)
                await asyncio.sleep(.05)
                assert len(sent) == 1 and len(executed) == 1
                assert len([item for item in completed if item[0] == "second"]) == 1
            else:
                assert not executed and not sent
                assert saved["state"] in {"uncertain", "cancelled", "quarantined"}
                if scenario == "other_uncertain_send":
                    assert saved_row.get("blocked")
                    assert saved_row["turns"][0]["explicit_delivery_state"] == "uncertain"
                if scenario == "unknown":
                    assert ledger["status"] == "unknown"
        finally:
            await close(*second)
    asyncio.run(run())
