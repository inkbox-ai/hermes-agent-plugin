"""Cached capability discovery and explicit host tool refresh."""
from __future__ import annotations

import json
from contextlib import ExitStack
import os
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from .config import inkbox_state_path, plugin_user_agent, read_runtime_config

_LOCK = threading.Lock()
_CACHE_KEY = None
_EXPIRES = 0.0
_SNAPSHOT = {}
_SMS_READS = {
    "inkbox_list_text_conversations", "inkbox_get_text_conversation", "inkbox_list_texts",
    "inkbox_get_text", "inkbox_mark_text_read", "inkbox_mark_text_conversation_read",
}
_SLACK_WRITES = {"inkbox_slack_send_message", "inkbox_slack_upload_file"}
_VAULT_READS = {"inkbox_get_vault_secret", "inkbox_get_totp_code"}
_WHATSAPP_WRITES = {
    "inkbox_send_whatsapp", "inkbox_upload_whatsapp_media", "inkbox_send_whatsapp_reaction", "inkbox_remove_whatsapp_reaction",
}


def _discovery_client(cfg):
    # Resource-only construction avoids the SDK client's automatic Vault unlock.
    from inkbox._config import resolve_client_settings
    from inkbox._http import HttpTransport, sdk_user_agent
    from inkbox.identities.resources.identities import IdentitiesResource
    from inkbox.slack import SlackResource
    from inkbox.vault.resources.vault import VaultResource
    api_key, base_url, _ = resolve_client_settings(api_key=cfg.api_key, base_url=cfg.base_url or None, vault_key="")
    base_url = (base_url or "https://inkbox.ai").rstrip("/")
    if not base_url.startswith("https://") and urlsplit(base_url).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Capability discovery requires HTTPS")
    cleanup = ExitStack()
    def transport(suffix):
        http = HttpTransport(api_key=api_key, base_url=base_url + "/api/v1" + suffix,
                             timeout=3, user_agent=sdk_user_agent(plugin_user_agent()))
        cleanup.callback(http.close)
        return http
    try:
        return SimpleNamespace(get_identity=IdentitiesResource(transport("/identities")).get,
                               slack=SlackResource(transport("")), vault=VaultResource(transport("/vault")),
                               close=cleanup.close)
    except Exception:
        cleanup.close()
        raise


def snapshot(*, force=False):
    global _CACHE_KEY, _EXPIRES, _SNAPSHOT

    cfg = read_runtime_config()
    key = (str(inkbox_state_path().parent.resolve()), cfg.api_key, cfg.base_url, cfg.identity, cfg.slack_enabled)
    with _LOCK:
        if not force and key == _CACHE_KEY and time.monotonic() < _EXPIRES:
            return dict(_SNAPSHOT)
        result = dict(sms_history=None, sms_send=None, slack_history=None, slack_send=None,
                      whatsapp_history=None, whatsapp_send=None, vault_exists=None, errors=[])
        if not cfg.api_key or not cfg.identity:
            result["errors"] = ["Inkbox is not configured"]
        else:
            client = None
            try:
                client = _discovery_client(cfg)
                identity = None
                try:
                    identity = client.get_identity(cfg.identity)
                    number = identity.phone_number
                    result["sms_history"] = number is not None
                    result["sms_send"] = (number is not None and getattr(number, "status", None) == "active"
                                          and getattr(number, "sms_status", None) == "ready")
                    result["whatsapp_history"] = identity.whatsapp_configured is True
                    result["whatsapp_send"] = result["whatsapp_history"] and identity.whatsapp_enabled is True
                except Exception:
                    result["errors"].append("Identity availability could not be checked")
                if cfg.slack_enabled and identity is not None:
                    try:
                        connections = [row for row in client.slack.list_connections(str(identity.id)).connections
                                       if str(row.identity_id) == str(identity.id)]
                        result["slack_history"] = bool(connections)
                        result["slack_send"] = any(row.status == "connected" for row in connections)
                    except Exception:
                        result["errors"].append("Slack availability could not be checked")
                try:
                    result["vault_exists"] = client.vault.info() is not None
                except Exception:
                    result["errors"].append("Vault availability could not be checked")
            except Exception:
                result["errors"].append("Inkbox availability could not be checked")
            finally:
                if client is not None:
                    client.close()
        _CACHE_KEY, _EXPIRES, _SNAPSHOT = key, time.monotonic() + 60, result
        return dict(result)


