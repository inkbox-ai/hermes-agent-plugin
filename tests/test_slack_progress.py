"""Progress uses its own source-bound message, never final-answer delivery."""

import asyncio
import json
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock
from uuid import UUID

import pytest

from inkbox_plugin.slack_activity import SlackActivity
from inkbox_plugin.slack_progress import SlackProgress
from .test_slack_activity import route, dm


def resource():
    return NS(send_message=Mock(return_value=NS(status="sent", message_ts="1234567890.000010")),
              update_message=Mock(return_value=NS(status="succeeded")),
              get_action_by_key=Mock(return_value=NS(status="sent", message_ts="1234567890.000010")),
              get_operation=Mock(return_value=NS(status="succeeded")))


@pytest.mark.parametrize("meta", [route(), dm()])
@pytest.mark.parametrize("outcome,text", [("completed", "Completed."), ("cancelled", "Stopped."),
                                          ("failed", "Could not complete.")])
def test_one_message_coalesces_updates_and_finishes_on_original_route(tmp_path, meta, outcome, text):
    async def scenario():
        sdk = resource()
        tracker = SlackProgress(sdk, tmp_path / "progress.json", interval=0)
        await tracker.notify("chat", meta, "accepted")
        handle = await tracker.progress("chat", meta, "Reading documentation…")
        await tracker.flush()
        for phrase in ("Searching…", "Checking…", "Testing…"):
            assert await tracker.progress("chat", meta, phrase) == handle
        await tracker.flush()
        sdk.send_message.assert_called_once()
        assert sdk.send_message.call_args.kwargs["thread_ts"] == meta["thread_ts"]
        assert sdk.send_message.call_args.args == (meta["connection_id"],)
        assert sdk.send_message.call_args.kwargs["conversation_id"] == meta["conversation_id"]
        assert [call.args[3] for call in sdk.update_message.call_args_list] == ["Testing…"]
        await tracker.notify("chat", meta, outcome)
        await tracker.flush()
        assert sdk.update_message.call_args.args == (meta["connection_id"], meta["conversation_id"],
                                                     "1234567890.000010", text)
        assert json.loads(tracker.path.read_text()) == {}
        assert await tracker.progress("chat", meta, "Late callback", handle) is None
    asyncio.run(scenario())


def test_no_progress_message_for_quiet_or_already_finished_turn(tmp_path):
    async def scenario():
        sdk = resource()
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        assert await tracker.progress("chat", route(), "No admission") is None
        await tracker.notify("chat", route(), "accepted")
        await tracker.progress("chat", route(), "Short operation")
        await tracker.notify("chat", route(), "completed")
        await tracker.flush()
        sdk.send_message.assert_not_called()
        sdk.update_message.assert_not_called()
    asyncio.run(scenario())


def test_old_handles_other_chats_and_changed_routes_cannot_update_new_turn(tmp_path):
    async def scenario():
        sdk = resource()
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", route(), "accepted")
        handle = await tracker.progress("chat", route(), "Working")
        await tracker.flush()
        for meta in ({**route(), "connection_id": "other"}, {**route(), "thread_ts": None},
                     {**route(), "source_event_id": "other"}):
            assert await tracker.progress("chat", meta, "Wrong source", handle) is None
        assert await tracker.progress("other", {}, "Wrong chat", handle) is None
        await tracker.notify("chat", route(), "cancelled")
        await tracker.notify("chat", route("event-2"), "accepted")
        assert await tracker.progress("chat", {}, "Old edit", handle) is None
        assert await tracker.progress("chat", route(), "Old callback") is None
        await tracker.flush()
        assert sdk.send_message.call_count == 1
    asyncio.run(scenario())


def test_stop_during_create_orders_terminal_edit_after_the_send(tmp_path):
    async def scenario():
        sdk = resource()
        started, release = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return NS(status="sent", message_ts="1234567890.000010")
        sdk.send_message.side_effect = send
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", route(), "accepted")
        await tracker.progress("chat", route(), "Working")
        assert await asyncio.to_thread(started.wait, 5)
        try:
            await tracker.notify("chat", route(), "cancelled")
            sdk.update_message.assert_not_called()
        finally:
            release.set()
        await tracker.flush()
        assert sdk.update_message.call_args.args[3] == "Stopped."
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["unknown", "sending", "exception"])
def test_uncertain_creation_never_resends_and_restart_looks_up_original_key(tmp_path, status):
    async def scenario():
        sdk = resource()
        if status == "exception":
            sdk.send_message.side_effect = TimeoutError()
        else:
            sdk.send_message.return_value = NS(status=status, message_ts=None)
        path = tmp_path / "p.json"
        tracker = SlackProgress(sdk, path, interval=0)
        await tracker.notify("chat", route(), "accepted")
        await tracker.progress("chat", route(), "Working")
        await tracker.flush()
        await tracker.progress("chat", route(), "Another update")
        await tracker.notify("chat", route(), "cancelled")
        await tracker.flush()
        sdk.send_message.assert_called_once()
        sdk.update_message.assert_not_called()
        restarted = SlackProgress(sdk, path, interval=0)
        await restarted.recover()
        await restarted.flush()
        assert sdk.get_action_by_key.call_args.args[1] == sdk.send_message.call_args.kwargs["idempotency_key"]
        sdk.send_message.assert_called_once()
        assert sdk.update_message.call_args.args[3] == "Stopped."
        assert json.loads(path.read_text()) == {}
    asyncio.run(scenario())


