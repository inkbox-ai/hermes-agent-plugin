"""Slack indicators follow admitted turns, not context-only messages."""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from inkbox_plugin.slack_activity import SlackActivity


def route(event="event-1", message="1234567890.000002"):
    return dict(connection_id="00000000-0000-4000-8000-000000000001",
                conversation_id="C123", message_ts=message, thread_ts="1234567890.000001",
                source_event_id=event, conversation_kind="group", slack_mentioned=True, sender="T123:U123")


def resource():
    sdk = Mock()
    sdk.set_processing_status.return_value = sdk.remove_reaction.return_value = sdk.add_reaction.return_value = NS(status="succeeded")
    return sdk


def statuses(sdk):
    return [call.args[3] for call in sdk.set_processing_status.call_args_list]


def dm(event="event-1", message="1234567890.000002"):
    return {**route(event, message), "thread_ts": None, "conversation_kind": "direct", "slack_mentioned": False}


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("kind", ["direct", "group"])
def test_inline_reactions_follow_exact_message_without_creating_thread(tmp_path, outcome, kind):
    async def scenario():
        sdk = resource()
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        meta = {**dm(), "conversation_kind": kind}
        for state in ("accepted", "accepted", "waiting", "resumed"):
            await tracker.notify("chat", "slack", meta, state)
        await tracker.flush()
        assert [call.args[3] for call in sdk.add_reaction.call_args_list] == ["eyes"]
        for state in (outcome, outcome):
            await tracker.notify("chat", "slack", meta, state)
        await tracker.flush()
        assert [call.args[3] for call in sdk.add_reaction.call_args_list] == (["eyes", "x"] if outcome == "failed" else ["eyes"])
        assert [call.args[3] for call in sdk.remove_reaction.call_args_list] == ["x", "eyes"]
        assert all(call.args[:3] == (dm()["connection_id"], "C123", dm()["message_ts"])
                   for call in sdk.mock_calls)
        assert json.loads(tracker.state_path.read_text()) == {}
        sdk.set_processing_status.assert_not_called()
    asyncio.run(scenario())


def test_dm_messages_and_thread_on_same_anchor_keep_independent_indicators(tmp_path):
    async def scenario():
        sdk = resource()
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        first, second = dm(), dm("event-2", "1234567890.000003")
        thread = {**route("event-3"), "thread_ts": first["message_ts"]}
        for meta in (first, second, thread):
            await tracker.notify("chat", "slack", meta, "accepted")
        await tracker.notify("chat", "slack", first, "cancelled")
        await tracker.flush()
        assert statuses(sdk) == ["processing"]
        removed_eyes = [call.args[2] for call in sdk.remove_reaction.call_args_list if call.args[3] == "eyes"]
        assert removed_eyes == [first["message_ts"]]
        assert len(json.loads(tracker.state_path.read_text())) == 2
        await tracker.close()
        assert statuses(sdk) == ["processing", "active"]
        assert json.loads(tracker.state_path.read_text()) == {}
        assert [call.args[3] for call in sdk.add_reaction.call_args_list] == ["eyes", "eyes"]
    asyncio.run(scenario())


def test_restart_marks_interrupted_dm_failed_and_retries_cleanup_with_same_keys(tmp_path):
    async def scenario():
        sdk = resource()
        path = tmp_path / "activity.json"
        tracker = SlackActivity(sdk, path)
        await tracker.notify("dm", "slack", dm(), "accepted")
        await tracker.flush()
        sdk.remove_reaction.return_value = NS(status="unknown")
        restarted = SlackActivity(sdk, path)
        await restarted.recover()
        await restarted.flush()
        assert sdk.add_reaction.call_args.args[3] == "x"
        assert [r["state"] for r in json.loads(path.read_text()).values()] == ["failed"]
        keys = [call.kwargs["idempotency_key"] for call in sdk.mock_calls[-2:]]
        sdk.remove_reaction.return_value = NS(status="succeeded")
        restarted_again = SlackActivity(sdk, path)
        await restarted_again.recover()
        await restarted_again.flush()
        assert [call.kwargs["idempotency_key"] for call in sdk.mock_calls[-2:]] == keys
        assert json.loads(path.read_text()) == {}
        # Once recorded successfully, failure markers stay on the message across restarts.
        sdk.reset_mock()
        await SlackActivity(sdk, path).recover()
        assert not sdk.mock_calls
    asyncio.run(scenario())


