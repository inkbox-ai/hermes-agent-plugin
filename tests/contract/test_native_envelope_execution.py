"""Journal-bound replay through the real authority, handler, and delivery path."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("gateway.platforms.webhook_ingress")

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from gateway.turn_context import TurnContext
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.conversation import reply_route
from inkbox_plugin.host_fencing import completed_worker_proof, observe_native_workers
from inkbox_plugin.native_turns import NativeTurns
from inkbox_plugin.native_envelope import route_for


@pytest.mark.parametrize("mode,scenario", [(mode, "normal") for mode in ("email", "sms", "whatsapp", "imessage", "slack")] + [("whatsapp", "stop"), ("email", "recovered"), ("email", "recovered_failure"), ("sms", "group_interrupt"), ("whatsapp", "preworker"), ("whatsapp", "producer_unicode"), ("email", "long_email"), ("whatsapp", "preadmission"), ("whatsapp", "outgoing_media"), ("sms", "remote_media"), ("email", "recovered_skill"), ("email", "recovered_later_guard"), ("email", "shared_busy"), ("email", "shared_busy_failure"), ("email", "shared_busy_empty"), ("email", "shared_busy_cancel")] + [(mode, case) for mode in ("email", "whatsapp") for case in ("late_terminal", "late_inflight", "late_empty", "late_delivery")] + [("email", "shared_busy_late_terminal"), ("email", "shared_busy_late_inflight")])
def test_real_authority_restores_owned_event_and_delivers_once(tmp_path, monkeypatch, mode, scenario):
    is_busy = scenario.startswith("shared_busy")
    is_late = "late_" in scenario
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("INKBOX_ALLOWED_USERS", "sample-sender,+15555550101,whatsapp:sample-conversation")
        monkeypatch.setenv("INKBOX_ALLOW_ALL_USERS", "false")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allowed_users_env="INKBOX_ALLOWED_USERS", allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        adapter = InkboxAdapter(PlatformConfig(enabled=True, typing_indicator=False,
            extra={"identity": "sample-agent", "require_signature": False}))
        adapter._slack_enabled = True
        adapter._imessage_threaded_replies = True
        adapter._base_url = "https://example.test"
        adapter._identity_id = "00000000-0000-0000-0000-000000000001"
        runner = GatewayRunner(GatewayConfig())
        runner.adapters = {adapter.platform: adapter}
        authority = await initialize_session_authority(runner, profile_id="default", instance_id="sample-instance")
        runner._wire_adapter_handlers(adapter)
        adapter.gateway_runner = runner
        adapter._resolve_contact_full = AsyncMock(return_value=None)
        sent, executed, media_sent = [], [], []
        returned_deliveries = []
        completions, shared_busy_calls = [], []
        if is_busy:
            from gateway import session_ingress
            dispatch_busy = session_ingress.dispatch_shared_busy
            async def observed_busy(*args, **kwargs):
                shared_busy_calls.append(args[1].message_id)
                return await dispatch_busy(*args, **kwargs)
            monkeypatch.setattr(session_ingress, "dispatch_shared_busy", observed_busy)
        completed = adapter.on_processing_complete
        async def observed_complete(event, outcome):
            if is_late and event.message_id == ("next-event" if is_busy else "original-event") and scenario != "late_empty":
                assert event.message_id in returned_deliveries, "Completion cannot precede actual delivery"
            completions.append((event.message_id, str(getattr(outcome, "value", outcome)).lower()))
            await completed(event, outcome)
        monkeypatch.setattr(adapter, "on_processing_complete", observed_complete)
        picture = tmp_path / "sample.png"
        if scenario == "recovered_skill":
            skill = tmp_path / "skills" / "sample-contract" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: sample-contract\ndescription: Synthetic contract fixture\n---\nUse the synthetic fixture instruction.\n")
        if scenario == "outgoing_media":
            from PIL import Image
            Image.new("RGB", (2, 2)).save(picture)
        if scenario == "preadmission":
            from inkbox_plugin import native_envelope
            monkeypatch.setattr(native_envelope, "bind_execution", lambda *args: (_ for _ in ()).throw(RuntimeError("Synthetic refusal")))
        delivery_started = asyncio.Event()
        delivery_release = asyncio.Event()
        started = asyncio.Event()
        release = threading.Event()
        inflight_release = threading.Event()
        if is_late:
            from hermes_state_runtime import get_session_admission
            admit = authority.admit_native
            async def delayed_receipt(event):
                receipt = await admit(event)
                target = "next-event" if is_busy else "original-event"
                if event.message_id != target:
                    return receipt
                if is_busy:
                    release.set()
                if scenario == "late_delivery":
                    await asyncio.wait_for(delivery_started.wait(), 5)
                    asyncio.get_running_loop().call_later(.1, delivery_release.set)
                    return receipt
                if scenario.endswith("inflight"):
                    for _ in range(500):
                        if any(item.message_id == target for item in executed):
                            asyncio.get_running_loop().call_later(.05, inflight_release.set)
                            return receipt
                        await asyncio.sleep(.01)
                else:
                    for _ in range(500):
                        row = get_session_admission(authority.db, admission_id=receipt.admission_id)
                        if row["status"] == "terminal" and any(item[0] == target for item in completions):
                            return receipt
                        await asyncio.sleep(.01)
                raise AssertionError("Late waiter interleaving did not reach its boundary")
            monkeypatch.setattr(authority, "admit_native", delayed_receipt)
        async def send(chat_id, content, reply_to=None, metadata=None, **kwargs):
            sent.append((chat_id, content, reply_route.get()))
            if scenario == "late_delivery":
                delivery_started.set()
                await asyncio.wait_for(delivery_release.wait(), 5)
                assert completions == [], "Receipt-only return cannot complete an in-flight delivery"
            if scenario.startswith("recovered"):
                assert adapter._conversation_state().row(chat_id)["quiet"]
            busy_delivery = is_busy and (reply_route.get() or {}).get("message_id") == "next-event"
            if busy_delivery:
                assert adapter._conversation_state().row(chat_id)["quiet"][0]["turn"] == "next-event"
                if scenario == "shared_busy_cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
            failed = scenario == "recovered_failure" or (busy_delivery and scenario == "shared_busy_failure")
            returned_deliveries.append((reply_route.get() or {}).get("message_id"))
            return SendResult(success=not failed, message_id="synthetic-outbound", retryable=False,
                raw_response={"inkbox_no_retry": True} if failed else None)
        monkeypatch.setattr(adapter, "send", send)
        async def send_image(chat_id, image_path, **kwargs):
            owned = queue.media_owner(chat_id, kwargs.get("metadata"))
            assert owned and owned[1]["id"] == "original-event"
            media_sent.append((str(image_path), copy.deepcopy(reply_route.get())))
            return SendResult(success=True, message_id="synthetic-image")
        monkeypatch.setattr(adapter, "send_image_file", send_image)
        observe_native_workers(runner)
        queue = None
        if mode in {"whatsapp", "imessage", "slack"}:
            adapter._native_turns = queue = NativeTurns(adapter, tmp_path / "native")
            queue.quiet_seconds = .01
            queue.max_burst_seconds = .02
            queue.completion_timeout = 3
            await queue.start()
        async def model_turn(event, source, session_key, run_generation):
            # Only the model seam is synthetic. Admission, restore, actual
            # _handle_message, worker creation and adapter delivery are real.
            executed.append(event)
            if scenario == "recovered_skill":
                before = event.text
                runner._hmwa_auto_load_skills(event, "sample-contract", session_key, session_key)
                assert event.text != before and event.text.endswith(before)
                assert "synthetic fixture instruction" in event.text
            if scenario == "recovered_later_guard":
                assert adapter._active_sessions[session_key]._inkbox_event is later_event
            assert reply_route.get()["mode"] == mode
            if mode == "email":
                assert reply_route.get()["stored_message_id"] == ("next-message" if is_busy and event.message_id == "next-event" else "original-message")
            if scenario == "producer_unicode":
                assert reply_route.get()["raw_text"] == "😀" * 4000
                assert "😀" * 4000 in event.text
                assert event._inkbox_producer_execution is True
            if scenario == "long_email":
                assert event.text == "Long email body. " * 10000
                assert event._inkbox_producer_execution is True
            if scenario == "remote_media":
                assert event.media_urls == []
                assert event.text.endswith("[The user sent an attachment (image/png) that could not be retrieved.]")
                assert event.text.startswith("Synthetic question")
            if scenario == "preworker":
                raise RuntimeError("Synthetic initialization failure")
            state = runner._session_state(session_key)
            state.turn.event = event
            agent = NS(_gateway_turn_process_task_id="sample-worker", _gateway_turn_process_baseline=frozenset(),
                interrupt=lambda *args, **kwargs: release.set())
            state.turn.agent = agent
            if not scenario.startswith("recovered") and not is_late and not (is_busy and event.message_id == "next-event"):
                assert event._inkbox_producer_execution is True
                assert session_key not in adapter._active_sessions
            context = TurnContext(source=source, session_key=session_key,
                session_id=runner.session_store.get_or_create_session(source).session_id,
                run_generation=run_generation, inbound_message_id=event.message_id, agent_holder=[agent])
            def model():
                if scenario == "stop" or (scenario == "group_interrupt" or is_busy) and event.message_id == "original-event":
                    assert release.wait(10)
                if scenario.endswith("inflight") and event.message_id == ("next-event" if is_busy else "original-event"):
                    assert inflight_release.wait(10)
                if (scenario == "late_empty" and event.message_id == "original-event"
                        or scenario == "shared_busy_empty" and event.message_id == "next-event"):
                    return ""
                return "Synthetic answer" + (f"\nMEDIA:{picture}" if scenario == "outgoing_media" else "")
            worker = runner._run_agent_start_turn_worker(context, model)
            started.set()
            answer = await asyncio.shield(worker.executor_task)
            execution_result.get()["result"] = {"final_response": answer, "messages": []}
            return answer
        monkeypatch.setattr(runner, "_handle_message_with_agent", model_turn)
        source = adapter.build_source(chat_id="sample-chat", chat_type="group" if scenario == "group_interrupt" else "dm", user_id="sample-sender")
        original = MessageEvent(text="Synthetic question", source=source, message_id="original-event",
            raw_message={"event_type": "message.received", "data": {"message": {
                "id": "original-message", "from_address": "sender@example.test", "body_text": "Synthetic question"}}})
        if scenario == "long_email":
            original.text = original.raw_message["data"]["message"]["body_text"] = "Long email body. " * 10000
        if mode == "sms":
            original.raw_message = {"event_type": "text.received", "data": {"text_message": {
                "id": "original-message", "sender_phone_number": "+15555550101", "body": "Synthetic question"}}}
        elif queue:
            original.metadata = {"inkbox_prepared": True, "inkbox_reply_route": {
                "mode": mode, "chat_id": source.chat_id, "message_id": original.message_id,
                "conversation_id": "sample-conversation", "author": "sample-sender",
                "raw_text": original.text, "identity_id": adapter._identity_id, "base_url": adapter._base_url,
                "imessage_reply_target": original.message_id, "imessage_event_ids": [original.message_id],
                "imessage_sources": [{"id": original.message_id}]}}
        if scenario == "remote_media":
            from inkbox_plugin.adapter import _extract_text_media
            original.media_urls, original.media_types, _ = _extract_text_media({"media": [
                {"url": "https://example.test/unavailable.png", "content_type": "image/png"}]})
        if mode == "imessage":
            original.metadata["inkbox_buffer_context"] = True
        try:
            if scenario == "producer_unicode":
                from inkbox_plugin.whatsapp import receive
                adapter._contact_memories = lambda *args, **kwargs: []
                result = await receive(adapter, {"id": "sample-envelope", "event_type": "whatsapp.received", "data": {
                    "message": {"id": "original-event", "agent_identity_id": adapter._identity_id,
                        "conversation_id": "sample-conversation", "direction": "inbound", "conversation_kind": "direct",
                        "sender_number": "+15555550101", "remote_number": "+15555550101", "local_number": "+15555550100",
                        "participants": ["+15555550101"], "content": "😀" * 4000, "media": [],
                        "created_at": "2026-10-01T12:00:00Z"}}})
                assert result.status == 202
                row = next(iter(queue.rows.values()))
                original = queue._event_from_turn(row, row["turns"][0])
            elif queue:
                await queue.accept(original)
            elif scenario.startswith("recovered"):
                from hermes_state_runtime import get_session_admission
                adapter._conversation_state().quiet(source.chat_id, "quiet-event", "Earlier context")
                assert adapter._prepare_conversation_event(original)
                schedule = authority._schedule
                monkeypatch.setattr(authority, "_schedule", lambda ref: None)
                await adapter.handle_message(original)
                record = adapter._conversation_state().row(source.chat_id)["native_admissions"][original.message_id]
                receipt = authority._receipt(get_session_admission(authority.db, admission_id=record["receipt"]["id"]))
                authority.producer_turns.clear()  # Simulate lost process-local producer bracket.
                if scenario == "recovered_later_guard":
                    later_event = copy.deepcopy(original)
                    later_event.message_id = "later-event"
                    later_event.metadata = {"inkbox_prepared": True, "inkbox_reply_route": {
                        **route_for(adapter, original), "message_id": "later-event"}}
                    guard = asyncio.Event()
                    adapter._active_sessions[adapter._event_session_key(original)] = guard
                    await adapter.on_processing_start(later_event)
                    assert guard._inkbox_event is later_event
                monkeypatch.setattr(authority, "_schedule", schedule)
                schedule(receipt.ref)
            else:
                task = await adapter._enqueue(original)
                if scenario != "group_interrupt" and not is_busy:
                    await task
            expected_route = route_for(adapter, original)
            if is_busy:
                await asyncio.wait_for(started.wait(), 3)
                journal = adapter._conversation_state()
                journal.quiet(source.chat_id, "quiet-between", "Context between two messages")
                incoming = MessageEvent(text="Next question", source=copy.deepcopy(source), message_id="next-event",
                    raw_message={"event_type": "message.received", "data": {"message": {
                        "id": "next-message", "from_address": "sender@example.test", "body_text": "Next question"}}})
                followup = await adapter._enqueue(incoming)
                await asyncio.wait_for(followup, 3)
                assert shared_busy_calls == []  # Producer FIFO owns both receipts, not Base busy delivery.
                if not is_late:
                    assert journal.row(source.chat_id)["quiet"][0]["turn"] == "next-event"
                release.set()
                await asyncio.gather(task, followup)
                for _ in range(500):
                    if not adapter._background_tasks and not adapter._session_tasks:
                        break
                    await asyncio.sleep(.01)
                assert len(executed) == 2
                expected_sends = [(source.chat_id, "Synthetic answer", expected_route)]
                if scenario != "shared_busy_empty":
                    expected_sends.append((source.chat_id, "Synthetic answer", route_for(adapter, incoming)))
                assert sent == expected_sends
                outcomes = [outcome for message_id, outcome in completions if message_id == "next-event"]
                assert len(outcomes) == 1, completions
                if scenario in {"shared_busy_failure", "shared_busy_cancel"}:
                    assert outcomes[0] in {"failure", "cancelled"}
                    assert len(journal.row(source.chat_id)["quiet"]) == 1
                    assert "turn" not in journal.row(source.chat_id)["quiet"][0]
                else:
                    assert outcomes == ["success"]
                    assert not journal.row(source.chat_id)["quiet"]
                assert not adapter._post_delivery_callbacks
                return
            if scenario == "group_interrupt":
                await asyncio.wait_for(started.wait(), 3)
                incoming = MessageEvent(text="Next question", source=copy.deepcopy(source), message_id="next-event",
                    raw_message={"event_type": "text.received", "data": {"text_message": {
                        "id": "next-message", "sender_phone_number": "+15555550101", "body": "Next question"}}})
                successor = await adapter._enqueue(incoming)
                await asyncio.wait_for(successor, 3)
                await asyncio.gather(task, return_exceptions=True)
                expected_route = route_for(adapter, incoming)
            if scenario == "stop":
                await asyncio.wait_for(started.wait(), 3)
                control = copy.deepcopy(original)
                control.message_id = "stop-event"
                control.text = "/stop"
                control.metadata["inkbox_reply_route"].update(message_id="stop-event", raw_text="/stop")
                await queue.accept(control)
            for _ in range(500):
                if (not is_late or completions) and (sent or scenario in {"stop", "preworker", "preadmission", "late_empty"}) and not adapter._session_tasks and (queue is None or not queue.tasks):
                    break
                await asyncio.sleep(.01)
            if scenario == "preadmission":
                assert not executed and not sent
                saved = next(iter(queue.rows.values()))["turns"][0]
                assert saved["state"] == "uncertain" and "answer" not in saved
                assert not saved.get("worker_completion")
                assert "inkbox_reply_route" in original.metadata
                return
            assert len(executed) == (2 if scenario == "group_interrupt" else 1), (queue.rows if queue else {
                "sent": sent, "accepted": getattr(original, "_gateway_accepted", None),
                "handler_tasks": len(adapter._session_tasks), "authority_sessions": str(authority.sessions),
                "source": original.source.to_dict(), "metadata": original.metadata})
            actual = executed[0]
            assert actual is not original and actual.raw_message is None
            if queue is None and not scenario.startswith("recovered") and not is_late:
                assert original._inkbox_execution_event is actual
            elif queue and scenario == "preworker":
                saved = next(iter(queue.rows.values()))["turns"][0]
                assert saved["state"] == "uncertain", saved
                assert not saved.get("worker_completion") and not saved.get("fenced")
            elif queue:
                saved = next(iter(queue.rows.values()))["turns"][0]
                assert saved["state"] == ("cancelled" if scenario == "stop" else "done") and saved["fenced"]
                assert saved["stop_worker_completion" if scenario == "stop" else "worker_completion"]["source_id"] == original.message_id
                assert callable(actual._inkbox_worker_exited)
            if scenario in {"stop", "preworker", "preadmission"}:
                assert not any(content == "Synthetic answer" for _, content, _ in sent)
            else:
                assert completed_worker_proof(runner, executed[-1]) is not None
                assert sent == ([] if scenario == "late_empty" else [(original.source.chat_id, "Synthetic answer", expected_route)])
                if scenario == "group_interrupt":
                    assert actual.metadata["inkbox_interrupted"] is True
            if is_late:
                assert completions == [("original-event", "success")], completions
                assert not adapter._post_delivery_callbacks
                if queue:
                    if scenario == "late_empty":
                        assert "answer" not in saved  # No send/extraction, and no fabricated answer.
                    else:
                        assert saved["answer"] == "Synthetic answer"
            if scenario == "outgoing_media":
                assert media_sent == [(str(picture), expected_route)]
                assert str(picture) in json.dumps(saved["rendered"])
            if scenario == "recovered_later_guard":
                assert adapter._active_sessions[adapter._event_session_key(original)]._inkbox_event is later_event
            if scenario.startswith("recovered"):
                journal_row = adapter._conversation_state().row(source.chat_id)
                for _ in range(100):
                    if original.message_id in journal_row.get("completed_routes", []):
                        break
                    await asyncio.sleep(.01)
                assert original.message_id in journal_row["completed_routes"]
                if scenario == "recovered_failure":
                    assert len(journal_row["quiet"]) == 1 and "turn" not in journal_row["quiet"][0]
                else:
                    assert not journal_row["quiet"]
            if queue:
                sent_before = list(sent)
                await queue.close()
                adapter._native_turns = queue = NativeTurns(adapter, tmp_path / "native")
                await queue.start()
                await asyncio.sleep(.02)
                assert len(executed) == 1 and sent == sent_before
                saved = next(iter(queue.rows.values()))["turns"][0]
                assert saved["state"] == ({"stop": "cancelled", "preworker": "uncertain"}.get(scenario, "done"))
                if scenario == "late_empty":
                    successor = copy.deepcopy(original)
                    successor.message_id = "after-empty-event"
                    successor.text = "Another question"
                    successor.metadata["inkbox_reply_route"].update(message_id=successor.message_id, raw_text=successor.text)
                    await queue.accept(successor)
                    for _ in range(500):
                        if len(executed) == 2 and not queue.tasks:
                            break
                        await asyncio.sleep(.01)
                    assert len(executed) == 2 and len(sent) == 1
                    assert completions == [("original-event", "success"), ("after-empty-event", "success")]
                    turns = next(iter(queue.rows.values()))["turns"]
                    assert all(turn["state"] == "done" and turn["fenced"] for turn in turns)
                    assert turns[-1]["answer"] == "Synthetic answer"
        finally:
            release.set()
            inflight_release.set()
            if queue:
                await queue.close()
            from gateway.session_runtime_workers import mutation_tasks
            await asyncio.gather(*mutation_tasks(authority))
            authority.db.close()
    asyncio.run(run())


def test_internal_a2a_uses_original_event_with_active_authority(tmp_path, monkeypatch):
    """Task callbacks retain their existing producer path, not native text replay."""
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("INKBOX_ALLOW_ALL_USERS", "true")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allowed_users_env="INKBOX_ALLOWED_USERS", allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        adapter = InkboxAdapter(PlatformConfig(enabled=True, typing_indicator=False,
            extra={"identity": "sample-agent", "require_signature": False}))
        adapter._base_url = "https://example.test"
        adapter._identity_id = "00000000-0000-0000-0000-000000000001"
        adapter._a2a_progress_interval_seconds = 0
        runner = GatewayRunner(GatewayConfig())
        runner.adapters = {adapter.platform: adapter}
        authority = await initialize_session_authority(runner, profile_id="default", instance_id="sample-instance")
        runner._wire_adapter_handlers(adapter)
        adapter.gateway_runner = runner
        replies, executed = [], []
        task = NS(id="sample-task", context_id="sample-context", state="submitted",
            caller=NS(identity_id="sample-caller", organization_id="sample-org", handle="sample-caller"),
            messages=[NS(role="ROLE_CALLER", message_id="sample-message", parts=[{"text": "Synthetic question"}])])
        class Identity:
            def a2a_task(self, task_id):
                assert task_id == task.id
                return task

            def a2a_reply(self, task_id, **kwargs):
                assert task_id == task.id
                replies.append(kwargs)
                task.state = "working" if kwargs.get("intent") == "progress" else "completed"
                task.messages.append(NS(role="ROLE_AGENT", parts=[{"text": kwargs["text"]}]))
        adapter._inkbox = NS(get_identity=lambda handle: Identity())
        from inkbox_plugin import tools as plugin_tools
        monkeypatch.setattr(plugin_tools, "_client_and_identity", lambda: (None, None, Identity()))
        observe_native_workers(runner)
        async def model_turn(event, source, session_key, run_generation):
            from inkbox_plugin.a2a_context import activate_next_a2a_turn_context
            executed.append(event)
            assert event.internal and event.raw_message["event_type"] == "a2a.task.created"
            assert not getattr(event, "_native_execution_lifecycle", False)
            session_id = runner.session_store.get_or_create_session(source).session_id
            context = activate_next_a2a_turn_context(session_id)
            assert context["task_id"] == task.id and context["message_id"] == "sample-message"
            state = runner._session_state(session_key)
            agent = NS(_gateway_turn_process_task_id="sample-worker", _gateway_turn_process_baseline=frozenset(),
                interrupt=lambda *args, **kwargs: None)
            state.turn.event, state.turn.agent = event, agent
            turn = TurnContext(source=source, session_key=session_key, session_id=session_id,
                run_generation=run_generation, inbound_message_id=event.message_id, agent_holder=[agent])
            def model():
                result = json.loads(plugin_tools.inkbox_a2a_complete({"text": "Synthetic answer"}, task_id=session_id))
                assert result.get("ok") is True, result
                return "Synthetic answer"
            worker = runner._run_agent_start_turn_worker(turn, model)
            return await asyncio.shield(worker.executor_task)
        monkeypatch.setattr(runner, "_handle_message_with_agent", model_turn)
        envelope = {"id": "sample-envelope", "event_type": "a2a.task.created", "data": {
            "task_id": task.id, "context_id": task.context_id, "state": "submitted",
            "caller": {"identity_id": "sample-caller", "organization_id": "sample-org", "handle": "sample-caller"},
            "message_id": "sample-message", "parts": [{"text": "Synthetic question"}]}}
        try:
            response = await adapter._on_a2a_event(envelope)
            assert response.status == 200
            for _ in range(500):
                if executed and not adapter._background_tasks and not adapter._session_tasks:
                    break
                await asyncio.sleep(.01)
            assert len(executed) == 1
            assert any(item.get("text") == "Synthetic answer" for item in replies), replies
            assert adapter._read_a2a_registry()["sample-task:sample-message"]["state"] == "finalized"
            assert (await adapter._on_a2a_event(envelope)).text == "stopped"
            assert len(executed) == 1
        finally:
            await adapter._stop_a2a_progress_updates(task.id)
            from gateway.session_runtime_workers import mutation_tasks
            await asyncio.gather(*mutation_tasks(authority))
            authority.db.close()
    asyncio.run(run())
