"""Restart must preserve observed run outcomes, not invent a successful Stop."""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from inkbox_plugin.slack_progress import SlackProgress
from .test_slack_progress import resource, route
from .test_slack_task_streams import OP_ID, native_resource, operation, source


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("outcome,text,status", [
    ("completed", "Completed.", "complete"),
    ("failed", "Could not complete.", "error"),
    ("cancelled", "Stopped.", "complete"),
])
def test_terminal_intent_survives_pending_update_and_restart(tmp_path, native, outcome, text, status):
    async def run():
        sdk = native_resource() if native else resource()
        meta = source() if native else route()
        tracker = SlackProgress(sdk, tmp_path / "progress.json", interval=0)
        await tracker.notify("chat", meta, "accepted")
        await tracker.progress("chat", meta, "Working")
        await tracker.flush()
        if native:
            sdk._http.post.side_effect = None
            sdk._http.post.return_value = operation("stream_append", "in_progress")
        else:
            sdk.update_message.return_value = NS(status="in_progress", id=OP_ID)
        await tracker.progress("chat", meta, "Checking")
        await tracker.flush()
        await tracker.notify("chat", meta, outcome)
        await tracker.flush()
        saved = next(iter(json.loads(tracker.path.read_text()).values()))
        assert saved["outcome"] == outcome
        assert saved["uncertain"]
        if native:
            assert sdk._http.post.call_count == 2
            sdk._http.get.return_value = operation("stream_append")
            sdk._http.post.return_value = operation("stream_stop")
        else:
            assert sdk.update_message.call_count == 1
            sdk.get_operation.return_value = NS(status="succeeded")
            sdk.update_message.return_value = NS(status="succeeded")
        restarted = SlackProgress(sdk, tracker.path, interval=0)
        await restarted.recover()
        await restarted.flush()
        if native:
            chunk = sdk._http.post.call_args.kwargs["json"]["chunks"][0]
            assert chunk["title"] == text
            assert chunk["status"] == status
            assert sdk._http.post.call_count == 3
        else:
            assert sdk.update_message.call_args.args[3] == text
            assert sdk.update_message.call_count == 2
        assert json.loads(tracker.path.read_text()) == {}
    asyncio.run(run())


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_disconnect_does_not_claim_verified_cancellation(tmp_path, native, restart):
    async def run():
        sdk = native_resource() if native else resource()
        meta = source() if native else route()
        tracker = SlackProgress(sdk, tmp_path / "progress.json", interval=0)
        await tracker.notify("chat", meta, "accepted")
        await tracker.progress("chat", meta, "Working")
        await tracker.flush()
        if restart:
            tracker = SlackProgress(sdk, tracker.path, interval=0)
            await tracker.recover()
            await tracker.flush()
        else:
            await tracker.close()
        if native:
            text = sdk._http.post.call_args.kwargs["json"]["chunks"][0]["title"]
        else:
            text = sdk.update_message.call_args.args[3]
        assert text == "Progress paused after disconnecting."
        assert json.loads(tracker.path.read_text()) == {}
    asyncio.run(run())


def test_restart_retires_intent_that_never_reached_presend_checkpoint(tmp_path):
    async def run():
        sdk = resource()
        tracker = SlackProgress(sdk, tmp_path / "progress.json", interval=0)
        key = tracker._key("chat", route())
        tracker.path.write_text(json.dumps({key: {
            "chat_id": "chat", "route": route(), "revision": 0, "desired": "Working",
        }}))
        await tracker.recover()
        await tracker.flush()
        sdk.send_message.assert_not_called()
        sdk.get_action_by_key.assert_not_called()
        sdk.update_message.assert_not_called()
        assert json.loads(tracker.path.read_text()) == {}
    asyncio.run(run())


@pytest.mark.parametrize("error,retries", [
    ("message_not_found", 0), ("rate_limited", 3), ("connection_failed", 3),
])
def test_definitive_terminal_message_failure_does_not_restart_retry_budget(tmp_path, monkeypatch, error, retries):
    async def run():
        sdk = resource()
        tracker = SlackProgress(sdk, tmp_path / "progress.json", interval=0)
        await tracker.notify("chat", route(), "accepted")
        await tracker.progress("chat", route(), "Working")
        await tracker.flush()
        clock = iter(range(0, 1000, 10))
        monkeypatch.setattr("inkbox_plugin.slack_progress.time.time", lambda: next(clock))
        sdk.update_message.return_value = NS(status="failed", error_code=error, retry_after=1)
        await tracker.notify("chat", route(), "completed")
        await tracker.flush()
        assert sdk.update_message.call_count == 1 + retries
        assert json.loads(tracker.path.read_text()) == {}
        restarted = SlackProgress(sdk, tracker.path, interval=0)
        await restarted.recover()
        await restarted.flush()
        assert sdk.update_message.call_count == 1 + retries
        sdk.get_action_by_key.assert_not_called()
        sdk.get_operation.assert_not_called()
    asyncio.run(run())
