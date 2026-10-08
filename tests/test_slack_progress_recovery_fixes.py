"""Trusted final delivery and durable reconciliation of uncertain progress edits."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from gateway.platforms.base import BasePlatformAdapter

from inkbox_plugin.slack_progress import SlackProgress
from . import test_slack_progress_adapter as adapter_harness
from .test_slack_progress import resource
from .test_slack_activity import dm
from .test_slack_task_streams import operation
from .test_slack_typed_streams import TypedResource

adapter_case = adapter_harness.adapter_case


@pytest.mark.parametrize("companion", [False, True])
@pytest.mark.parametrize("body", ["👁 Looking at the image:", "⚙️ custom_tool: completed"])
@pytest.mark.parametrize("notice", [None, "admin", "tool_progress"])
def test_host_final_delivery_preserves_rendered_or_chunked_text(adapter_case, monkeypatch, companion, body, notice):
    async def run():
        case = adapter_case(companion)
        case.turn["result" if companion else "answer"] = body + " ![chart](https://example.com/chart.png)"
        metadata = {"notify": True, **({"notice_type": notice} if notice else {})}

        async def final_send(adapter, event, *args, **kwargs):
            return await adapter.send(event.source.chat_id, body, reply_to=event.message_id, metadata=metadata)

        monkeypatch.setattr(BasePlatformAdapter, "_send_final_text", final_send, raising=False)
        event = NS(source=NS(chat_id=case.chat_id), message_id="turn-1")
        result = await case.adapter._send_final_text(event)
        assert result.message_id == ("suppressed-admin-notice" if notice else "delivered-answer")
        assert case.send.await_count == (0 if notice else 1)
        case.adapter._slack_activity.progress.assert_not_awaited()
        # Caller-supplied notify metadata does not retain/invent host authority.
        case.send.reset_mock()
        await case.adapter.send(case.chat_id, body, reply_to="turn-1", metadata={"notify": True})
        case.send.assert_not_awaited()
        case.adapter._slack_activity.progress.assert_awaited_once()
    asyncio.run(run())


def test_host_final_boundary_cannot_authorize_a_different_turn(adapter_case, monkeypatch):
    async def run():
        case = adapter_case()

        async def final_send(adapter, event, *args, **kwargs):
            return await adapter.send(case.chat_id, "📄 Reading report.txt", reply_to="turn-1", metadata={"notify": True})

        monkeypatch.setattr(BasePlatformAdapter, "_send_final_text", final_send, raising=False)
        await case.adapter._send_final_text(NS(source=NS(chat_id=case.chat_id), message_id="another-turn"))
        case.send.assert_not_awaited()
        case.adapter._slack_activity.progress.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown", "in_progress", "wrong_route", "wrong_message"])
def test_timed_out_progress_edit_recovers_only_after_original_key_is_resolved(tmp_path, typed, status):
    async def run():
        sdk = resource()
        result = operation("message_update", status if status in {"succeeded", "failed", "unknown", "in_progress"} else "succeeded")
        if status == "wrong_route":
            result["conversation_id"] = "COTHER"
        if status == "wrong_message":
            result["message_ts"] = "9999999999.000001"
        lookup = Mock(return_value=NS(**result) if typed else result)
        if typed:
            class TypedEdits(TypedResource):
                def get_operation_by_key(self, *args, **kwargs):
                    return lookup(*args, **kwargs)
            actual = TypedEdits()
            for key, value in vars(sdk).items():
                setattr(actual, key, value)
            sdk = actual
        else:
            sdk._http = Mock(get=lookup)
        path = tmp_path / "progress.json"
        tracker = SlackProgress(sdk, path, interval=0)
        await tracker.notify("chat", dm(), "accepted")
        await tracker.progress("chat", dm(), "Reading")
        await tracker.flush()
        sdk.update_message.side_effect = TimeoutError("Synthetic unconfirmed PATCH")
        await tracker.progress("chat", dm(), "Checking")
        await tracker.flush()
        original_key = sdk.update_message.call_args.kwargs["idempotency_key"]
        await tracker.notify("chat", dm(), "completed")
        await tracker.flush()
        sdk.update_message.assert_called_once()
        saved = next(iter(json.loads(path.read_text()).values()))
        assert saved["uncertain"] and not saved.get("operation_id")
        sdk.update_message.side_effect = None
        sdk.update_message.return_value = NS(status="succeeded")
        recovered = SlackProgress(sdk, path, interval=0)
        await recovered.recover()
        await recovered.flush()
        lookup.assert_called_once()
        if typed:
            assert lookup.call_args.args == (dm()["connection_id"],)
            assert lookup.call_args.kwargs == {"idempotency_key": original_key}
        else:
            assert lookup.call_args.args[0].endswith("/operations/by-key")
            assert lookup.call_args.kwargs == {"headers": {"Idempotency-Key": original_key}}
        resolved = status in {"succeeded", "failed"}
        assert sdk.update_message.call_count == (2 if resolved else 1)
        sdk.send_message.assert_called_once()
        if resolved:
            assert sdk.update_message.call_args.args[:3] == (dm()["connection_id"], dm()["conversation_id"], "1234567890.000010")
            assert sdk.update_message.call_args.args[3] == "Completed."
            assert sdk.update_message.call_args.kwargs["idempotency_key"] != original_key
            assert json.loads(path.read_text()) == {}
        else:
            assert next(iter(json.loads(path.read_text()).values()))["uncertain"]
    asyncio.run(run())


@pytest.mark.skipif(not hasattr(BasePlatformAdapter, "_send_final_text"), reason="Current real host final-delivery hook")
@pytest.mark.parametrize("companion", [False, True])
def test_real_host_final_hook_scopes_rendered_delivery(adapter_case, monkeypatch, companion):
    async def run():
        case = adapter_case(companion)
        rendered = "👁 Looking at the image:"
        case.turn["result" if companion else "answer"] = rendered + " ![chart](https://example.com/chart.png)"

        async def ledgered(event, session_key, text, metadata, **kwargs):
            result = await case.adapter.send(event.source.chat_id, text, reply_to=kwargs["reply_to"], metadata=metadata)
            return result, case.adapter

        monkeypatch.setattr(case.adapter, "send_final_ledgered", ledgered, raising=False)
        event = NS(source=NS(chat_id=case.chat_id, platform="inkbox"), message_id="turn-1")
        deliveries = []
        await case.adapter._send_final_text(event, "session", rendered, {"notify": True}, False, 0, deliveries.append)
        assert [item.message_id for item in deliveries] == ["delivered-answer"]
        case.send.assert_awaited_once()
        case.adapter._slack_activity.progress.assert_not_awaited()
    asyncio.run(run())
