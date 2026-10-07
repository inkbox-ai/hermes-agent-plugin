"""Exercise native iMessage tools and gateway through the real SDK HTTP stack."""

import asyncio
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
from tests import test_companion as companion_harness

factory = companion_harness.factory


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
            if getattr(state, "on_thread_read", None):
                state.on_thread_read()
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
    reads = [request for request in sdk.requests if request.url.path.endswith((f"/messages/{SOURCE_ID}", "/thread"))]
    assert [request.url.path for request in reads] == [
        f"/api/v1/imessage/messages/{SOURCE_ID}", f"/api/v1/imessage/messages/{SOURCE_ID}/thread",
    ]
    assert reads[-1].url.params["limit"] == "1"
    assert all(request.url.params["agent_identity_id"] == IDENTITY_ID for request in reads)
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


@pytest.mark.parametrize("destination", [{"conversationId": ROOT_ID}, {"to": "+15555550103"}])
def test_deliberate_send_elsewhere_is_plain_and_does_not_consume_current_answer(sdk, destination):
    from inkbox_plugin.imessage_state import active_context
    bind()
    result = json.loads(inkbox_send_imessage({**destination, "text": "Separate instruction"}, session_id="native-session"))
    assert result["ok"]
    assert len(sends(sdk)) == 1
    body = json.loads(sends(sdk)[0].content)
    assert body.get("conversation_id") == destination.get("conversationId")
    if "to" in destination:
        assert body["to"] == destination["to"]
    assert "reply_to_message_id" not in body and "plain_reply_fallback" not in body
    assert not active_context("native-session")["explicit_sends"]
    assert not any(request.url.path.endswith("/thread") for request in sdk.requests)
    clear_context("native-session", SOURCE_ID)


@pytest.mark.parametrize("during_upload", [False, True])
def test_independent_destination_still_requires_owned_feature_during_upload(sdk, monkeypatch, during_upload):
    from inkbox_plugin import tools
    bind()
    def upload(identity, path):
        monkeypatch.setenv("INKBOX_IMESSAGE_THREADED_REPLIES", "false")
        return "https://example.com/photo.png"
    monkeypatch.setattr(tools, "_upload_imessage_media_path", upload)
    if not during_upload:
        monkeypatch.setenv("INKBOX_IMESSAGE_THREADED_REPLIES", "false")
    result = json.loads(inkbox_send_imessage({"conversationId": ROOT_ID, "text": "Separate instruction", "mediaPaths": ["photo.png"]}, session_id="native-session"))
    assert "disabled" in result["error"]
    assert not sends(sdk)
    if not during_upload:
        assert not sdk.requests
    clear_context("native-session", SOURCE_ID)


def test_other_destination_tool_send_does_not_suppress_automatic_original_answer(sdk, factory, tmp_path):
    from tests.test_native_turns import harness, receipt, settle, started
    async def run():
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate)
        sdk.source["conversation_id"] = companion_harness.uid(30)
        sdk.page["conversation_id"] = companion_harness.uid(30)
        value.adapter._reply_identity = sdk.client.get_identity("agent")
        await value.queue.accept(receipt(3))
        await started(value)
        turn = next(iter(value.queue.rows.values()))["turns"][0]
        result = json.loads(inkbox_send_imessage({"conversationId": ROOT_ID, "text": "answer " + SOURCE_ID}, session_id=turn["session_id"]))
        assert result["ok"] and not turn.get("explicit_sends")
        gate.set()
        await settle(value.queue)
        bodies = [json.loads(request.content) for request in sends(sdk)]
        assert len(value.inputs) == 1 and len(bodies) == 2
        assert bodies[0]["conversation_id"] == ROOT_ID and "reply_to_message_id" not in bodies[0]
        assert bodies[1]["conversation_id"] == companion_harness.uid(30) and bodies[1]["reply_to_message_id"] == SOURCE_ID
        assert turn["state"] == "done" and turn["sent"]["content"] == bodies[0]["text"]
        await value.queue.close()
    asyncio.run(run())


def test_other_destination_cannot_spoof_source_target_before_reads_or_media_upload(sdk):
    bind()
    result = json.loads(inkbox_send_imessage({"conversationId": ROOT_ID, "reply_to_message_id": SOURCE_ID, "text": "Answer", "mediaPaths": ["/tmp/private.png"]}, session_id="native-session"))
    assert "gateway" in result["error"]
    assert not sdk.requests
    clear_context("native-session", SOURCE_ID)


def test_unrelated_send_still_rechecks_native_owner_after_client_lookup(sdk, monkeypatch):
    from inkbox_plugin import tools
    bind()
    original = tools._client_and_identity
    def lookup():
        result = original()
        clear_context("native-session", SOURCE_ID)
        return result
    monkeypatch.setattr(tools, "_client_and_identity", lookup)
    result = json.loads(inkbox_send_imessage({"conversationId": ROOT_ID, "text": "Separate instruction"}, session_id="native-session"))
    assert "error" in result
    assert not sends(sdk)


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


