"""Slack source resolution uses exact SDK reads and preserves receipt isolation."""

from copy import deepcopy
from dataclasses import make_dataclass
from types import SimpleNamespace as NS
from typing import Literal
from unittest.mock import Mock
from uuid import UUID

import pytest

from inkbox_plugin.slack_companion import prepare_envelope, require_sdk_support


IDENTITY = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"
SOURCE = "00000000-0000-4000-8000-000000000003"
OTHER = "00000000-0000-4000-8000-000000000004"
TIMESTAMP = "1791000000.000001"


@pytest.fixture
def receipt():
    return {
        "id": "evt-example", "event_type": "slack.mention_received",
        "data": {
            "identity_id": IDENTITY, "connection_id": CONNECTION, "workspace_id": "TINSTALL",
            "conversation_id": "CEXAMPLE", "message_ts": TIMESTAMP, "thread_ts": None,
            "actor_id": "UALICE", "actor_profile": {"id": "UALICE", "team_id": "THOME"},
            "event": {"type": "message", "text": "Please help"},
        },
    }


@pytest.fixture
def client():
    resource = Mock()
    resource.list_connections.return_value = NS(connections=[NS(
        id=UUID(CONNECTION), identity_id=UUID(IDENTITY), workspace_id="TINSTALL",
        status="connected", bot_user_id="UAGENT",
    )])
    resource.list_archived_messages.return_value = NS(messages=[NS(
        id=UUID(SOURCE), connection_id=UUID(CONNECTION), conversation_id="CEXAMPLE",
        message_ts=TIMESTAMP, thread_ts=None, user_id="UALICE", source="event",
    )], next_cursor=None)
    resource.get_user.return_value = {"id": "UALICE", "team_id": "THOME"}
    return NS(get_identity=Mock(return_value=NS(id=UUID(IDENTITY))), slack=resource)


def test_exact_current_source_and_external_home_workspace(client, receipt):
    original = deepcopy(receipt)
    result = prepare_envelope(client, "example-agent", receipt)
    assert receipt == original and result is not receipt
    assert result["_hermes_slack_source"] == {
        "id": SOURCE, "author": "THOME:UALICE", "thread_ts": None, "bot_user_id": "UAGENT",
    }
    client.get_identity.assert_called_once_with("example-agent")
    client.slack.list_connections.assert_called_once_with(UUID(IDENTITY))
    client.slack.list_archived_messages.assert_called_once_with(
        CONNECTION, conversation_id="CEXAMPLE", after_ts="1791000000.000000",
        before_ts="1791000000.000002", limit=2,
    )
    client.slack.get_user.assert_not_called()


def test_never_accepts_received_local_normalization(client, receipt):
    receipt["_hermes_slack_source"] = {"id": OTHER, "author": "TINSTALL:UOTHER"}
    result = prepare_envelope(client, "example-agent", receipt)
    assert result["_hermes_slack_source"]["id"] == SOURCE
    assert result["_hermes_slack_source"]["author"] == "THOME:UALICE"
    assert receipt["_hermes_slack_source"]["id"] == OTHER


@pytest.mark.parametrize("profile", [None, {}, {"id": "UOTHER", "team_id": "TINSTALL"},
                                     {"id": "UALICE", "team_id": None},
                                     {"id": "UALICE", "team_id": "bad-workspace"}])
def test_missing_or_unusable_profile_uses_direct_sdk_user_dictionary(client, receipt, profile):
    receipt["data"]["actor_profile"] = profile
    assert prepare_envelope(client, "agent", receipt)["_hermes_slack_source"]["author"] == "THOME:UALICE"
    client.slack.get_user.assert_called_once_with(CONNECTION, "UALICE")


@pytest.mark.parametrize("profile", [{}, {"user": {"id": "UALICE", "team_id": "THOME"}},
                                     {"id": "UOTHER", "team_id": "THOME"},
                                     {"id": "UALICE", "team_id": "not-a-workspace"}])