def test_reactions_work_without_native_status_capability(tmp_path):
    async def scenario():
        sdk = NS(add_reaction=Mock(return_value=NS(status="succeeded")),
                 remove_reaction=Mock(return_value=NS(status="succeeded")))
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        await tracker.notify("dm", "slack", dm(), "accepted")
        await tracker.notify("dm", "slack", dm(), "completed")
        await tracker.flush()
        sdk.add_reaction.assert_called_once()
        sdk.add_reaction.reset_mock()
        sdk.remove_reaction.reset_mock()
        await tracker.notify("thread", "slack", route(), "accepted")
        await tracker.flush()
        sdk.add_reaction.assert_not_called()
        sdk.remove_reaction.assert_not_called()
        assert json.loads(tracker.state_path.read_text()) == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_native_status_starts_and_clears_without_reactions(tmp_path, outcome):
    async def scenario():
        sdk = resource()
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        await tracker.notify("chat", "slack", route(), "accepted")
        await tracker.flush()
        assert statuses(sdk) == ["processing"]
        await tracker.notify("chat", "slack", route(), outcome)
        await tracker.flush()
        assert statuses(sdk) == ["processing", "active"]
        assert all(call.args[:3] == (route()["connection_id"], "C123", route()["thread_ts"])
                   for call in sdk.set_processing_status.call_args_list)
        assert json.loads(tracker.state_path.read_text()) == {}
        keys = [call.kwargs["idempotency_key"] for call in sdk.mock_calls]
        assert len(set(keys)) == 2 and all(len(key) <= 128 for key in keys)
        sdk.add_reaction.assert_not_called()
        sdk.remove_reaction.assert_not_called()
    asyncio.run(scenario())


def test_overlapping_messages_in_thread_and_duplicates_do_not_clear_busy_status(tmp_path):
    async def scenario():
        sdk = resource()
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        second = route("event-2", "1234567890.000003")
        await tracker.notify("chat", "slack", route(), "accepted")
        await tracker.notify("chat", "slack", route(), "accepted")
        await tracker.notify("chat", "slack", second, "accepted")
        await tracker.notify("chat", "slack", route(), "cancelled")
        await tracker.flush()
        assert statuses(sdk) == ["processing"]
        await tracker.notify("chat", "slack", second, "completed")
        await tracker.flush()
        assert statuses(sdk) == ["processing", "active"]
    asyncio.run(scenario())


def test_waiting_for_input_and_independent_threads(tmp_path):
    async def scenario():
        sdk = resource()
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        other = {**route("event-2"), "thread_ts": "1234567890.000004"}
        await tracker.notify("first", "slack", route(), "accepted")
        await tracker.notify("second", "slack", other, "accepted")
        await tracker.flush()
        await tracker.notify("first", "slack", route(), "waiting")
        await tracker.notify("first", "slack", route(), "resumed")
        await tracker.notify("first", "slack", route(), "failed")
        await tracker.flush()
        assert statuses(sdk)[-3:] == ["suspended", "processing", "active"]
        records = json.loads(tracker.state_path.read_text())
        assert [(r["thread_ts"], r["state"]) for r in records.values()] == [(other["thread_ts"], "processing")]
        await tracker.close()
        assert statuses(sdk)[-1] == "active"
        assert json.loads(tracker.state_path.read_text()) == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["processing", "suspended"])
def test_restart_clears_unfinished_native_status(tmp_path, state):
    async def scenario():
        sdk = resource()
        path = tmp_path / "activity.json"
        original = SlackActivity(sdk, path)
        await original.notify("chat", "slack", route(), "accepted")
        if state == "suspended":
            await original.notify("chat", "slack", route(), "waiting")
        await original.flush()
        restarted = SlackActivity(sdk, path)
        await restarted.recover()
        await restarted.flush()
        assert statuses(sdk)[-1] == "active"
        assert json.loads(path.read_text()) == {}
    asyncio.run(scenario())


def test_upgrade_removes_leftover_reactions_without_adding_any(tmp_path):
    async def scenario():
        sdk = resource()
        path = tmp_path / "activity.json"
        path.write_text(json.dumps({"old": {
            "connection_id": route()["connection_id"], "conversation_id": "C123",
            "message_ts": route()["message_ts"], "state": "active", "token": "old-token",
        }}))
        tracker = SlackActivity(sdk, path)
        await tracker.recover()
        await tracker.flush()
        assert [call.args[3] for call in sdk.remove_reaction.call_args_list] == ["eyes", "x"]
        sdk.add_reaction.assert_not_called()
        sdk.set_processing_status.assert_not_called()
        assert json.loads(path.read_text()) == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "unknown", "feature_disabled", "feature_not_enabled",
                                    "app_not_eligible", "private-token"])
def test_native_failure_does_not_block_turn_or_fall_back_to_reactions(tmp_path, failure, caplog):
    async def scenario():
        sdk = resource()
        if failure == "exception":
            sdk.set_processing_status.side_effect = RuntimeError("private-token")
        else:
            sdk.set_processing_status.return_value = NS(status="unknown" if failure == "unknown" else "failed",
                                                        error_code=failure)
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        await tracker.notify("chat", "slack", route(), "accepted")
        await tracker.notify("chat", "slack", route(), "failed")
        await tracker.flush()
        assert statuses(sdk) == ["processing", "active"]
        assert len(json.loads(tracker.state_path.read_text())) == 1
        sdk.add_reaction.assert_not_called()
        sdk.remove_reaction.assert_not_called()
        assert "private-token" not in caplog.text
        if failure in {"feature_disabled", "feature_not_enabled", "app_not_eligible"}:
            assert f"status=failed, reason={failure}" in caplog.text
        keys = [call.kwargs["idempotency_key"] for call in sdk.set_processing_status.call_args_list]
        sdk.set_processing_status.side_effect = None
        sdk.set_processing_status.return_value = NS(status="succeeded")
        restarted = SlackActivity(sdk, tracker.state_path)
        await restarted.recover()
        await restarted.flush()
        assert sdk.set_processing_status.call_args.kwargs["idempotency_key"] == keys[-1]
        assert json.loads(tracker.state_path.read_text()) == {}
    asyncio.run(scenario())