def test_uncertain_edit_defers_newer_edits_until_reconciliation(tmp_path):
    async def scenario():
        sdk = resource()
        path = tmp_path / "p.json"
        tracker = SlackProgress(sdk, path, interval=0)
        await tracker.notify("chat", route(), "accepted")
        await tracker.progress("chat", route(), "Working")
        await tracker.flush()
        operation_id = UUID("00000000-0000-4000-8000-000000000001")
        sdk.update_message.return_value = NS(status="in_progress", id=operation_id)
        await tracker.progress("chat", route(), "Testing")
        await tracker.flush()
        await tracker.notify("chat", route(), "completed")
        await tracker.flush()
        sdk.update_message.assert_called_once()
        sdk.get_operation.return_value = NS(status="unknown")
        await SlackProgress(sdk, path, interval=0).recover()
        sdk.update_message.assert_called_once()
        sdk.get_operation.return_value = NS(status="succeeded")
        sdk.update_message.return_value = NS(status="succeeded")
        restarted = SlackProgress(sdk, path, interval=0)
        await restarted.recover()
        await restarted.flush()
        assert sdk.get_operation.call_args.args[1] == str(operation_id)
        assert sdk.update_message.call_count == 2
    asyncio.run(scenario())


def test_definitive_edit_rejection_does_not_freeze_terminal_cleanup(tmp_path, monkeypatch):
    async def scenario():
        sdk = resource()
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", route(), "accepted")
        await tracker.progress("chat", route(), "Working")
        await tracker.flush()
        sdk.update_message.return_value = NS(status="failed", error_code="rate_limited", retry_after=1)
        await tracker.progress("chat", route(), "Testing")
        await tracker.flush()
        sdk.update_message.assert_called_once()
        saved = next(iter(json.loads(tracker.path.read_text()).values()))
        assert not saved.get("uncertain")
        # Move past the provider's cooldown without making the test sleep.
        monkeypatch.setattr("inkbox_plugin.slack_progress.time.time", lambda: saved["retry_at"] + 1)
        sdk.update_message.return_value = NS(status="succeeded")
        await tracker.notify("chat", route(), "completed")
        await tracker.flush()
        assert sdk.update_message.call_args.args[3] == "Completed."
        assert sdk.update_message.call_count == 2
        assert json.loads(tracker.path.read_text()) == {}
    asyncio.run(scenario())


def test_activity_lifecycle_and_progress_are_independent(tmp_path):
    async def scenario():
        sdk = resource()
        sdk.set_processing_status = Mock(return_value=NS(status="succeeded"))
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        tracker._progress.interval = 0
        await tracker.notify("chat", "slack", route(), "accepted")
        await tracker.progress("chat", route(), "Checking <@U123> & tests")
        await tracker.flush()
        assert "&lt;@U123&gt; &amp;" in sdk.send_message.call_args.kwargs["text"]
        await tracker.notify("chat", "slack", route(), "waiting")
        await tracker.flush()
        assert sdk.update_message.call_args.args[3] == "Waiting for your approval."
        await tracker.close()
        assert sdk.update_message.call_args.args[3] == "Progress paused after disconnecting."
        assert [call.args[3] for call in sdk.set_processing_status.call_args_list] == ["processing", "suspended", "active"]
        assert not tracker.has_chat("chat")
    asyncio.run(scenario())


def test_no_external_effect_without_durable_intent(tmp_path, monkeypatch):
    async def scenario():
        sdk = resource()
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        def fail():
            raise OSError("unavailable")
        monkeypatch.setattr(tracker, "_save", fail)
        await tracker.notify("chat", route(), "accepted")
        await tracker.progress("chat", route(), "Working")
        await tracker.flush()
        sdk.send_message.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("meta", [route(), dm()])
def test_progress_uses_published_sdk_message_edit_wire(tmp_path, meta):
    import httpx
    from inkbox import Inkbox

    requests = []
    message_ts = "1234567890.000010"
    def handle(request):
        requests.append(request)
        payload = {"id": "00000000-0000-4000-8000-000000000003",
                   "connection_id": meta["connection_id"], "conversation_id": meta["conversation_id"],
                   "message_ts": message_ts}
        if request.method == "POST":
            payload.update(status="sent", thread_ts=meta["thread_ts"])
        else:
            payload.update(status="succeeded", operation="message_update")
        return httpx.Response(200, json=payload)

    client = Inkbox(api_key="synthetic-test-key", base_url="https://api.example")
    client._api_http._client.close()
    client._api_http._client = httpx.Client(base_url="https://api.example/api/v1",
                                           transport=httpx.MockTransport(handle))
    async def scenario():
        tracker = SlackProgress(client.slack, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", meta, "accepted")
        await tracker.progress("chat", meta, "Checking information")
        await tracker.flush()
        await tracker.progress("chat", meta, "Running checks")
        await tracker.flush()
        await tracker.notify("chat", meta, "completed")
        await tracker.flush()
    try:
        asyncio.run(scenario())
        assert [request.method for request in requests] == ["POST", "PATCH", "PATCH"]
        created = json.loads(requests[0].content)
        assert created == {"conversation_id": meta["conversation_id"], "text": "Checking information",
                           **({"thread_ts": meta["thread_ts"]} if meta["thread_ts"] else {})}
        assert all(request.url.path.endswith(f"/messages/{message_ts}") for request in requests[1:])
        assert json.loads(requests[-1].content) == {"text": "Completed."}
        assert len({request.headers["Idempotency-Key"] for request in requests}) == 3
    finally:
        client.close()