def test_does_not_fall_back_to_installation_workspace(client, receipt, profile):
    receipt["data"].pop("actor_profile")
    client.slack.get_user.return_value = profile
    with pytest.raises(ValueError, match="home workspace"):
        prepare_envelope(client, "agent", receipt)


def test_identity_mismatch_stops_before_connection_or_source_reads(client, receipt):
    client.get_identity.return_value.id = UUID(OTHER)
    with pytest.raises(ValueError, match="configured identity"):
        prepare_envelope(client, "agent", receipt)
    client.slack.list_connections.assert_not_called()
    client.slack.list_archived_messages.assert_not_called()


@pytest.mark.parametrize("method", ["list_connections", "list_archived_messages", "get_user"])
def test_missing_sdk_source_lookup_capabilities_are_clear_errors(client, receipt, method):
    setattr(client.slack, method, None)
    with pytest.raises(ValueError, match="archive and user reads"):
        prepare_envelope(client, "agent", receipt)


@pytest.mark.parametrize("field,value", [("identity_id", OTHER), ("workspace_id", "TOTHER"),
                                       ("status", "disconnected"), ("bot_user_id", "invalid")])
def test_connection_must_match_current_identity_and_workspace(client, receipt, field, value):
    setattr(client.slack.list_connections.return_value.connections[0], field, value)
    with pytest.raises(ValueError):
        prepare_envelope(client, "agent", receipt)
    client.slack.list_archived_messages.assert_not_called()


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_connection_is_rejected(client, receipt, count):
    client.slack.list_connections.return_value.connections *= count
    with pytest.raises(ValueError, match="uniquely"):
        prepare_envelope(client, "agent", receipt)
    client.slack.list_archived_messages.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("id", "not-a-uuid"), ("connection_id", OTHER), ("conversation_id", "COTHER"),
    ("message_ts", "1791000000.000002"), ("message_ts", 1791000000.000001),
    ("user_id", "UBOB"), ("source", "backfill"), ("source", "action"),
    ("thread_ts", "1790999999.000001"),
])
def test_source_must_match_exact_native_receipt(client, receipt, field, value):
    setattr(client.slack.list_archived_messages.return_value.messages[0], field, value)
    with pytest.raises(ValueError):
        prepare_envelope(client, "agent", receipt)
    client.slack.get_user.assert_not_called()


@pytest.mark.parametrize("count,cursor", [(0, None), (2, None), (1, "page-two"), (0, "page-two")])
def test_missing_ambiguous_or_nonterminal_source_lookup_fails_closed(client, receipt, count, cursor):
    page = client.slack.list_archived_messages.return_value
    page.messages *= count
    page.next_cursor = cursor
    with pytest.raises(ValueError, match="missing, ambiguous, or incomplete"):
        prepare_envelope(client, "agent", receipt)
    assert client.slack.list_archived_messages.call_count == 1


@pytest.mark.parametrize("thread", [None, TIMESTAMP, "1790999999.123456"])
def test_preserves_actual_thread_scope_and_normalizes_native_parent(client, receipt, thread):
    receipt["data"]["thread_ts"] = thread
    client.slack.list_archived_messages.return_value.messages[0].thread_ts = thread
    expected = thread if thread != TIMESTAMP else None
    assert prepare_envelope(client, "agent", receipt)["_hermes_slack_source"]["thread_ts"] == expected


@pytest.mark.parametrize("field,value", [
    ("identity_id", "bad"), ("connection_id", None), ("workspace_id", "THOME:UALICE"),
    ("conversation_id", "../other"), ("actor_id", "bad"), ("message_ts", 1791000000.1),
    ("message_ts", "NaN"), ("message_ts", "1791000000.0000001"), ("thread_ts", ""),
])
def test_invalid_wire_coordinates_do_not_make_sdk_requests(client, receipt, field, value):
    receipt["data"][field] = value
    with pytest.raises(ValueError):
        prepare_envelope(client, "agent", receipt)
    client.get_identity.assert_not_called()


