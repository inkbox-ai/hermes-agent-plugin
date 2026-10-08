"""Host progress is source-bound status, never an ordinary conversation reply."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.conversation import reply_route
from tests import test_native_turns as native_harness
from tests import test_slack_companion as companion_harness

factory = native_harness.factory
isolated_home = native_harness.isolated_home
slack_host = companion_harness.slack_host


@pytest.fixture
def adapter_case():
    def make(companion=False):
        chat_id = "companion:example" if companion else "slack:example"
        route = {"mode": "slack", "chat_id": chat_id, "message_id": "turn-1", "source_event_id": "source-1",
                 "connection_id": "00000000-0000-4000-8000-000000000001", "conversation_id": "CEXAMPLE",
                 "message_ts": "1234567890.000002", "thread_ts": "1234567890.000001"}
        turn = {"id": "turn-1", "state": "submitted" if companion else "running", "route": route}
        send = AsyncMock(return_value=NS(success=True, message_id="delivered-answer"))
        adapter = object.__new__(InkboxAdapter)
        adapter._active_call_ws = {}
        adapter._last_inbound_modality = {}
        adapter._slack_activity = NS(progress=AsyncMock(return_value="inkbox-slack-progress:example"),
                                     has_chat=lambda chat: chat == chat_id)
        adapter._stop_imessage_typing_for_chat = lambda chat: None
        adapter._conversation_state = lambda: NS(row=lambda chat: {"routes": {}})
        if companion:
            adapter._native_turns = None
            adapter._companion = NS(rows={"example": {"meta": {"channel": "slack"}, "turns": [turn]}},
                                    _slack_route=lambda turn: route, send=send)
        else:
            adapter._companion = None
            adapter._native_turns = NS(rows={"example": {"chat_id": chat_id, "turns": [turn]}}, send=send)
        return NS(adapter=adapter, chat_id=chat_id, route=route, turn=turn, send=send)
    return make


@pytest.mark.parametrize("companion", [False, True])
@pytest.mark.parametrize("body,status", [
    ("🔀 Delegating list worker_1234", "Checking delegated work"),
    ("🔀 Delegating steer worker_1234: inspect the result", "Updating delegated work"),
    ("🔀 Delegating stop worker_1234", "Stopping delegated work"),
    ("🔀 Delegating 2 tasks: Check the result", "Delegating work"),
    ("📄 Reading /tmp/synthetic/report.txt", "Reading information"),
    ("👁 Looking at the image Check chart labels", "Checking the image"),
])
def test_tool_narration_uses_progress_without_recording_reply(adapter_case, companion, body, status):
    async def run():
        case = adapter_case(companion)
        result = await case.adapter.send(case.chat_id, body, reply_to="turn-1")
        assert result.success and result.message_id == "inkbox-slack-progress:example"
        case.adapter._slack_activity.progress.assert_awaited_once_with(
            case.chat_id, case.route, status, message_id=None)
        case.send.assert_not_awaited()
        assert case.turn["state"] == ("submitted" if companion else "running")
    asyncio.run(run())


def test_context_origin_wins_over_unrelated_progress_metadata(adapter_case):
    async def run():
        case = adapter_case()
        token = reply_route.set(case.route)
        try:
            result = await case.adapter.send(case.chat_id, "Checking the result", reply_to="another-turn",
                metadata={"notice_type": "tool_progress", "source_event_id": "another-source", "thread_ts": "999.000001"})
        finally:
            reply_route.reset(token)
        assert result.success
        sent_route = case.adapter._slack_activity.progress.call_args.args[1]
        assert sent_route["source_event_id"] == "source-1"
        assert sent_route["thread_ts"] == "1234567890.000001"
        case.send.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("body", ["Delegating steer opaque_worker_id", '⚙️ delegate_task: {"subagent_id": "opaque_worker_id"}'])
def test_tagged_progress_does_not_publish_delegate_arguments(adapter_case, body):
    async def run():
        case = adapter_case()
        await case.adapter.send(case.chat_id, body, reply_to="turn-1", metadata={"notice_type": "tool_progress"})
        text = case.adapter._slack_activity.progress.call_args.args[2]
        assert "opaque_worker_id" not in text and "subagent_id" not in text
        case.send.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("companion", [False, True])
@pytest.mark.parametrize("body", [
    '⚙️ custom_tool: {"argument": "opaque-value"}',
    '⚙️ custom_tool: {"argument": "opaque-value"}\nResult:\nRaw command output',
    "📨 send_message...",
])
def test_legacy_generic_progress_is_status_without_raw_arguments(adapter_case, companion, body):
    async def run():
        case = adapter_case(companion)
        result = await case.adapter.send(case.chat_id, body, reply_to="turn-1")
        assert result.message_id == "inkbox-slack-progress:example"
        assert case.adapter._slack_activity.progress.call_args.args[2] == "Working on your request"
        case.send.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("companion", [False, True])
@pytest.mark.parametrize("final", [False, True])
def test_approval_and_final_answer_never_become_progress(adapter_case, companion, final):
    async def run():
        case = adapter_case(companion)
        content = "📄 Reading is complete." if final else "⚠️ Approve this action?"
        if final:
            case.turn["result" if companion else "answer"] = content
        result = await case.adapter.send(case.chat_id, content, reply_to="turn-1",
                                        metadata=None if final else {"is_approval_prompt": True})
        assert result.message_id == "delivered-answer"
        case.send.assert_awaited_once()
        case.adapter._slack_activity.progress.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("companion", [False, True])
@pytest.mark.parametrize("body", ["⚙️ custom_tool: completed successfully", "💻 Running is complete."])
@pytest.mark.parametrize("notice", [None, "tool_progress", "admin"])
def test_checkpointed_slack_final_bypasses_only_body_shape_suppression(adapter_case, companion, body, notice):
    async def run():
        case = adapter_case(companion)
        case.turn["result" if companion else "answer"] = body
        result = await case.adapter.send(case.chat_id, body, reply_to="turn-1",
                                        metadata={"notice_type": notice} if notice else None)
        assert result.message_id == ("suppressed-admin-notice" if notice else "delivered-answer")
        assert case.send.await_count == (0 if notice else 1)
        case.adapter._slack_activity.progress.assert_not_awaited()
    asyncio.run(run())


def test_untagged_human_prose_and_multiline_reply_are_not_progress(adapter_case):
    async def run():
        case = adapter_case()
        for text in ("Reading the report now.", "📄 Reading report.txt\nHere are the conclusions."):
            assert (await case.adapter.send(case.chat_id, text, reply_to="turn-1")).message_id == "delivered-answer"
        assert case.send.await_count == 2
        case.adapter._slack_activity.progress.assert_not_awaited()
    asyncio.run(run())


def test_progress_requires_receipt_binding_not_just_active_chat(adapter_case):
    async def run():
        case = adapter_case()
        assert case.adapter.supports_progress_updates(case.chat_id)
        assert await case.adapter._send_slack_progress(case.chat_id, "📄 Reading report.txt") is None
        case.adapter._slack_activity.progress.assert_not_awaited()
        assert not case.adapter.supports_progress_updates("slack:untracked")
        assert not case.adapter.supports_interim_messages(case.chat_id)
    asyncio.run(run())


def test_progress_edit_uses_synthetic_handle_without_sending_answer(adapter_case):
    async def run():
        case = adapter_case()
        handle = "inkbox-slack-progress:example"
        result = await case.adapter.edit_message(case.chat_id, handle, "🔀 Delegating list opaque_worker_id")
        assert result.success
        case.adapter._slack_activity.progress.assert_awaited_once_with(
            case.chat_id, {}, "Checking delegated work", message_id=handle)
        case.send.assert_not_awaited()
        assert not (await case.adapter.edit_message(case.chat_id, "ordinary-message", "Edit text")).success
    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_unavailable_or_stale_progress_never_falls_through_to_reply(adapter_case, failure):
    async def run():
        case = adapter_case()
        case.adapter._slack_activity.progress.return_value = None
        if failure:
            case.adapter._slack_activity.progress.side_effect = RuntimeError("Synthetic failure")
        result = await case.adapter.send(case.chat_id, "🔀 Delegating list opaque_worker_id", reply_to="turn-1")
        assert result.success and result.message_id == "suppressed-slack-progress"
        case.send.assert_not_awaited()
    asyncio.run(run())


def test_native_progress_message_stays_separate_from_checkpointed_answer(factory, tmp_path):
    async def run():
        finish = asyncio.Event()
        value = await native_harness.harness(factory, tmp_path, gate=finish)
        sdk = value.adapter._inkbox.slack
        sdk.send_message.return_value = NS(id=native_harness.uid(70), status="sent", message_ts="1234567890.000010")
        sdk.update_message.return_value = NS(status="succeeded")
        value.adapter._slack_activity._progress.interval = 0
        await value.queue.accept(native_harness.receipt(1, mode="slack"))
        await native_harness.started(value)
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        token = reply_route.set(None)
        try:
            await value.adapter.on_processing_start(value.inputs[0])
            progress = await value.adapter.send(row["chat_id"], "🔀 Delegating list worker_1234")
            assert progress.message_id.startswith("inkbox-slack-progress:")
            await value.adapter._slack_activity.flush()
            assert sdk.send_message.call_args.kwargs["text"] == "Checking delegated work"
            await value.adapter.edit_message(row["chat_id"], progress.message_id, "📄 Reading /tmp/report.txt")
            await value.adapter._slack_activity.flush()
            assert sdk.update_message.call_args.args[3] == "Reading information"
            assert turn["state"] == "running" and not turn.get("deliveries") and not turn.get("sent")
        finally:
            reply_route.reset(token)
            finish.set()
            await native_harness.settle(value.queue)
            await value.adapter._slack_activity.flush()
            await value.queue.close()
            await value.adapter._slack_activity.close()
        assert turn["state"] == "done" and len(turn["deliveries"]) == 1
        assert sdk.send_message.call_count == 2  # One progress message, one final answer.
        assert sdk.send_message.call_args.kwargs["text"] == "answer " + native_harness.uid(1)
        assert sdk.update_message.call_args.args[3] == "Completed."
    asyncio.run(run())


def test_companion_progress_and_final_answer_keep_the_original_thread(slack_host):
    async def run():
        value = slack_host
        value.gate.clear()
        sdk = value.adapter._inkbox.slack
        sdk.send_message.return_value = NS(id=native_harness.uid(70), status="sent", message_ts="1234567890.000010")
        sdk.update_message.return_value = NS(status="succeeded")
        value.adapter._slack_activity._progress.interval = 0
        thread = "1234567800.000001"
        await value.receiver.accept(companion_harness.incoming(thread=thread))
        await companion_harness.wait_inputs(value, 1)
        event = value.inputs[0]
        row = next(iter(value.receiver.rows.values()))
        turn = row["turns"][0]
        token = reply_route.set(None)
        try:
            await value.adapter.on_processing_start(event)
            progress = await value.adapter.send(event.source.chat_id, "👁 Looking at the image Check the chart")
            assert progress.message_id.startswith("inkbox-slack-progress:")
            await value.adapter._slack_activity.flush()
            assert sdk.send_message.call_args.kwargs["text"] == "Checking the image"
            assert sdk.send_message.call_args.kwargs["thread_ts"] == thread
            await value.adapter.edit_message(event.source.chat_id, progress.message_id, "🔀 Delegating list worker_1234")
            await value.adapter._slack_activity.flush()
            assert sdk.update_message.call_args.args[1:4] == (
                "CEXAMPLE", "1234567890.000010", "Checking delegated work")
            assert "delivery" not in turn
            value.receiver.capture_result(event, "Here is the result.")
            result = await value.adapter.send(event.source.chat_id, "Here is the result.", reply_to=event.message_id)
            assert result.success and turn["delivery"]["state"] == "sent"
        finally:
            reply_route.reset(token)
            value.gate.set()
            await companion_harness.idle(value)
            await value.adapter._slack_activity.flush()
            await value.receiver.close()
            await value.adapter._slack_activity.close()
        assert sdk.send_message.call_count == 2
        assert sdk.send_message.call_args.kwargs["text"] == "Here is the result."
        assert sdk.send_message.call_args.kwargs["thread_ts"] == thread
        assert sdk.update_message.call_args.args[3] == "Completed."
    asyncio.run(run())
