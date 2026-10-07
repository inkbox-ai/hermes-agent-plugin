"""Slack SDK wire, ownership, subscription, and exact reply-route contracts."""
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from inkbox_plugin.slack import SLACK_SUBSCRIPTION_EVENTS, inbound_message, reconcile_subscription, run_tool, send_reply
IDENTITY = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"


def event(**overrides):
    result = {"id": "evt_1", "event_type": "slack.mention_received", "data": {
        "identity_id": IDENTITY, "connection_id": CONNECTION, "workspace_id": "T_TEST",
        "conversation_id": "C_TEST", "message_ts": "1234567890.000001", "actor_id": "U_ALICE",
        "thread_ts": None, "message_kinds": ["channel", "mention"],
        "event": {"type": "message", "text": "<@U_AGENT> hello"},
    }}
    result["data"].update(overrides)
    return result

def test_ordinary_dms_share_context_but_explicit_threads_are_isolated():
    top = inbound_message(event(conversation_id="D_TEST", message_kinds=["dm"]), IDENTITY)
    threaded = inbound_message(event(conversation_id="D_TEST", message_kinds=["dm", "thread"],
                                     thread_ts="1234567890.000001"), IDENTITY)
    assert top[0] != threaded[0]
    assert top[2]["thread_ts"] is None
    assert threaded[2]["thread_ts"] == "1234567890.000001"
    assert top[2]["conversation_kind"] == threaded[2]["conversation_kind"] == "direct"
    new_request = inbound_message(event(conversation_id="D_TEST", message_kinds=["dm"],
                                        message_ts="1234567890.000002"), IDENTITY)
    assert new_request[0] == top[0]
    assert new_request[2]["thread_ts"] is None
    thread_followup = inbound_message(event(
        conversation_id="D_TEST", message_kinds=["dm", "thread"],
        thread_ts="1234567890.000001", message_ts="1234567890.000003",
    ), IDENTITY)
    assert thread_followup[0] == threaded[0]
    other_dm = inbound_message(event(conversation_id="D_OTHER", message_kinds=["dm"]), IDENTITY)
    assert other_dm[0] != top[0]
    other_workspace = inbound_message(event(connection_id="another-connection"), IDENTITY)
    assert other_workspace[0] != inbound_message(event(), IDENTITY)[0]

def test_native_dm_mention_starts_thread_and_followups_keep_its_context():
    mentioned = inbound_message(event(
        conversation_id="D_TEST", message_kinds=["dm", "mention"],
    ), IDENTITY)
    followup = inbound_message(event(
        conversation_id="D_TEST", message_kinds=["dm", "thread"],
        thread_ts="1234567890.000001", message_ts="1234567890.000002",
    ), IDENTITY)
    ordinary = inbound_message(event(
        conversation_id="D_TEST", message_kinds=["dm"], message_ts="1234567890.000003",
    ), IDENTITY)
    assert mentioned[2]["thread_ts"] == "1234567890.000001"
    assert mentioned[0] == followup[0] != ordinary[0]
    another_mention = inbound_message(event(
        conversation_id="D_TEST", message_kinds=["dm", "mention"],
        message_ts="1234567890.000004",
    ), IDENTITY)
    assert another_mention[0] != mentioned[0]

@pytest.mark.parametrize("actor", [None, "invalid", {"id": "U_OTHER", "profile": {"email": "other@example.com"}}])
def test_absent_or_mismatched_actor_profile_does_not_change_routing(actor):
    incoming = inbound_message(event(actor_profile=actor, contact_id=None), IDENTITY)
    assert "slack_sender_context" not in incoming[2]
    assert incoming[2]["sender"] == "T_TEST:U_ALICE"

@pytest.mark.parametrize("via_tool", [False, True])
@pytest.mark.parametrize("text,valid", [("x" * 12000, True), ("x" * 12001, False), ("hello\x00world", False)])
def test_send_respects_api_text_boundary(via_tool, text, valid):
    client = Mock()
    client.get_identity.return_value = NS(id=IDENTITY)
    client.slack.list_connections.return_value = NS(connections=[NS(id=CONNECTION)])
    client.slack.send_message.return_value = NS(status="sent")

    def send():
        if via_tool:
            return run_tool(client, "agent", "inkbox_slack_send_message", {
                "connection_id": CONNECTION, "conversation_id": "CTEST", "text": text,
                "idempotency_key": "boundary-test",
            })
        return send_reply(client, inbound_message(event(), IDENTITY)[2], text)

    if valid:
        send()
        assert client.slack.send_message.call_count == 1
        assert client.slack.send_message.call_args.kwargs["text"] == text
    else:
        with pytest.raises(ValueError):
            send()
        client.slack.send_message.assert_not_called()