def test_microsecond_window_crosses_second_without_float_rounding(client, receipt):
    receipt["data"]["message_ts"] = "1791000000.000000"
    client.slack.list_archived_messages.return_value.messages[0].message_ts = "1791000000.000000"
    prepare_envelope(client, "agent", receipt)
    assert client.slack.list_archived_messages.call_args.kwargs == {
        "conversation_id": "CEXAMPLE", "after_ts": "1790999999.999999",
        "before_ts": "1791000000.000001", "limit": 2,
    }


@pytest.mark.parametrize("channels,context_fields,works", [
    (Literal["mail", "phone", "imessage"], ["conversation_id"], False),
    (Literal["mail", "slack"], ["conversation_id"], False),
    (Literal["mail", "slack"], ["connection_id", "slack_conversation_id", "thread_ts"], True),
])
def test_capability_checks_slack_channel_and_parsed_reply_fields(monkeypatch, channels, context_fields, works):
    import inkbox.companion as companion

    monkeypatch.setattr(companion, "CompanionChannel", channels)
    monkeypatch.setattr(companion, "CompanionReplyContext", make_dataclass("Reply", context_fields))
    if works:
        require_sdk_support()
    else:
        with pytest.raises(ValueError, match="0.7.14"):
            require_sdk_support()


def test_preparation_uses_real_sdk_archive_and_user_wire(monkeypatch, receipt):
    pytest.importorskip("inkbox.slack_operations", reason="requires the Slack archive SDK")
    import httpx
    from inkbox import Inkbox

    requests = []
    receipt["data"].pop("actor_profile")

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/slack/connections"):
            payload = {"connections": [{
                "id": CONNECTION, "identity_id": IDENTITY, "workspace_id": "TINSTALL",
                "workspace_name": "Example", "bot_user_id": "UAGENT", "status": "connected",
                "scopes": [], "created_at": "2026-01-01T00:00:00Z",
            }], "installation_available": False}
        elif request.url.path.endswith("/archive/messages"):
            payload = {"messages": [{
                "id": SOURCE, "connection_id": CONNECTION, "conversation_id": "CEXAMPLE",
                "message_ts": TIMESTAMP, "thread_ts": None, "user_id": "UALICE",
                "text": "Please help", "files": [], "mentioned": True,
                "source": "event", "captured_at": "2026-01-01T00:00:00Z",
            }], "next_cursor": None}
        elif request.url.path.endswith("/users/UALICE"):
            payload = {"id": "UALICE", "team_id": "THOME"}
        else:
            pytest.fail(f"Unexpected request path: {request.url.path}")
        return httpx.Response(200, json=payload)

    client = Inkbox(api_key="synthetic-test-key", base_url="https://api.example")
    monkeypatch.setattr(client, "get_identity", lambda handle: NS(id=UUID(IDENTITY)))
    client._api_http._client.close()
    client._api_http._client = httpx.Client(
        base_url="https://api.example/api/v1", transport=httpx.MockTransport(handle),
    )
    try:
        result = prepare_envelope(client, "example-agent", receipt)
        assert result["_hermes_slack_source"]["id"] == SOURCE
        assert result["_hermes_slack_source"]["author"] == "THOME:UALICE"
        assert [request.url.path for request in requests] == [
            "/api/v1/slack/connections",
            f"/api/v1/slack/connections/{CONNECTION}/archive/messages",
            f"/api/v1/slack/connections/{CONNECTION}/users/UALICE",
        ]
        assert dict(requests[0].url.params) == {"identity_id": IDENTITY}
        assert dict(requests[1].url.params) == {
            "conversation_id": "CEXAMPLE", "after_ts": "1791000000.000000",
            "before_ts": "1791000000.000002", "limit": "2",
        }
    finally:
        client.close()
