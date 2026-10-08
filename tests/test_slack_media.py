"""Slack file bytes, SDK wire, exact source ownership, and uncertain-effect fences."""
import asyncio
import base64
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from inkbox import Inkbox

from inkbox_plugin import extended_tools
from inkbox_plugin.companion import CompanionReceiver
from inkbox_plugin.config import set_runtime_config_extra
from inkbox_plugin.conversation import reply_route
from inkbox_plugin.native_turns import NativeTurns
from inkbox_plugin.slack import SLACK_MAX_UPLOAD_BYTES, SLACK_TOOLS, file_payload, run_tool
from tests import test_companion as base_harness
from tests import test_slack_companion as companion_harness
from tests.test_companion import idle, uid
from tests.test_native_turns import harness, receipt, settle

factory = base_harness.factory
slack_host = companion_harness.slack_host
IDENTITY, CONNECTION, OPERATION = uid(100), uid(40), uid(70)


def operation(status="succeeded"):
    return NS(id=OPERATION, connection_id=CONNECTION, operation="file_upload", status=status,
              conversation_id="C123", thread_ts="1234567890.000001", file_id="FEXAMPLE")


@pytest.fixture
def local_file(tmp_path):
    path = tmp_path / "diagram.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic image bytes\x00\xff")
    return path


@pytest.fixture
def client():
    client = Mock()
    client.get_identity.return_value = NS(id=IDENTITY)
    client.slack.list_connections.return_value = NS(connections=[NS(
        id=CONNECTION, identity_id=IDENTITY, status="connected", workspace_id="T123")])
    client.slack.upload_file.return_value = operation()
    client.slack.get_operation.return_value = operation()
    return client


def arguments(path, **overrides):
    return {"connection_id": CONNECTION, "conversation_id": "C123", "file_path": str(path),
            "thread_ts": "1234567890.000001", "idempotency_key": "example:upload:1", **overrides}


@pytest.mark.parametrize("thread", [None, "1234567890.000001"])
def test_local_file_and_operation_through_real_sdk_wire(local_file, monkeypatch, thread):
    requests = []
    wire_operation = vars(operation()) | {"thread_ts": thread}

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/connections"):
            assert dict(request.url.params) == {"identity_id": IDENTITY}
            return httpx.Response(200, json={"connections": [{
                "id": CONNECTION, "identity_id": IDENTITY, "workspace_id": "T123", "workspace_name": "Example",
                "status": "connected", "bot_user_id": "UAGENT", "scopes": ["files:write"],
                "created_at": "2026-01-01T00:00:00Z"}], "installation_available": False})
        if request.method == "POST":
            assert request.url.path == f"/api/v1/slack/connections/{CONNECTION}/files"
            assert request.headers["Idempotency-Key"] == "example:upload:1"
            body = json.loads(request.content)
            assert base64.b64decode(body.pop("content_base64"), validate=True) == local_file.read_bytes()
            assert body == {"conversation_id": "C123", "filename": "diagram.png", "title": "Diagram",
                            "initial_comment": "Attached", **({"thread_ts": thread} if thread else {})}
        else:
            assert request.url.path == f"/api/v1/slack/connections/{CONNECTION}/operations/{OPERATION}"
        return httpx.Response(200, json=wire_operation)

    with Inkbox(api_key="synthetic-test-key", base_url="https://api.example") as sdk:
        monkeypatch.setattr(sdk, "get_identity", lambda handle: NS(id=IDENTITY))
        sdk._api_http._client.close()
        sdk._api_http._client = httpx.Client(base_url="https://api.example/api/v1", transport=httpx.MockTransport(handle))
        args = arguments(local_file, title="Diagram", initial_comment="Attached")
        if thread is None:
            args.pop("thread_ts")
        uploaded = run_tool(sdk, "agent", "inkbox_slack_upload_file", args)
        inspected = run_tool(sdk, "agent", "inkbox_slack_get_operation", {
            "connection_id": CONNECTION, "operation_id": str(uploaded.id)})
        assert inspected.status == "succeeded" and inspected.file_id == "FEXAMPLE"
    assert sum(r.method == "POST" for r in requests) == 1


@pytest.mark.parametrize("updates", [
    {"filename": "../secret"}, {"filename": "a\\b"}, {"filename": ".."}, {"filename": "bad\nname"},
    {"filename": "x" * 256}, {"title": "x" * 256}, {"initial_comment": "x" * 12001},
    {"initial_comment": "bad\x00caption"}, {"idempotency_key": "bad key"}, {"idempotency_key": 1},
    {"file_path": "https://example.com/image.png"}, {"file_path": None}, {"unexpected": True},
])
def test_invalid_upload_arguments_never_upload(client, local_file, updates):
    with pytest.raises((ValueError, OSError)):
        run_tool(client, "agent", "inkbox_slack_upload_file", arguments(local_file, **updates))
    client.slack.upload_file.assert_not_called()


