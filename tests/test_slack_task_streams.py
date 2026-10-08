"""Native task cards fall back only before a stream could have been created."""

import asyncio
import json
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from inkbox_plugin.slack_progress import SlackProgress
from .test_slack_activity import route
from .test_slack_progress import resource

STREAM_ID = "00000000-0000-4000-8000-000000000010"
OP_ID = "00000000-0000-4000-8000-000000000011"


def source():
    return {**route(), "workspace_id": "T123", "actor_id": "U123", "recipient_team_id": "T123"}


def operation(kind="stream_start", status="succeeded", **overrides):
    return {"id": STREAM_ID if kind == "stream_start" else OP_ID, "operation": kind,
            "connection_id": source()["connection_id"], "conversation_id": "C123", "status": status,
            "message_ts": "1234567890.000010" if status == "succeeded" else None, **overrides}


def native_resource():
    sdk = resource()
    sdk.capabilities = Mock(return_value=NS(capabilities={"task_streaming": NS(scopes_satisfied=True)}))
    def post(path, **kwargs):
        kind = "stream_append" if path.endswith("/append") else "stream_stop" if path.endswith("/stop") else "stream_start"
        return operation(kind)
    sdk._http = Mock(post=Mock(side_effect=post), get=Mock(return_value=operation()))
    return sdk


@pytest.mark.parametrize("outcome,status", [("completed", "complete"), ("cancelled", "complete"), ("failed", "error")])
def test_native_card_lifecycle_never_sends_an_ordinary_progress_reply(tmp_path, outcome, status):
    async def run():
        sdk = native_resource()
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", source(), "accepted")
        handle = await tracker.progress("chat", source(), "Reading information")
        await tracker.flush()
        await tracker.progress("chat", source(), "Checking results", handle)
        await tracker.flush()
        await tracker.notify("chat", source(), outcome)
        await tracker.flush()
        calls = sdk._http.post.call_args_list
        assert len(calls) == 3
        assert calls[0].args[0].endswith("/conversations/C123/streams")
        assert calls[1].args[0].endswith(f"/streams/{STREAM_ID}/append")
        assert calls[2].args[0].endswith(f"/streams/{STREAM_ID}/stop")
        initial = calls[0].kwargs["json"]
        assert initial["thread_ts"] == source()["thread_ts"]
        assert (initial["recipient_user_id"], initial["recipient_team_id"]) == ("U123", "T123")
        assert initial["task_display_mode"] == "timeline"
        chunks = [call.kwargs["json"]["chunks"][0] for call in calls]
        assert len({chunk["id"] for chunk in chunks}) == 1
        assert [chunk["status"] for chunk in chunks] == ["in_progress", "in_progress", status]
        assert len({call.kwargs["headers"]["Idempotency-Key"] for call in calls}) == 3
        assert json.loads(tracker.path.read_text()) == {}
        sdk.send_message.assert_not_called()
        sdk.update_message.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("missing", ["thread_ts", "actor_id", "workspace_id", "recipient_team_id", "capability", "scope", "transport"])
def test_absent_native_support_falls_back_without_inventing_a_thread(tmp_path, missing):
    async def run():
        sdk = native_resource()
        meta = source()
        if missing in meta:
            meta[missing] = None
        elif missing == "capability":
            sdk.capabilities.return_value.capabilities = {}
        elif missing == "scope":
            sdk.capabilities.return_value.capabilities["task_streaming"].scopes_satisfied = False
        else:
            del sdk._http
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", meta, "accepted")
        await tracker.progress("chat", meta, "Working")
        await tracker.flush()
        sdk.send_message.assert_called_once()
        assert sdk.send_message.call_args.kwargs["thread_ts"] == meta["thread_ts"]
        if missing != "transport":
            sdk._http.post.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("error", ["channel_type_not_supported", "missing_scope", "app_not_eligible", "invalid_thread_ts"])
def test_only_definitive_unsupported_start_permits_fallback(tmp_path, error):
    async def run():
        sdk = native_resource()
        sdk._http.post.side_effect = None
        sdk._http.post.return_value = operation(status="failed", error_code=error)
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Working")
        await tracker.flush()
        sdk._http.post.assert_called_once()
        sdk.send_message.assert_called_once()
        await tracker.notify("chat", source(), "completed")
        await tracker.flush()
        assert sdk.update_message.call_args.args[3] == "Completed."
        sdk._http.post.assert_called_once()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["unknown", "in_progress", "timeout", "wrong_connection", "wrong_conversation"])