def available(name):
    cfg = read_runtime_config()
    if not cfg.api_key or not cfg.identity:
        return False
    if name.startswith("inkbox_slack_"):
        if not cfg.slack_enabled:
            return False
        if name == "inkbox_slack_list_connections":
            return True
        state = snapshot()
        return state["slack_send" if name in _SLACK_WRITES else "slack_history"] is not False
    if name in _VAULT_READS:
        return bool(os.getenv("INKBOX_HERMES_VAULT_KEY")) and snapshot()["vault_exists"] is not False
    if name in _SMS_READS or name == "inkbox_send_sms":
        state = snapshot()
        return state["sms_send" if name == "inkbox_send_sms" else "sms_history"] is not False
    if name.startswith("inkbox_") and "whatsapp" in name:
        if name == "inkbox_get_whatsapp_status":
            return True
        state = snapshot()
        return state["whatsapp_send" if name in _WHATSAPP_WRITES else "whatsapp_history"] is not False
    if name in {"inkbox_get_imessage_thread", "inkbox_get_imessage_conversation_thread"}:
        return cfg.imessage_threaded_replies
    return True


def check(name):
    def enabled():
        return available(name)
    try:
        from tools.registry import no_cache_check_fn
        return no_cache_check_fn(enabled)
    except ImportError:
        return enabled


def refresh_host_tools(*, session_id="", current_session_only=False):
    from tools.mcp_tool_agent import refresh_agent_mcp_tools, reprobe_tool_availability
    reprobe_tool_availability()
    home = inkbox_state_path().parent.resolve()
    targets, errors, seen = [], [], set()

    def publish(agent, surface):
        if agent is None or id(agent) in seen:
            return False
        if current_session_only and (not session_id or str(agent.session_id) != session_id):
            return False
        seen.add(id(agent))
        try:
            refresh_agent_mcp_tools(agent, quiet_mode=True)
        except Exception:
            errors.append(f"One {surface} session could not be refreshed")
            return False
        targets.append(surface)
        return True

    module = sys.modules.get("gateway.run")
    runner = getattr(module, "_gateway_runner_ref", lambda: None)()
    if runner is not None:
        from gateway.session_authorities import authority_for_home
        from gateway.session import _session_key_namespace
        authority = authority_for_home(runner, home)
        cache_lock = getattr(runner, "_agent_cache_lock", None)
        if authority is not None and cache_lock is not None:
            registry = getattr(runner, "session_authorities", None)
            profile = registry.profile_name(authority) if registry is not None else None
            prefix = _session_key_namespace(profile) + ":" if runner.config.multiplex_profiles else None
            with cache_lock:
                entries = [entry for key, entry in runner._agent_cache.items() if prefix is None or str(key).startswith(prefix)]
            for entry in entries:
                publish(entry[0] if isinstance(entry, tuple) else entry, "gateway")
    server = sys.modules.get("tui_gateway.server")
    if server is not None:
        from hermes_constants import get_process_hermes_home, hermes_home_key
        with server._sessions_lock:
            sessions = [(sid, item) for sid, item in server._sessions.items() if item.get("agent") is not None
                        and hermes_home_key(item.get("profile_home") or get_process_hermes_home()) == hermes_home_key(home)]
        for sid, session in sessions:
            try:
                with server._session_profile_runtime_scope(session):
                    if publish(session["agent"], "open_chats"):
                        server._emit("session.info", sid, server._session_info(session["agent"], session))
            except Exception:
                errors.append("One open-chat session could not finish refreshing")
    for module_name in ("cli", "__main__"):
        agent = getattr(sys.modules.get(module_name), "_active_agent_ref", None)
        publish(agent, "cli")
    return {"sessions_refreshed": len(targets), "refreshed": sorted(set(targets)), "refresh_errors": errors}


def refresh(*, session_id="", current_session_only=False):
    try:
        state = snapshot(force=True)
    except Exception:
        return {"ok": False, "error": "Capability discovery failed; verify local Inkbox configuration."}
    state["vault_key_present"] = bool(os.getenv("INKBOX_HERMES_VAULT_KEY"))
    try:
        host = refresh_host_tools(session_id=session_id, current_session_only=current_session_only)
    except Exception:
        return {"ok": False, "capabilities": state, "error": "Capabilities checked, but the host tool refresh failed."}
    return {"ok": not host["refresh_errors"], "capabilities": state, **host,
            "message": "Available tools were rebuilt for the reported sessions; no gateway restart is required." if host["sessions_refreshed"] else
                       "Discovery refreshed, but no live session snapshot was updated. Use /inkbox refresh in an active chat."}


REFRESH_SCHEMA = {"name": "inkbox_refresh_capabilities",
                  "description": "Recheck identity/channel and Vault availability and refresh this session's tool catalog without restarting. Call after connecting or disconnecting a channel. Never provide credentials as arguments.",
                  "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}


def refresh_tool(args, *, session_id="", **_kwargs):
    if args:
        return json.dumps({"error": "No arguments are accepted"})
    return json.dumps(refresh(session_id=session_id, current_session_only=True))