@pytest.mark.parametrize("size,valid", [(0, False), (1, True), (SLACK_MAX_UPLOAD_BYTES, True), (SLACK_MAX_UPLOAD_BYTES + 1, False)])
def test_file_size_bounds(tmp_path, size, valid):
    path = tmp_path / "sized.bin"
    with path.open("wb") as stream:
        stream.truncate(size)
    if valid:
        assert len(base64.b64decode(file_payload(str(path))["content_base64"])) == size
    else:
        with pytest.raises(ValueError, match="1 byte and 10 MiB"):
            file_payload(str(path))


def test_host_path_policy_and_missing_file(local_file):
    with pytest.raises(ValueError, match="unsafe"):
        file_payload(str(local_file), validator=lambda path: None)
    with pytest.raises(ValueError):
        file_payload(str(local_file.parent))
    with pytest.raises(ValueError):
        file_payload(str(local_file) + ".missing")


@pytest.mark.parametrize("tool", ["upload_file", "get_operation"])
@pytest.mark.parametrize("field,value", [("id", uid(99)), ("identity_id", uid(99)), ("status", "disconnected")])
def test_connection_and_identity_ownership(client, local_file, tool, field, value):
    setattr(client.slack.list_connections.return_value.connections[0], field, value)
    args = arguments(local_file) if tool == "upload_file" else {"connection_id": CONNECTION, "operation_id": OPERATION}
    with pytest.raises((ValueError, PermissionError)):
        run_tool(client, "agent", "inkbox_slack_" + tool, args)
    client.slack.upload_file.assert_not_called()
    client.slack.get_operation.assert_not_called()


@pytest.mark.parametrize("status", ["succeeded", "in_progress", "unknown", "failed"])
@pytest.mark.parametrize("tool", ["upload_file", "get_operation"])
def test_registered_tool_dispatch_preserves_operation_evidence(client, local_file, monkeypatch, status, tool):
    from inkbox_plugin import tools
    set_runtime_config_extra({"slack_enabled": True})
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(), client, client.get_identity.return_value))
    getattr(client.slack, tool).return_value = operation(status)
    specs = {}
    extended_tools.register(NS(register_tool=lambda name, group, schema, handler, **kw: specs.update({name: (schema, handler)})), lambda: True)
    args = arguments(local_file) if tool == "upload_file" else {"connection_id": CONNECTION, "operation_id": OPERATION}
    try:
        spec, handler = specs["inkbox_slack_" + tool]
        assert spec["parameters"] == next(t["inputSchema"] for t in SLACK_TOOLS if t["name"] == "inkbox_slack_" + tool)
        assert set(args) <= set(spec["parameters"]["properties"])
        result = json.loads(handler(args))
        assert result == {"ok": True, "result": vars(operation(status))}
        getattr(client.slack, tool).assert_called_once()
    finally:
        set_runtime_config_extra({})


async def reserved_native(factory, tmp_path, monkeypatch, thread=None):
    value = await harness(factory, tmp_path)
    monkeypatch.setattr(value.queue, "_kick", lambda row: None)
    incoming = receipt(mode="slack", thread=thread)
    incoming.source.chat_id = "slack:example-source"
    incoming.metadata["inkbox_reply_route"]["chat_id"] = incoming.source.chat_id
    await value.queue.accept(incoming)
    row = next(iter(value.queue.rows.values()))
    turn = row["turns"][0]
    turn["state"] = "running"
    value.queue.active[row["chat_id"]] = turn
    value.adapter._inkbox.slack.upload_file.return_value = operation()
    return value, row, turn


@pytest.mark.parametrize("method", ["send_image_file", "send_document", "send_video", "send_voice"])
@pytest.mark.parametrize("thread", [None, "1234567890.000001"])
def test_adapter_local_bytes_exact_original_route(factory, tmp_path, monkeypatch, local_file, method, thread):
    async def run():
        value, row, turn = await reserved_native(factory, tmp_path, monkeypatch, thread)
        token = reply_route.set(turn["route"])
        try:
            kwargs = {"file_name": "renamed.pdf"} if method == "send_document" else {}
            send = getattr(value.adapter, method)
            # Host context wins over caller-supplied metadata and reply_to.
            result = await send(row["chat_id"], str(local_file), caption="Caption", reply_to=uid(999),
                                metadata={"mode": "imessage", "conversation_id": "COTHER", "thread_ts": "1.1"}, **kwargs)
            assert result.success, result.error
            assert result.raw_response["file_id"] == "FEXAMPLE"
            upload = value.adapter._inkbox.slack.upload_file
            upload.assert_called_once()
            args = upload.call_args
            assert args.args == (CONNECTION,)
            assert args.kwargs["conversation_id"] == "C123" and args.kwargs["thread_ts"] == thread
            assert args.kwargs["initial_comment"] == "Caption"
            assert args.kwargs["filename"] == ("renamed.pdf" if method == "send_document" else local_file.name)
            assert base64.b64decode(args.kwargs["content_base64"]) == local_file.read_bytes()
            assert (await send(row["chat_id"], str(local_file), caption="Caption", **kwargs)).success
            upload.assert_called_once()  # Checkpointed duplicate, no SDK resend.
            value.adapter._reply_identity.send_imessage.assert_not_called()
        finally:
            reply_route.reset(token)
            await value.queue.close()
            await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("state", ["unknown", "in_progress", "failed", "timeout"])