def test_ambiguous_start_never_falls_back_and_restart_closes_original(tmp_path, failure):
    async def run():
        sdk = native_resource()
        original_post = sdk._http.post.side_effect
        if failure == "timeout":
            sdk._http.post.side_effect = TimeoutError()
        else:
            sdk._http.post.side_effect = None
            overrides = {failure.removeprefix("wrong_") + "_id": "unrelated"} if failure.startswith("wrong_") else {}
            sdk._http.post.return_value = operation(status=failure if not overrides else "succeeded", **overrides)
        path = tmp_path / "p.json"
        tracker = SlackProgress(sdk, path, interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Working")
        await tracker.flush()
        await tracker.notify("chat", source(), "cancelled")
        await tracker.flush()
        sdk.send_message.assert_not_called()
        sdk._http.post.assert_called_once()
        initial_key = sdk._http.post.call_args.kwargs["headers"]["Idempotency-Key"]
        sdk._http.post.side_effect = original_post
        restarted = SlackProgress(sdk, path, interval=0)
        await restarted.recover()
        await restarted.flush()
        assert sdk._http.get.call_args.kwargs["headers"] == {"Idempotency-Key": initial_key}
        assert sdk._http.post.call_count == 2
        assert sdk._http.post.call_args.args[0].endswith(f"/{STREAM_ID}/stop")
        sdk.send_message.assert_not_called()
    asyncio.run(run())


def test_ambiguous_append_does_not_open_a_second_progress_message(tmp_path):
    async def run():
        sdk = native_resource()
        original_post = sdk._http.post.side_effect
        path = tmp_path / "p.json"
        tracker = SlackProgress(sdk, path, interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Working")
        await tracker.flush()
        sdk._http.post.side_effect = None
        sdk._http.post.return_value = operation("stream_append", "unknown")
        await tracker.progress("chat", source(), "Checking")
        await tracker.flush()
        await tracker.notify("chat", source(), "completed")
        await tracker.flush()
        assert sdk._http.post.call_count == 2
        sdk.send_message.assert_not_called()
        sdk._http.get.return_value = operation("stream_append", "unknown")
        await SlackProgress(sdk, path, interval=0).recover()
        assert sdk._http.post.call_count == 2
        sdk._http.get.return_value = operation("stream_append")
        sdk._http.post.side_effect = original_post
        restarted = SlackProgress(sdk, path, interval=0)
        await restarted.recover()
        await restarted.flush()
        assert sdk._http.post.call_args.args[0].endswith(f"/{STREAM_ID}/stop")
    asyncio.run(run())


@pytest.mark.parametrize("during", ["capabilities", "start"])
def test_stop_during_native_setup_does_not_leave_open_stream(tmp_path, during):
    async def run():
        sdk = native_resource()
        entered, release = threading.Event(), threading.Event()
        target = sdk.capabilities if during == "capabilities" else sdk._http.post
        original = target.side_effect
        result = target.return_value
        def delayed(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs) if original else result
        target.side_effect = delayed
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Completed.")
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            await tracker.notify("chat", source(), "completed")
        finally:
            release.set()
        await tracker.flush()
        if during == "capabilities":
            sdk._http.post.assert_not_called()
        else:
            assert sdk._http.post.call_count == 2
            assert sdk._http.post.call_args.args[0].endswith("/stop")
        assert json.loads(tracker.path.read_text()) == {}
    asyncio.run(run())


def test_crash_after_successful_stop_reconciles_without_a_second_stop(tmp_path, monkeypatch):
    async def run():
        sdk = native_resource()
        path = tmp_path / "p.json"
        tracker = SlackProgress(sdk, path, interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Working")
        await tracker.flush()
        save = tracker._save
        def fail_after_stop():
            if not tracker.records:
                raise OSError("synthetic checkpoint failure")
            save()
        monkeypatch.setattr(tracker, "_save", fail_after_stop)
        await tracker.notify("chat", source(), "completed")
        await tracker.flush()
        assert sdk._http.post.call_count == 2
        sdk._http.get.return_value = operation("stream_stop")
        restarted = SlackProgress(sdk, path, interval=0)
        await restarted.recover()
        await restarted.flush()
        assert sdk._http.post.call_count == 2
        assert json.loads(path.read_text()) == {}
    asyncio.run(run())


def test_published_sdk_transport_preserves_stream_routes_and_credentials(tmp_path):
    import httpx
    from inkbox import Inkbox

    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            payload = {"connection_id": source()["connection_id"], "scopes": ["chat:write"], "missing_scopes": [],
                "capabilities": {"task_streaming": {"required_scopes": ["chat:write"], "missing_scopes": [],
                                                    "scopes_satisfied": True}},
                "native_processing_status": "unknown", "native_task_streaming": "unknown", "max_upload_bytes": 10485760}
        else:
            kind = "stream_stop" if request.url.path.endswith("/stop") else "stream_append" if request.url.path.endswith("/append") else "stream_start"
            payload = operation(kind)
        return httpx.Response(200, json=payload)
    client = Inkbox(api_key="synthetic-test-key", base_url="https://api.example")
    client._api_http._client.close()
    client._api_http._client = httpx.Client(base_url="https://api.example/api/v1",
        headers={"X-API-Key": "synthetic-test-key"}, transport=httpx.MockTransport(handle))
    async def run():
        tracker = SlackProgress(client.slack, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Reading information")
        await tracker.flush()
        await tracker.progress("chat", source(), "Checking results")
        await tracker.flush()
        await tracker.notify("chat", source(), "completed")
        await tracker.flush()
    try:
        asyncio.run(run())
        assert [r.method for r in requests] == ["GET", "POST", "POST", "POST"]
        assert all(r.url.host == "api.example" and r.headers["X-API-Key"] == "synthetic-test-key" for r in requests)
        assert json.loads(requests[1].content)["recipient_user_id"] == "U123"
        assert requests[2].url.path.endswith(f"/streams/{STREAM_ID}/append")
        assert requests[3].url.path.endswith(f"/streams/{STREAM_ID}/stop")
        assert json.loads(requests[3].content)["chunks"][0]["status"] == "complete"
    finally:
        client.close()


@pytest.mark.parametrize("keeps_rejecting", [False, True])
@pytest.mark.parametrize("error_code,retry_after", [("rate_limited", 1), ("connection_failed", None)])
def test_terminal_safe_retries_are_bounded_and_keep_original_stream(
    tmp_path, monkeypatch, keeps_rejecting, error_code, retry_after,
):
    async def run():
        sdk = native_resource()
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Working")
        await tracker.flush()
        # Advance wall time on each access, exercising the cooldown without a
        # real sleep; ordinary async scheduling still uses its monotonic clock.
        clock = iter(range(0, 1000, 10))
        monkeypatch.setattr("inkbox_plugin.slack_progress.time.time", lambda: next(clock))
        rejected = operation("stream_stop", "failed", error_code=error_code, retry_after=retry_after)
        sdk._http.post.side_effect = None if keeps_rejecting else [rejected, operation("stream_stop")]
        sdk._http.post.return_value = rejected
        await tracker.notify("chat", source(), "completed")
        await tracker.flush()
        stops = sdk._http.post.call_args_list[1:]
        assert len(stops) == (4 if keeps_rejecting else 2)
        assert all(call.args[0].endswith(f"/{STREAM_ID}/stop") for call in stops)
        assert len({call.kwargs["headers"]["Idempotency-Key"] for call in stops}) == len(stops)
        assert bool(json.loads(tracker.path.read_text())) is keeps_rejecting
        sdk.send_message.assert_not_called()
    asyncio.run(run())


def test_unknown_connection_failure_never_retries_terminal_stop(tmp_path):
    async def run():
        sdk = native_resource()
        tracker = SlackProgress(sdk, tmp_path / "p.json", interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Working")
        await tracker.flush()
        sdk._http.post.side_effect = None
        sdk._http.post.return_value = operation("stream_stop", "unknown", error_code="connection_failed")
        await tracker.notify("chat", source(), "completed")
        await tracker.flush()
        await tracker.close()
        assert sdk._http.post.call_count == 2  # One start and one unconfirmed stop.
        assert next(iter(json.loads(tracker.path.read_text()).values()))["uncertain"]
        sdk.send_message.assert_not_called()
    asyncio.run(run())


def test_restart_cleanup_keeps_unvisited_streams_durable_on_another_crash(tmp_path, monkeypatch):
    async def run():
        sdk = native_resource()
        path = tmp_path / "p.json"
        tracker = SlackProgress(sdk, path, interval=0)
        await tracker.notify("first", source(), "accepted")
        await tracker.progress("first", source(), "Working")
        await tracker.flush()
        records = json.loads(path.read_text())
        first = next(iter(records.values()))
        second_route = {**source(), "source_event_id": "event-2"}
        second_key = tracker._key("second", second_route)
        records[second_key] = {**first, "chat_id": "second", "route": second_route,
                               "stream_id": OP_ID}
        first.update(uncertain=True, terminal=True, pending_kind="stream_stop", pending_key="original-stop")
        path.write_text(json.dumps(records))
        sdk._http.get.return_value = operation("stream_stop")
        restarted = SlackProgress(sdk, path, interval=0)
        save = restarted._save
        class Crash(BaseException):
            pass
        def crash_after_save():
            save()
            raise Crash()
        monkeypatch.setattr(restarted, "_save", crash_after_save)
        with pytest.raises(Crash):
            await restarted.recover()
        assert set(json.loads(path.read_text())) == {second_key}
        again = SlackProgress(sdk, path, interval=0)
        await again.recover()
        await again.flush()
        assert sdk._http.post.call_args.args[0].endswith(f"/{OP_ID}/stop")
        assert json.loads(path.read_text()) == {}
    asyncio.run(run())
