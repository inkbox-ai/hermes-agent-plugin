import json
from pathlib import Path
import sys
import threading
from contextlib import nullcontext
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from inkbox_plugin import capabilities as caps
from inkbox_plugin.config import InkboxPluginConfig

REAL_DISCOVERY_CLIENT = caps._discovery_client


@pytest.fixture
def discovery(monkeypatch, tmp_path):
    cfg = InkboxPluginConfig(api_key="synthetic-agent-key", identity="example-agent", slack_enabled=True)
    identity = NS(id="identity-one", phone_number=None, whatsapp_configured=False, whatsapp_enabled=False)
    client = Mock()
    client.get_identity.return_value = identity
    client.slack.list_connections.return_value = NS(connections=[])
    client.vault.info.return_value = None
    factory = Mock()
    factory.return_value = client
    monkeypatch.setattr(caps, "_discovery_client", factory)
    monkeypatch.setattr(caps, "read_runtime_config", lambda: cfg)
    monkeypatch.setattr(caps, "inkbox_state_path", lambda: tmp_path / "inkbox_identity_state.json")
    monkeypatch.setattr(caps, "_CACHE_KEY", None)
    monkeypatch.setattr(caps, "_SNAPSHOT", {})
    monkeypatch.delenv("INKBOX_HERMES_VAULT_KEY", raising=False)
    return cfg, identity, client, factory


def test_missing_capabilities_preserve_discovery_and_refresh(discovery):
    for name in ("inkbox_send_sms", "inkbox_get_text", "inkbox_slack_send_message", "inkbox_slack_list_messages",
                 "inkbox_get_vault_secret", "inkbox_get_totp_code", "inkbox_send_whatsapp", "inkbox_get_whatsapp_message"):
        assert not caps.available(name), name
    for name in ("inkbox_refresh_capabilities", "inkbox_whoami", "inkbox_list_vault_secrets",
                 "inkbox_slack_list_connections", "inkbox_get_whatsapp_status", "inkbox_lookup_message_send"):
        assert caps.available(name), name


@pytest.mark.parametrize("status,ready", [("ready", True), ("pending", False), ("assignment_failed", False)])
def test_sms_readiness_only_hides_sending(discovery, status, ready):
    _, identity, _, _ = discovery
    identity.phone_number = NS(status="active", sms_status=status)
    assert caps.available("inkbox_send_sms") is ready
    assert caps.available("inkbox_list_texts")
    assert caps.available("inkbox_mark_text_read")


@pytest.mark.parametrize("status,send", [("connected", True), ("disconnected", False), ("reauthorization_required", False)])
def test_slack_retained_connection_keeps_history_and_recovery(discovery, status, send):
    _, identity, client, _ = discovery
    client.slack.list_connections.return_value = NS(connections=[NS(identity_id=identity.id, status=status)])
    assert caps.available("inkbox_slack_send_message") is send
    assert caps.available("inkbox_slack_upload_file") is send
    for name in ("inkbox_slack_get_action", "inkbox_slack_get_operation", "inkbox_slack_download_file_preview", "inkbox_slack_list_messages"):
        assert caps.available(name)


def test_slack_respects_identity_and_local_opt_out(discovery):
    cfg, _, client, _ = discovery
    client.slack.list_connections.return_value = NS(connections=[NS(identity_id="other", status="connected")])
    assert not caps.available("inkbox_slack_send_message")
    cfg.slack_enabled = False
    assert not caps.available("inkbox_slack_list_connections")
    assert not caps.available("inkbox_slack_get_operation")


def test_vault_requires_metadata_and_local_unlock_key_without_unlocking(discovery, monkeypatch):
    _, _, client, _ = discovery
    client.vault.info.return_value = NS(id="vault-one")
    assert not caps.available("inkbox_get_vault_secret")
    monkeypatch.setenv("INKBOX_HERMES_VAULT_KEY", "synthetic-local-key")
    assert caps.available("inkbox_get_vault_secret")
    assert caps.available("inkbox_get_totp_code")
    client.vault.unlock.assert_not_called()
    client.vault.list_secrets.assert_not_called()
    monkeypatch.delenv("INKBOX_HERMES_VAULT_KEY")
    assert not caps.available("inkbox_get_vault_secret")
    assert caps.available("inkbox_list_vault_secrets")