def test_positive_new_native_turn_reuses_session_plainly_but_old_tool_stays_fenced(sdk, monkeypatch):
    import sys
    import types
    from inkbox_plugin.imessage_state import observe_host_turn
    bind()
    clear_context("native-session", SOURCE_ID)
    current = {"source": SOURCE_ID}
    session_context = types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda *args: current["source"]
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    observe_host_turn("native-session", "proactive-source")
    old = json.loads(inkbox_send_imessage({"conversation_id": CONVERSATION_ID, "text": "Stale"}, session_id="native-session"))
    assert "error" in old and not sends(sdk)
    current["source"] = "proactive-source"
    result = json.loads(inkbox_send_imessage({"conversation_id": CONVERSATION_ID, "text": "Fresh proactive"}, session_id="native-session"))
    assert "error" not in result
    payload = json.loads(sends(sdk)[0].content)
    assert "reply_to_message_id" not in payload
    assert "plain_reply_fallback" not in payload


@pytest.mark.parametrize("failure", ["source-conversation", "source-id", "thread-conversation", "backend-404"])
@pytest.mark.parametrize("media", [False, True])
def test_backend_reply_preflight_fails_before_upload_or_send(sdk, tmp_path, failure, media):
    bind()
    if failure == "source-conversation":
        sdk.source["conversation_id"] = ROOT_ID
    elif failure == "source-id":
        sdk.source["id"] = ROOT_ID
    elif failure == "thread-conversation":
        sdk.page["conversation_id"] = ROOT_ID
    else:
        sdk.thread_status = 404
    args = {"conversation_id": CONVERSATION_ID, "text": "Answer"}
    if media:
        attachment = tmp_path / "photo.png"
        attachment.write_bytes(b"synthetic attachment")
        args["mediaPaths"] = [str(attachment)]
    result = json.loads(inkbox_send_imessage(args, session_id="native-session"))
    assert "no send attempted" in result["error"]
    assert not any(request.method != "GET" for request in sdk.requests)
    clear_context("native-session", SOURCE_ID)


def test_native_owner_cancellation_during_backend_preflight_prevents_send(sdk):
    bind()
    sdk.on_thread_read = lambda: clear_context("native-session", SOURCE_ID)
    result = json.loads(inkbox_send_imessage({"conversation_id": CONVERSATION_ID, "text": "Answer"}, session_id="native-session"))
    assert "error" in result
    assert not sends(sdk)


@pytest.mark.parametrize("during_lookup", [False, True])
def test_disabling_native_replies_cannot_turn_an_owned_reply_into_plain_send(sdk, monkeypatch, during_lookup):
    from inkbox_plugin import tools
    bind()
    if during_lookup:
        original = tools._client_and_identity
        def lookup():
            result = original()
            monkeypatch.setenv("INKBOX_IMESSAGE_THREADED_REPLIES", "false")
            return result
        monkeypatch.setattr(tools, "_client_and_identity", lookup)
    else:
        monkeypatch.setenv("INKBOX_IMESSAGE_THREADED_REPLIES", "false")
    result = json.loads(inkbox_send_imessage({"conversation_id": CONVERSATION_ID, "text": "Answer"}, session_id="native-session"))
    assert "disabled" in result["error"]
    assert not sends(sdk)
    clear_context("native-session", SOURCE_ID)


def test_unadmitted_reply_target_is_rejected_before_backend_reads(sdk):
    from inkbox_plugin.imessage_state import validate_reply_target
    identity = sdk.client.get_identity("agent")
    sdk.requests.clear()
    meta = {"conversation_id": CONVERSATION_ID, **source_metadata(message())}
    meta["imessage_reply_target"] = ROOT_ID
    with pytest.raises(RuntimeError, match="not an admitted source"):
        validate_reply_target(identity, meta)
    assert not sdk.requests


@pytest.mark.parametrize("companion", [False, True])
@pytest.mark.parametrize("available", [False, True])
def test_automatic_native_and_companion_reply_preflight_uses_real_sdk(sdk, factory, tmp_path, companion, available):
    from tests.test_native_turns import harness, receipt, settle
    async def run():
        sdk.source["conversation_id"] = companion_harness.uid(30)
        sdk.page["conversation_id"] = companion_harness.uid(30)
        sdk.thread_status = 200 if available else 404
        identity = sdk.client.get_identity("agent")
        if companion:
            value = factory("imessage")
            value.adapter._imessage_threaded_replies = True
            value.adapter._reply_identity = identity
            value.adapter._inkbox.get_identity.return_value = identity
            await value.receiver.accept(companion_harness.event("imessage"))
            await companion_harness.idle(value)
            event = value.inputs[0]
            result = await value.adapter.send(event.source.chat_id, "Answer", reply_to=event.message_id)
            assert result.success is available
            turn = next(iter(value.receiver.rows.values()))["turns"][0]
            assert turn["delivery"]["state"] == ("sent" if available else "pending")
            await value.receiver.close()
        else:
            value = await harness(factory, tmp_path)
            value.adapter._reply_identity = identity
            await value.queue.accept(receipt(3))
            await settle(value.queue)
            turn = next(iter(value.queue.rows.values()))["turns"][0]
            assert turn["state"] == ("done" if available else "answer_ready")
            await value.queue.close()
        assert len(sends(sdk)) == int(available)
        source_reads = [request for request in sdk.requests if request.url.path.endswith(f"/messages/{SOURCE_ID}")]
        thread_reads = [request for request in sdk.requests if request.url.path.endswith("/thread")]
        assert source_reads and thread_reads
        assert all(request.url.params["limit"] == "1" for request in thread_reads)
        assert all(request.url.params["agent_identity_id"] == IDENTITY_ID for request in source_reads + thread_reads)
        if available:
            assert json.loads(sends(sdk)[0].content)["reply_to_message_id"] == SOURCE_ID
    asyncio.run(run())