def test_subscription_reconciliation_does_not_remove_other_receivers():
    client = Mock()
    unrelated = NS(id="other", url="https://other.example/webhook", event_types=["slack.mention_received"])
    client.webhooks.subscriptions.list.return_value = [unrelated]
    reconcile_subscription(client, IDENTITY, "https://agent.example/webhook?channel=slack")
    client.webhooks.subscriptions.list.assert_called_once_with(
        agent_identity_id=IDENTITY, scope="identity", url="https://agent.example/webhook?channel=slack")
    created = client.webhooks.subscriptions.create.call_args.kwargs
    assert created == {
        "agent_identity_id": IDENTITY,
        "url": "https://agent.example/webhook?channel=slack",
        "event_types": ["slack.dm_received", "slack.group_dm_received",
                        "slack.channel_message_received", "slack.mention_received",
                        "slack.thread_reply_received", "slack.session_stopped"],
    }
    assert set(created["event_types"]) == set(SLACK_SUBSCRIPTION_EVENTS)
    assert "slack.channel_message_received" in created["event_types"]
    client.webhooks.subscriptions.delete.assert_not_called()
    existing = NS(id="ours", status="active", **created)
    client.webhooks.subscriptions.list.return_value = [unrelated, existing]
    reconcile_subscription(client, IDENTITY, existing.url)
    assert client.webhooks.subscriptions.create.call_count == 1
    client.webhooks.subscriptions.update.assert_not_called()

@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("upgrade", [False, True])
def test_subscription_uses_current_sdk_wire_and_reuses_migrated_selection(mixed, upgrade):
    slack = pytest.importorskip("inkbox.slack", reason="requires the Slack-capable SDK preview")
    if not hasattr(slack.SlackResource, "list_provisioning_workspaces"):
        pytest.skip("installed SDK predates identity-wide subscriptions")
    import httpx
    from inkbox import Inkbox

    requests, rows = [], []
    url = "https://agent.example/webhook?channel=slack"
    row = {
        "id": "00000000-0000-4000-8000-000000000003", "organization_id": "org_test",
        "agent_identity_id": IDENTITY, "url": url, "status": "active",
        "event_types": list(reversed(SLACK_SUBSCRIPTION_EVENTS)),
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    }
    if mixed or upgrade:
        selected = [event for event in row["event_types"]
                    if not upgrade or event != "slack.channel_message_received"]
        rows.append({**row, "event_types": [*selected, *(["message.received"] if mixed else [])]})

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/v1/webhooks/catalog":
            return httpx.Response(200, json={"supports_identity_subscriptions": True})
        if request.method == "PATCH":
            assert request.url.path == "/api/v1/webhooks/subscriptions/" + row["id"]
            assert dict(request.url.params) == {"scope": "identity"}
            assert json.loads(request.content) == {
                "event_types": [*rows[0]["event_types"], "slack.channel_message_received"],
            }
            rows[0] = {**rows[0], **json.loads(request.content)}
            return httpx.Response(200, json=rows[0])
        assert request.url.path == "/api/v1/webhooks/subscriptions"
        if request.method == "GET":
            assert request.url.params["agent_identity_id"] == IDENTITY
            assert request.url.params["scope"] == "identity"
            assert request.url.params["url"] == url
            return httpx.Response(200, json={"subscriptions": rows})
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "agent_identity_id": IDENTITY, "url": url,
            "event_types": list(SLACK_SUBSCRIPTION_EVENTS),
        }
        rows.append(row)
        return httpx.Response(201, json=rows[0])

    with Inkbox(api_key="synthetic-test-key", base_url="https://api.example") as client:
        client._api_http._client.close()
        client._api_http._client = httpx.Client(
            base_url="https://api.example/api/v1", transport=httpx.MockTransport(handle),
        )
        reconcile_subscription(client, IDENTITY, url)
        reconcile_subscription(client, IDENTITY, url)
    assert [request.method for request in requests] == (
        ["GET", "GET", "PATCH", "GET", "GET"] if upgrade else
        ["GET", "GET", "GET", "GET"] if mixed else ["GET", "GET", "POST", "GET", "GET"])