def test_failed_probes_do_not_hide_other_channels_or_possible_history(discovery):
    _, identity, client, _ = discovery
    identity.phone_number = NS(status="active", sms_status="ready")
    client.slack.list_connections.side_effect = RuntimeError("synthetic-secret-not-for-output")
    client.vault.info.side_effect = RuntimeError("synthetic-secret-not-for-output")
    assert caps.available("inkbox_send_sms")
    assert caps.available("inkbox_slack_send_message")
    assert caps.available("inkbox_slack_get_operation")
    assert "synthetic-secret-not-for-output" not in json.dumps(caps.snapshot())


def test_force_refresh_detects_connect_disconnect_and_caches_probes(discovery):
    _, identity, client, _ = discovery
    assert not caps.available("inkbox_send_sms")
    identity.phone_number = NS(status="active", sms_status="ready")
    assert not caps.available("inkbox_send_sms")
    caps.snapshot(force=True)
    assert caps.available("inkbox_send_sms")
    assert client.get_identity.call_count == 2
    identity.phone_number = None
    caps.snapshot(force=True)
    assert not caps.available("inkbox_send_sms")


@pytest.mark.parametrize("field", ["identity", "api_key", "base_url"])
def test_identity_and_credentials_changes_do_not_reuse_prior_capabilities(discovery, field):
    cfg, identity, client, _ = discovery
    identity.phone_number = NS(status="active", sms_status="ready")
    assert caps.available("inkbox_send_sms")
    setattr(cfg, field, "new-value")
    identity.phone_number = None
    assert not caps.available("inkbox_send_sms")
    assert client.get_identity.call_count == 2


def test_profile_home_change_rechecks_discovery(discovery, monkeypatch):
    _, _, client, _ = discovery
    caps.snapshot()
    monkeypatch.setattr(caps, "inkbox_state_path", lambda: Path("/synthetic/other-profile/state.json"))
    caps.snapshot()
    assert client.get_identity.call_count == 2


def test_refresh_command_and_tool_use_current_session_without_accepting_credentials(discovery, monkeypatch):
    from inkbox_plugin.cli import slash_handler
    refresh = Mock(return_value={"ok": True})
    monkeypatch.setattr(caps, "refresh", refresh)
    assert json.loads(slash_handler("refresh", session_id="current"))["ok"]
    refresh.assert_called_once_with(session_id="current")
    assert "error" in json.loads(caps.refresh_tool({"api_key": "not-accepted"}))
    assert refresh.call_count == 1
    caps.refresh_tool({}, session_id="current")
    assert refresh.call_count == 2


def test_refresh_reprobes_and_scopes_native_gateway_and_cli_refresh(discovery, monkeypatch):
    reprobe, refresh_agent = Mock(), Mock()
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_agent", NS(
        reprobe_tool_availability=reprobe, refresh_agent_mcp_tools=refresh_agent))
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    authority = object()
    agent, sibling, cli_agent = NS(session_id="current"), NS(session_id="unrelated"), NS(session_id="cli-session")
    runner = NS(config=NS(multiplex_profiles=True), session_authorities=NS(profile_name=lambda item: "profile-one"),
                _agent_cache_lock=threading.Lock(), _agent_cache={"agent:profile-one:current": (agent, "sig"),
                                                               "agent:other:unrelated": (sibling, "sig")})
    monkeypatch.setitem(sys.modules, "gateway.session_authorities", NS(
        authority_for_home=lambda candidate, home: authority if candidate is runner else None))
    monkeypatch.setitem(sys.modules, "gateway.run", NS(_gateway_runner_ref=lambda: runner))
    monkeypatch.setitem(sys.modules, "gateway.session", NS(_session_key_namespace=lambda profile: "agent:" + profile))
    monkeypatch.setitem(sys.modules, "cli", NS(_active_agent_ref=cli_agent))
    result = caps.refresh(session_id="current")
    assert result["ok"] and result["refreshed"] == ["cli", "gateway"]
    assert result["sessions_refreshed"] == 2
    reprobe.assert_called_once()
    assert [call.args[0] for call in refresh_agent.call_args_list] == [agent, cli_agent]
    assert all(call.kwargs == {"quiet_mode": True} for call in refresh_agent.call_args_list)
    refresh_agent.reset_mock()
    result = caps.refresh(session_id="current", current_session_only=True)
    assert result["ok"] and result["sessions_refreshed"] == 1
    refresh_agent.assert_called_once_with(agent, quiet_mode=True)