def test_native_uncertain_upload_blocks_resend_and_restart(factory, tmp_path, monkeypatch, local_file, state):
    async def run():
        value, row, turn = await reserved_native(factory, tmp_path, monkeypatch)
        upload = value.adapter._inkbox.slack.upload_file
        if state == "timeout":
            upload.side_effect = TimeoutError("synthetic transport uncertainty")
        else:
            upload.return_value = operation(state)
        kwargs = {"reply_to": turn["id"]}
        first = await value.adapter.send_image_file(row["chat_id"], str(local_file), **kwargs)
        assert not first.success and first.raw_response["inkbox_no_retry"]
        if state != "timeout":
            assert first.raw_response["id"] == OPERATION and first.raw_response["status"] == state
        assert not (await value.adapter.send_image_file(row["chat_id"], str(local_file), **kwargs)).success
        assert not (await value.adapter.send(row["chat_id"], "no fallback", **kwargs)).success
        upload.assert_called_once()
        await value.queue.close()
        resumed = NativeTurns(value.adapter, tmp_path / "native")
        value.adapter._native_turns = resumed
        await resumed.start()
        await settle(resumed)
        assert next(iter(resumed.rows.values()))["turns"][0]["state"] == "uncertain"
        upload.assert_called_once()
        await resumed.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("fence", ["cancelled", "quarantined", "closed", "disabled", "denied", "identity", "workspace", "blocked"])
def test_native_media_fences(factory, tmp_path, monkeypatch, local_file, fence):
    async def run():
        value, row, turn = await reserved_native(factory, tmp_path, monkeypatch)
        if fence in {"cancelled", "quarantined"}:
            turn["state"] = fence
        elif fence == "closed":
            await value.queue.close()
        elif fence == "disabled":
            value.adapter._slack_enabled = False
        elif fence == "denied":
            value.base.host.denied.add(turn["route"]["author"])
        elif fence == "blocked":
            row["blocked"] = True
        else:
            connection = value.adapter._inkbox.slack.list_connections.return_value.connections[0]
            setattr(connection, "identity_id" if fence == "identity" else "workspace_id", "OTHER")
        result = await value.adapter.send_document(row["chat_id"], str(local_file), reply_to=turn["id"])
        assert not result.success
        value.adapter._inkbox.slack.upload_file.assert_not_called()
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_native_stop_during_preflight_read_fences_sdk_effect(factory, tmp_path, monkeypatch, local_file):
    async def run():
        value, row, turn = await reserved_native(factory, tmp_path, monkeypatch)
        connections = value.adapter._inkbox.slack.list_connections.return_value
        def stop(*args):
            turn["state"] = "cancelled"
            return connections
        value.adapter._inkbox.slack.list_connections.side_effect = stop
        result = await value.adapter.send_document(row["chat_id"], str(local_file), reply_to=turn["id"])
        assert not result.success
        value.adapter._inkbox.slack.upload_file.assert_not_called()
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_url_image_is_link_without_download_and_orphan_file_fails_closed(factory, tmp_path, monkeypatch, local_file):
    async def run():
        value, row, turn = await reserved_native(factory, tmp_path, monkeypatch)
        value.adapter.send = AsyncMock(return_value=NS(success=True))
        result = await value.adapter.send_image(row["chat_id"], "https://example.com/image.png", caption="Link", reply_to=turn["id"])
        assert result.success
        value.adapter.send.assert_awaited_once_with(row["chat_id"], "Link\nhttps://example.com/image.png", turn["id"], None)
        result = await value.adapter.send_image_file("slack:unknown-source", str(local_file))
        assert not result.success and result.raw_response["inkbox_no_retry"]
        value.adapter._inkbox.slack.upload_file.assert_not_called()
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("thread", [None, "1234567800.000001"])
def test_companion_media_preserves_original_thread_not_latest(slack_host, local_file, thread):
    async def run():
        value = slack_host
        value.adapter._inkbox.slack.upload_file.return_value = operation()
        await value.receiver.accept(companion_harness.incoming(thread=thread))
        await idle(value)
        original = value.inputs[0]
        await value.receiver.accept(companion_harness.incoming(4, phase="live", thread="1234567800.000002"))
        await idle(value)
        result = await value.adapter.send_document(original.source.chat_id, str(local_file), reply_to=original.message_id)
        assert result.success, result.error
        call = value.adapter._inkbox.slack.upload_file.call_args
        assert call.args == (CONNECTION,)
        assert call.kwargs["conversation_id"] == "CEXAMPLE" and call.kwargs["thread_ts"] == thread
        assert base64.b64decode(call.kwargs["content_base64"]) == local_file.read_bytes()
        await value.receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("fence", ["revoked", "paused", "denied", "stopped", "connection", "closed"])
