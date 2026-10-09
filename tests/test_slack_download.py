"""Download cached Slack previews through the native tool and real SDK wire."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import httpx
import pytest
from inkbox import Inkbox

from inkbox_plugin import extended_tools, slack
from inkbox_plugin.config import set_runtime_config_extra

IDENTITY = "00000000-0000-0000-0000-000000000100"
CONNECTION = "00000000-0000-0000-0000-000000000040"
TOOL = "inkbox_slack_download_file_preview"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


def arguments(**overrides):
    return {"connection_id": CONNECTION, "file_id": "FEXAMPLE", **overrides}


@pytest.fixture
def client():
    client = Mock()
    client.get_identity.return_value = NS(id=IDENTITY)
    client.slack.list_connections.return_value = NS(connections=[NS(
        id=CONNECTION, identity_id=IDENTITY, status="connected", workspace_id="T123")])
    client.slack.download_file_preview.return_value = PNG
    return client


@pytest.fixture
def cache(monkeypatch, tmp_path):
    import gateway.platforms.base as host

    def save(data, ext):
        path = tmp_path / ("preview" + ext)
        path.write_bytes(data)
        return str(path)

    writer = Mock(side_effect=save)
    monkeypatch.setattr(host, "cache_image_from_bytes", writer, raising=False)
    return writer


def test_real_sdk_wire_saves_local_preview(monkeypatch, cache):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/connections"):
            assert dict(request.url.params) == {"identity_id": IDENTITY}
            return httpx.Response(200, json={"connections": [{
                "id": CONNECTION, "identity_id": IDENTITY, "workspace_id": "T123", "workspace_name": "Example",
                "status": "connected", "bot_user_id": "UAGENT", "scopes": ["files:read"],
                "created_at": "2026-01-01T00:00:00Z"}], "installation_available": False})
        assert request.url.path == f"/api/v1/slack/connections/{CONNECTION}/files/FEXAMPLE/preview"
        assert request.headers["accept"] == "image/*"
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    with Inkbox(api_key="synthetic-test-key", base_url="https://api.example") as sdk:
        monkeypatch.setattr(sdk, "get_identity", lambda handle: NS(id=IDENTITY))
        sdk._api_http._client.close()
        sdk._api_http._client = httpx.Client(base_url="https://api.example/api/v1", transport=httpx.MockTransport(handle))
        result = slack.run_tool(sdk, "agent", TOOL, arguments())
    assert Path(result["file_path"]).read_bytes() == PNG
    assert result["file_id"] == "FEXAMPLE"
    assert result["connection_id"] == CONNECTION
    assert result["mimetype"] == "image/png"
    assert result["size_bytes"] == len(PNG)
    assert result["preview_only"] is True
    assert "vision" in result["next_step"].lower()
    assert len(requests) == 2
    cache.assert_called_once_with(PNG, ext=".png")


@pytest.mark.parametrize("updates", [
    {"file_id": "../F123"}, {"file_id": "https://example.com/image.png"}, {"file_id": "F123\n"},
    {"file_id": " F123"}, {"file_id": "f123"}, {"file_id": ""}, {"file_id": 123},
    {"file_id": "F" + "A" * 64}, {"file_path": "/arbitrary/output"}, {"url": "https://example.com"},
    {"connection_id": "not-a-uuid"}, {"connection_id": " " + CONNECTION},
])
def test_invalid_arguments_fail_before_sdk(client, cache, updates):
    with pytest.raises(ValueError):
        slack.run_tool(client, "agent", TOOL, arguments(**updates))
    client.get_identity.assert_not_called()
    client.slack.download_file_preview.assert_not_called()
    cache.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("id", "00000000-0000-0000-0000-000000000099"),
    ("identity_id", "00000000-0000-0000-0000-000000000099"), ("status", "disconnected"),
])
def test_connection_must_be_owned_and_active(client, cache, field, value):
    setattr(client.slack.list_connections.return_value.connections[0], field, value)
    with pytest.raises(PermissionError):
        slack.run_tool(client, "agent", TOOL, arguments())
    client.slack.download_file_preview.assert_not_called()
    cache.assert_not_called()


@pytest.mark.parametrize("data", [b"", b"<html>private error</html>", b"%PDF-1.7", b"<svg/>", None])
def test_invalid_preview_never_cached(client, cache, data):
    client.slack.download_file_preview.return_value = data
    with pytest.raises(ValueError):
        slack.run_tool(client, "agent", TOOL, arguments())
    cache.assert_not_called()


def test_oversize_preview_never_cached(client, cache):
    client.slack.download_file_preview.return_value = PNG + bytes(slack.SLACK_MAX_PREVIEW_BYTES)
    with pytest.raises(ValueError, match="2 MiB"):
        slack.run_tool(client, "agent", TOOL, arguments())
    cache.assert_not_called()


def test_missing_sdk_method(client, cache):
    client.slack.download_file_preview = None
    with pytest.raises(RuntimeError, match="upgrade"):
        slack.run_tool(client, "agent", TOOL, arguments())
    cache.assert_not_called()


@pytest.mark.parametrize("status", [403, 404, 409, 429, 503])
def test_sdk_error_propagates_without_local_file(monkeypatch, cache, status):
    with Inkbox(api_key="synthetic-test-key", base_url="https://api.example") as sdk:
        monkeypatch.setattr(sdk, "get_identity", lambda handle: NS(id=IDENTITY))
        monkeypatch.setattr(sdk.slack, "list_connections", lambda identity: NS(connections=[NS(
            id=CONNECTION, identity_id=IDENTITY, status="connected")]))
        sdk._api_http._client.close()
        sdk._api_http._client = httpx.Client(base_url="https://api.example/api/v1", transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"detail": {"code": "preview_unavailable"}})))
        with pytest.raises(Exception) as error:
            slack.run_tool(sdk, "agent", TOOL, arguments())
        assert error.value.status_code == status
    cache.assert_not_called()


@pytest.mark.parametrize("status,retry_after", [(404, None), (503, "5"), (429, "30")])
def test_dispatch_preserves_status_and_retry_guidance(client, monkeypatch, cache, status, retry_after):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(), client, client.get_identity.return_value))
    client.slack.download_file_preview.side_effect = InkboxAPIError(
        status_code=status, detail={"code": "preview_unavailable"}, retry_after=retry_after)
    try:
        set_runtime_config_extra({"slack_enabled": True})
        result = json.loads(extended_tools.dispatch(TOOL, arguments()))
        assert result["status_code"] == status
        assert result.get("retry_after_seconds") == (int(retry_after) if retry_after is not None else None)
        assert "file_path" not in result
        cache.assert_not_called()
        client.slack.download_file_preview.assert_called_once()
    finally:
        set_runtime_config_extra({})


@pytest.mark.parametrize("data,ext,mimetype", [
    (PNG, ".png", "image/png"), (b"\xff\xd8\xff" + bytes(20), ".jpg", "image/jpeg"),
    (b"GIF89a" + bytes(20), ".gif", "image/gif"),
    (b"RIFF" + bytes(4) + b"WEBP" + bytes(20), ".webp", "image/webp"),
])
def test_preview_format_detection(client, cache, data, ext, mimetype):
    # Synthetic headers test dispatch only; image integrity is checked by the API.
    client.slack.download_file_preview.return_value = data
    result = slack.run_tool(client, "agent", TOOL, arguments())
    assert result["mimetype"] == mimetype
    assert Path(result["file_path"]).suffix == ext
    cache.assert_called_once_with(data, ext=ext)


def test_registered_tool_dispatch_and_enablement(client, monkeypatch, cache):
    from inkbox_plugin import tools
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(), client, client.get_identity.return_value))
    specs = {}
    extended_tools.register(NS(register_tool=lambda name, group, schema, handler, **kw:
                              specs.update({name: (schema, handler, kw["check_fn"])})), lambda: True)
    try:
        set_runtime_config_extra({"slack_enabled": False})
        schema, handler, available = specs[TOOL]
        assert not available()
        assert "disabled" in json.loads(handler(arguments()))["error"]
        client.slack.download_file_preview.assert_not_called()
        set_runtime_config_extra({"slack_enabled": True})
        assert available()
        assert schema["parameters"] == next(t["inputSchema"] for t in slack.SLACK_TOOLS if t["name"] == TOOL)
        result = json.loads(handler(arguments()))
        assert result["ok"]
        assert Path(result["result"]["file_path"]).read_bytes() == PNG
        assert "content_base64" not in result["result"]
    finally:
        set_runtime_config_extra({})