@pytest.mark.parametrize("split", [False, True])
def test_subscription_coverage_reuses_mixed_and_split_event_sets(split):
    client = Mock()
    url = "https://agent.example/webhook?channel=slack"
    first, *remaining = SLACK_SUBSCRIPTION_EVENTS
    rows = [NS(id="ours", url=url, status="active", event_types=["message.received", first])]
    if split:
        rows.append(NS(url=url, status="active", event_types=remaining))
    client.webhooks.subscriptions.list.return_value = rows
    reconcile_subscription(client, IDENTITY, url)
    if split:
        client.webhooks.subscriptions.create.assert_not_called()
        client.webhooks.subscriptions.update.assert_not_called()
    else:
        client.webhooks.subscriptions.update.assert_called_once_with(
            "ours", event_types=["message.received", first, *remaining], scope="identity")
        client.webhooks.subscriptions.create.assert_not_called()
    client.webhooks.subscriptions.delete.assert_not_called()

def test_paused_subscription_is_not_bypassed_by_new_registration():
    client = Mock()
    url = "https://agent.example/webhook?channel=slack"
    client.webhooks.subscriptions.list.return_value = [
        NS(url=url, status="paused", event_types=["message.received", "slack.dm_received"])]
    with pytest.raises(RuntimeError, match="paused"):
        reconcile_subscription(client, IDENTITY, url)
    client.webhooks.subscriptions.create.assert_not_called()
    client.webhooks.subscriptions.update.assert_not_called()

def test_tools_and_replies_match_real_slack_sdk_wire(monkeypatch):
    pytest.importorskip("inkbox.slack", reason="requires the Slack-capable SDK preview")
    import httpx
    from inkbox import Inkbox

    requests = []
    connection = {
        "id": CONNECTION, "identity_id": IDENTITY, "workspace_id": "TTEST",
        "workspace_name": "Example", "bot_user_id": "UAGENT", "status": "connected",
        "scopes": [], "created_at": "2026-01-01T00:00:00Z",
    }
    action = {
        "id": "00000000-0000-4000-8000-000000000003", "connection_id": CONNECTION,
        "status": "sent", "conversation_id": "CTEST", "message_ts": "1234567890.000003",
        "thread_ts": "1234567890.000001",
    }

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/connections"):
            payload = {"connections": [connection], "installation_available": False}
        elif path.endswith("/conversations"):
            payload = {"conversations": [{"id": "CTEST"}], "next_cursor": "page-2"}
        elif request.method == "POST" or "/actions/" in path:
            payload = action
        else:
            payload = {"messages": [], "next_cursor": "page-2", "has_more": True}
        return httpx.Response(200, json=payload)

    client = Inkbox(api_key="synthetic-test-key", base_url="https://api.example")
    monkeypatch.setattr(client, "get_identity", lambda handle: NS(id=IDENTITY))
    client._api_http._client.close()
    client._api_http._client = httpx.Client(
        base_url="https://api.example/api/v1", transport=httpx.MockTransport(handle),
    )
    try:
        for name, args in [
            ("list_connections", {}),
            ("list_conversations", {"connection_id": CONNECTION}),
            ("list_messages", {"connection_id": CONNECTION, "conversation_id": "CTEST",
                               "thread_ts": "1234567890.000001", "cursor": "page-1"}),
            ("search", {"q": "release"}),
            ("send_message", {"connection_id": CONNECTION, "conversation_id": "CTEST",
                              "text": "Hello", "idempotency_key": "test-1"}),
            ("get_action", {"connection_id": CONNECTION, "action_id": action["id"]}),
        ]:
            run_tool(client, "agent", "inkbox_slack_" + name, args)
        search = next(r for r in requests if r.url.path.endswith("/search"))
        assert dict(search.url.params) == {"q": "release", "identity_id": IDENTITY, "limit": "50"}
        history = next(r for r in requests if r.method == "GET" and r.url.path.endswith("/messages"))
        assert history.url.params["thread_ts"] == "1234567890.000001"
        assert history.url.params["cursor"] == "page-1"
        send = next(r for r in requests if r.method == "POST")
        assert send.headers["Idempotency-Key"] == "test-1"
        assert json.loads(send.content) == {"conversation_id": "CTEST", "text": "Hello"}
        send_reply(client, inbound_message(event(conversation_id="CTEST"), IDENTITY)[2], "Reply")
        assert json.loads(requests[-1].content)["thread_ts"] == "1234567890.000001"
    finally:
        client.close()