def test_companion_media_rechecks_authority(slack_host, local_file, fence):
    async def run():
        value = slack_host
        await value.receiver.accept(companion_harness.incoming())
        await idle(value)
        original = value.inputs[0]
        row = next(iter(value.receiver.rows.values()))
        if fence == "revoked":
            value.resource.activation_messages.side_effect = PermissionError("revoked")
        elif fence == "paused":
            row["state"] = "paused"
        elif fence == "denied":
            value.host._is_user_authorized = lambda source: False
        elif fence == "stopped":
            row["turns"][0]["state"] = "quarantined"
        elif fence == "connection":
            value.adapter._inkbox.slack.list_connections.return_value.connections[0].identity_id = uid(999)
        else:
            await value.receiver.close()
        result = await value.adapter.send_document(original.source.chat_id, str(local_file), reply_to=original.message_id)
        assert not result.success
        value.adapter._inkbox.slack.upload_file.assert_not_called()
        await value.receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_cancelled_upload_keeps_uncertain_checkpoint_and_does_not_resend(factory, tmp_path, monkeypatch, local_file):
    import threading

    async def run():
        value, row, turn = await reserved_native(factory, tmp_path, monkeypatch)
        entered, release = threading.Event(), threading.Event()
        def upload(*args, **kwargs):
            entered.set()
            assert release.wait(3)
            return operation()
        value.adapter._inkbox.slack.upload_file.side_effect = upload
        task = asyncio.create_task(value.adapter.send_document(row["chat_id"], str(local_file), reply_to=turn["id"]))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            disk = json.loads((value.queue.root / (row["key"] + ".json")).read_text())
            assert next(iter(disk["turns"][0]["media_deliveries"].values()))["state"] == "sending"
            result = await value.adapter.send_document(row["chat_id"], str(local_file), reply_to=turn["id"])
            assert not result.success and result.raw_response["inkbox_no_retry"]
            value.adapter._inkbox.slack.upload_file.assert_called_once()
        finally:
            release.set()
            await value.queue.close()
            await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_changed_bytes_get_distinct_idempotency_key_without_storing_content(factory, tmp_path, monkeypatch, local_file):
    async def run():
        value, row, turn = await reserved_native(factory, tmp_path, monkeypatch)
        for content in (b"first", b"second"):
            local_file.write_bytes(content)
            result = await value.adapter.send_document(row["chat_id"], str(local_file), reply_to=turn["id"])
            assert result.success
        calls = value.adapter._inkbox.slack.upload_file.call_args_list
        assert len(calls) == 2 and calls[0].kwargs["idempotency_key"] != calls[1].kwargs["idempotency_key"]
        assert "content_base64" not in json.dumps(row)
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("tool", ["upload_file", "get_operation"])
def test_missing_sdk_api_produces_upgrade_error(client, local_file, tool):
    setattr(client.slack, tool, None)
    args = arguments(local_file) if tool == "upload_file" else {"connection_id": CONNECTION, "operation_id": OPERATION}
    with pytest.raises(RuntimeError, match="0.7.15"):
        run_tool(client, "agent", "inkbox_slack_" + tool, args)


def test_companion_unknown_media_survives_restart_without_resend(slack_host, local_file):
    async def run():
        value = slack_host
        await value.receiver.accept(companion_harness.incoming())
        await idle(value)
        original = value.inputs[0]
        value.adapter._inkbox.slack.upload_file.return_value = operation("unknown")
        for _ in range(2):
            result = await value.adapter.send_document(original.source.chat_id, str(local_file), reply_to=original.message_id)
            assert not result.success and result.raw_response["inkbox_no_retry"]
        value.adapter._inkbox.slack.upload_file.assert_called_once()
        root = value.receiver.root
        await value.receiver.close()
        receiver = CompanionReceiver(value.adapter, root, 128_000)
        await receiver.start()
        assert next(iter(receiver.rows.values()))["state"] == "paused"
        assert not receiver.tasks
        value.adapter._inkbox.slack.upload_file.assert_called_once()
        await receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())
