"""Exercise native iMessage tools and gateway through the real SDK HTTP stack."""

import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from inkbox import Inkbox
from inkbox import _config as sdk_config
from inkbox.agent_identity import AgentIdentity

from inkbox_plugin.imessage_state import bind_context, clear_context, source_metadata
from inkbox_plugin.tools import inkbox_send_imessage
from inkbox_plugin.extended_tools import dispatch
from inkbox_plugin.config import set_runtime_config_extra


pytestmark = pytest.mark.skipif(
    not hasattr(AgentIdentity, "get_imessage_thread"),
    reason="The installed SDK does not yet expose native iMessage reply APIs",
)

IDENTITY_ID = str(UUID(int=1))
CONVERSATION_ID = str(UUID(int=2))
SOURCE_ID = str(UUID(int=3))
OUTBOUND_ID = str(UUID(int=4))
THREAD_ID = str(UUID(int=5))
ROOT_ID = str(UUID(int=6))
PARENT_ID = str(UUID(int=7))
TIMESTAMP = "2026-01-01T00:00:00+00:00"


def message(**changes):
    return {
        "id": SOURCE_ID, "conversation_id": CONVERSATION_ID, "assignment_id": None,
        "direction": "inbound", "content": "Question", "message_type": "message",
        "service": "imessage", "is_read": False, "status": "received",
        "created_at": TIMESTAMP, "updated_at": TIMESTAMP,
        "reply_to_message_id": None, "thread_id": None, "thread_root_message_id": None,
        **changes,
    }


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    """Use real SDK clients/serializers with synthetic credentials and no network."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("INKBOX_IMESSAGE_THREADED_REPLIES", "true")
    monkeypatch.setenv("INKBOX_BASE_URL", "https://example.com")
    monkeypatch.setenv("INKBOX_VAULT_KEY", "")
    monkeypatch.setenv("INKBOX_API_KEY", "ApiKey_synthetic_test_only")
    monkeypatch.setenv("INKBOX_IDENTITY", "agent")
    set_runtime_config_extra({})
    monkeypatch.setattr(sdk_config, "_CONFIG_PATH", tmp_path / "unused-sdk-config")
    state = SimpleNamespace(
        requests=[], source=message(),
        outbound=message(id=OUTBOUND_ID, direction="outbound", content="Answer", status="pending"),
        thread_status=200, send_status=201,
        page={
            "conversation_id": CONVERSATION_ID, "thread_id": None, "thread_root_message_id": None,
            "messages": [message()], "next_cursor": "next:opaque+/=",
        },
    )

    def handle(request):
        assert request.url.host == "example.com"
        state.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/identities/agent":
            return httpx.Response(200, json={
                "id": IDENTITY_ID, "organization_id": "org_example", "agent_handle": "agent",
                "imessage_enabled": True, "created_at": TIMESTAMP, "updated_at": TIMESTAMP,
            })
        if request.method == "GET" and path == f"/api/v1/imessage/messages/{SOURCE_ID}":
            return httpx.Response(200, json=state.source)
        if request.method == "GET" and path in {
            f"/api/v1/imessage/messages/{SOURCE_ID}/thread",
            f"/api/v1/imessage/conversations/{CONVERSATION_ID}/threads/{THREAD_ID}",
        }:
            if state.thread_status != 200:
                return httpx.Response(state.thread_status, json={"detail": "Thread endpoint unavailable"})
            return httpx.Response(200, json=state.page)
        if request.method == "POST" and path == "/api/v1/imessage/messages":
            if state.send_status != 201:
                return httpx.Response(state.send_status, json={"detail": {"error": "imessage_reply_target_unavailable"}})
            return httpx.Response(201, json={"message": state.outbound})
        raise AssertionError(f"Unexpected mocked request: {request.method} {path}")

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    client = Inkbox(api_key="ApiKey_synthetic_test_only", base_url="https://example.com")
    state.client = client
    yield state
    client.close()


def bind():
    bind_context("native-session", {"conversation_id": CONVERSATION_ID, "turn_id": SOURCE_ID, **source_metadata(message())})


def sends(sdk):
    return [request for request in sdk.requests if request.method == "POST"]


def test_sdk_source_target_fallback_identity_and_idempotency_on_wire(sdk):
    bind()
    result = json.loads(inkbox_send_imessage({"conversationId": CONVERSATION_ID, "text": "Answer"}, session_id="native-session"))
    assert result["ok"]
    assert len(sends(sdk)) == 1
    request = sends(sdk)[0]
    body = json.loads(request.content)
    assert body["conversation_id"] == CONVERSATION_ID
    assert body["reply_to_message_id"] == SOURCE_ID
    assert body["plain_reply_fallback"] is True
    assert request.url.params["agent_identity_id"] == IDENTITY_ID
    assert request.headers["Idempotency-Key"].startswith("hermes:tool:")
    clear_context("native-session", SOURCE_ID)


@pytest.mark.parametrize("field,value", [("reply_to_message_id", ROOT_ID), ("plain_reply_fallback", False), ("threadId", THREAD_ID)])
def test_injected_target_rejected_before_any_sdk_read(sdk, field, value):
    result = json.loads(inkbox_send_imessage({"conversationId": CONVERSATION_ID, "text": "Answer", field: value}))
    assert "gateway" in result["error"]
    assert not sdk.requests


def test_proactive_send_has_no_ancestry_or_stale_context(sdk):
    result = json.loads(inkbox_send_imessage({"conversationId": CONVERSATION_ID, "text": "Fresh"}))
    assert result["ok"]
    body = json.loads(sends(sdk)[0].content)
    assert "reply_to_message_id" not in body


def test_wrong_conversation_is_rejected_before_reads_or_media_upload(sdk):
    bind()
    result = json.loads(inkbox_send_imessage({"conversationId": ROOT_ID, "text": "Answer", "mediaPaths": ["/tmp/private.png"]}, session_id="native-session"))
    assert "original conversation" in result["error"]
    assert not sdk.requests
    clear_context("native-session", SOURCE_ID)


def test_send_rejection_does_not_retry_plainly(sdk):
    bind()
    sdk.send_status = 409
    result = json.loads(inkbox_send_imessage({"conversationId": CONVERSATION_ID, "text": "Answer"}, session_id="native-session"))
    assert "error" in result
    assert len(sends(sdk)) == 1
    assert json.loads(sends(sdk)[0].content)["reply_to_message_id"] == SOURCE_ID
    clear_context("native-session", SOURCE_ID)


@pytest.mark.parametrize("name,args", [
    ("inkbox_get_imessage_thread", {"message_id": SOURCE_ID}),
    ("inkbox_get_imessage_conversation_thread", {"conversation_id": CONVERSATION_ID, "thread_id": THREAD_ID}),
])
def test_real_sdk_thread_pages_preserve_null_and_cursor(sdk, name, args):
    bind()
    result = json.loads(dispatch(name, {**args, "limit": 2, "cursor": "opaque:+/="}, session_id="native-session"))
    assert result["ok"]
    page = result["result"]
    assert page["thread_id"] is None and page["thread_root_message_id"] is None
    assert page["next_cursor"] == "next:opaque+/="
    assert page["messages"][0]["reply_to_message_id"] is None
    request = sdk.requests[-1]
    assert request.url.params["limit"] == "2" and request.url.params["cursor"] == "opaque:+/="
    assert request.url.params["agent_identity_id"] == IDENTITY_ID
    clear_context("native-session", SOURCE_ID)


def test_companion_cannot_expand_native_thread_history(sdk):
    bind_context("companion-session", {"conversation_id": CONVERSATION_ID, "turn_id": SOURCE_ID, "companion": True})
    result = json.loads(dispatch("inkbox_get_imessage_thread", {"message_id": SOURCE_ID}, session_id="companion-session"))
    assert "supplied Companion history" in result["error"]
    assert not any(request.url.path.endswith("/thread") for request in sdk.requests)
    clear_context("companion-session", SOURCE_ID)