def test_incomplete_routes_and_other_channels_do_not_open_agent_threads(tmp_path):
    async def scenario():
        sdk = resource()
        tracker = SlackActivity(sdk, tmp_path / "activity.json")
        await tracker.notify("chat", "email", route(), "accepted")
        await tracker.notify("chat", "slack", {**route(), "thread_ts": None, "message_ts": None}, "accepted")
        await tracker.flush()
        assert sdk.mock_calls == []
    asyncio.run(scenario())


def test_old_sdk_does_not_break_conversations(tmp_path):
    async def scenario():
        tracker = SlackActivity(NS(), tmp_path / "activity.json")
        await tracker.recover()
        await tracker.notify("chat", "slack", route(), "accepted")
        assert not tracker._records
    asyncio.run(scenario())












def test_native_status_through_actual_sdk_http_transport(tmp_path, monkeypatch):
    import httpx
    from inkbox import Inkbox
    slack = pytest.importorskip("inkbox.slack")
    if not hasattr(slack.SlackResource, "set_processing_status"):
        pytest.skip("SDK predates native Slack status")
    requests = []
    def handle(request):
        assert request.headers["X-API-Key"] == "test-key"
        assert request.headers["Idempotency-Key"].startswith("hermes:activity:")
        assert request.url.path == f"/api/v1/slack/connections/{route()['connection_id']}/conversations/C123/processing-status"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["thread_ts"] == route()["thread_ts"]
        requests.append(body["status"])
        return httpx.Response(200, json={
            "id": "00000000-0000-4000-8000-000000000002", "connection_id": route()["connection_id"],
            "operation": "processing_status", "status": "succeeded", "conversation_id": "C123",
            "processing_status": body["status"], "agent_status": body["status"],
        })
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    async def scenario():
        with Inkbox(api_key="test-key", base_url="https://example.com") as client:
            tracker = SlackActivity(client.slack, tmp_path / "activity.json")
            for state in ("accepted", "waiting", "resumed", "completed"):
                await tracker.notify("chat", "slack", route(), state)
            await tracker.flush()
            assert requests == ["processing", "suspended", "processing", "active"]
    asyncio.run(scenario())


def test_dm_reactions_through_actual_sdk_http_transport(tmp_path, monkeypatch):
    import httpx
    from inkbox import Inkbox
    slack = pytest.importorskip("inkbox.slack")
    if not hasattr(slack.SlackResource, "add_reaction"):
        pytest.skip("SDK predates Slack reactions")
    requests = []
    keys = []
    base = f"/api/v1/slack/connections/{dm()['connection_id']}/conversations/C123/messages/{dm()['message_ts']}/reactions"
    def handle(request):
        assert request.headers["X-API-Key"] == "test-key"
        keys.append(request.headers["Idempotency-Key"])
        if request.method == "POST":
            assert request.url.path == base
            name = json.loads(request.content)["name"]
        else:
            assert request.method == "DELETE" and request.url.path.startswith(base + "/")
            name = request.url.path.rsplit("/", 1)[1]
        requests.append((request.method, name))
        return httpx.Response(200, json={
            "id": "00000000-0000-4000-8000-000000000002", "connection_id": dm()["connection_id"],
            "operation": "reaction_add" if request.method == "POST" else "reaction_remove",
            "status": "succeeded", "conversation_id": "C123", "message_ts": dm()["message_ts"],
        })
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    async def scenario():
        with Inkbox(api_key="test-key", base_url="https://example.com") as client:
            tracker = SlackActivity(client.slack, tmp_path / "activity.json")
            await tracker.notify("dm", "slack", dm(), "accepted")
            await tracker.notify("dm", "slack", dm(), "failed")
            await tracker.flush()
            assert requests == [("POST", "eyes"), ("DELETE", "x"), ("DELETE", "eyes"), ("POST", "x")]
            assert len(set(keys)) == 4
            assert all(key.startswith("hermes:activity:") and len(key) <= 128 for key in keys)
            assert json.loads(tracker.state_path.read_text()) == {}
    asyncio.run(scenario())


def test_activity_revalidates_current_identity_connection(tmp_path):
    async def run():
        resource = Mock()
        resource.list_connections.return_value = NS(connections=[])
        tracker = SlackActivity(resource, tmp_path / "activity.json", identity_id="identity")
        await tracker.notify("chat", "slack", {"connection_id": "connection", "conversation_id": "CEXAMPLE",
            "workspace_id": "TEXAMPLE", "thread_ts": "1234567890.000001", "source_event_id": "source"}, "accepted")
        await tracker.flush()
        resource.list_connections.assert_called_once_with("identity")
        resource.set_processing_status.assert_not_called()
        resource.add_reaction.assert_not_called()
        await tracker.close()
    asyncio.run(run())