def test_refresh_reports_host_failure_instead_of_claiming_success(discovery, monkeypatch):
    monkeypatch.setattr(caps, "refresh_host_tools", Mock(side_effect=RuntimeError("synthetic-private-detail")))
    result = caps.refresh()
    assert result["ok"] is False
    assert "synthetic-private-detail" not in json.dumps(result)


def test_failed_client_discovery_is_cached_as_unknown_not_permanent_absence(discovery):
    _, _, _, factory = discovery
    factory.side_effect = ValueError("synthetic-private-config")
    assert caps.available("inkbox_send_sms")
    assert caps.available("inkbox_send_whatsapp")
    assert caps.snapshot()["sms_send"] is None
    assert factory.call_count == 1
    assert "synthetic-private-config" not in json.dumps(caps.snapshot())


@pytest.mark.parametrize("slack_enabled", [False, True])
def test_real_sdk_discovery_never_unlocks_or_fetches_secret_payloads(discovery, monkeypatch, slack_enabled):
    import httpx
    from tests.fixtures.vault_api import VaultAPI
    cfg, _, _, _ = discovery
    cfg.base_url, cfg.slack_enabled = "https://api.example.com", slack_enabled
    api = VaultAPI()
    def respond(request):
        if request.url.path == "/api/v1/slack/connections":
            from tests.fixtures.vault_api import IDENTITY_ID
            api.requests.append(request)
            assert request.method == "GET" and request.url.params["identity_id"] == IDENTITY_ID
            assert request.headers["X-API-Key"] == "synthetic-agent-key"
            return httpx.Response(200, json={"connections": [], "installation_available": True})
        return api.respond(request)
    monkeypatch.setenv("INKBOX_VAULT_KEY", "synthetic-global-key-must-not-be-used")
    monkeypatch.setattr(caps, "_discovery_client", REAL_DISCOVERY_CLIENT)
    monkeypatch.setattr("inkbox._http.httpx.HTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    result = caps.snapshot(force=True)
    assert result["vault_exists"] is True
    assert result["sms_history"] is False
    assert result["errors"] == []
    if slack_enabled:
        assert result["slack_history"] is False and result["slack_send"] is False
    assert [request.url.path for request in api.requests] == ["/api/v1/identities/example-agent",
        *(["/api/v1/slack/connections"] if slack_enabled else []), "/api/v1/vault/info"]


def test_open_chat_refresh_isolated_continues_after_failure_and_emits_ui_update(discovery, monkeypatch):
    first, second, foreign = NS(session_id="first"), NS(session_id="second"), NS(session_id="foreign")
    refresh_agent = Mock(side_effect=[RuntimeError("synthetic-private-error"), set()])
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_agent", NS(
        reprobe_tool_availability=Mock(), refresh_agent_mcp_tools=refresh_agent))
    monkeypatch.setitem(sys.modules, "gateway.run", NS(_gateway_runner_ref=lambda: None))
    monkeypatch.setitem(sys.modules, "cli", NS(_active_agent_ref=None))
    home = caps.inkbox_state_path().parent
    monkeypatch.setitem(sys.modules, "hermes_constants", NS(
        get_process_hermes_home=lambda: home, hermes_home_key=lambda value: str(Path(value).resolve())))
    server = NS(_sessions_lock=threading.Lock(), _sessions={
        "first": {"agent": first, "profile_home": home}, "second": {"agent": second, "profile_home": home},
        "foreign": {"agent": foreign, "profile_home": home / "other"}},
        _session_profile_runtime_scope=lambda session: nullcontext(), _emit=Mock(), _session_info=lambda *args: {"tools": []})
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    result = caps.refresh()
    assert result["ok"] is False and result["sessions_refreshed"] == 1
    assert [call.args[0] for call in refresh_agent.call_args_list] == [first, second]
    server._emit.assert_called_once_with("session.info", "second", {"tools": []})
    assert "synthetic-private-error" not in json.dumps(result)
    server._sessions.clear()
    result = caps.refresh()
    assert result["ok"] and result["sessions_refreshed"] == 0 and result["refreshed"] == []
    assert "no live session" in result["message"]
