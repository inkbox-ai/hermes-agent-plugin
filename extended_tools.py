"""Optional SDK tools registered through the native Hermes tool catalog."""
from __future__ import annotations

import json
import os
from uuid import UUID

from .config import inkbox_client_kwargs, read_runtime_config
from .slack import SLACK_TOOLS, run_tool


def _schema(name, description, properties, required=()):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False,
    }}


_ID = {"type": "string", "format": "uuid"}
VAULT_TOOLS = [
    _schema("inkbox_list_vault_secrets", "List accessible Vault metadata without credential values or unlocking the Vault.", {
        "secret_type": {"type": "string", "enum": ["login", "api_key", "key_pair", "ssh_key", "other"]},
    }),
    _schema("inkbox_get_vault_secret", "Decrypt one accessible Vault secret. Requires INKBOX_HERMES_VAULT_KEY in the local environment, never in chat. Login TOTP seeds are omitted; use inkbox_get_totp_code for 2FA.", {"secret_id": _ID}, ["secret_id"]),
    _schema("inkbox_get_totp_code", "Get a login secret's current 2FA code and expiry, without its password or TOTP seed. Requires INKBOX_HERMES_VAULT_KEY locally.", {"secret_id": _ID}, ["secret_id"]),
]
_PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
         "cursor": {"type": "string", "minLength": 1, "maxLength": 1024}}
THREAD_TOOLS = [
    _schema("inkbox_get_imessage_thread", "Read one bounded native iMessage reply-thread page, oldest first, from a visible message ID. Nullable ancestry is preserved; follow next_cursor.", {"message_id": {"type": "string", "minLength": 1}, **_PAGE}, ["message_id"]),
    _schema("inkbox_get_imessage_conversation_thread", "Read one bounded iMessage thread page within its conversation. An opaque thread ID is not a root message ID.", {"conversation_id": {"type": "string", "minLength": 1}, "thread_id": {"type": "string", "minLength": 1}, **_PAGE}, ["conversation_id", "thread_id"]),
]


def _vault_access(rules, identity_id, secret_id):
    return any(str(rule.identity_id) == str(identity_id) and str(rule.vault_secret_id) == str(secret_id) for rule in rules)


def dispatch(name, args, *, session_id="", **_kwargs):
    # Lazy import avoids a circular dependency with the native registration entrypoint.
    from .tools import _client_and_identity, _json_safe
    client = None
    try:
        cfg = read_runtime_config()
        if name.startswith("inkbox_slack_") and not cfg.slack_enabled:
            raise ValueError("Slack is disabled; configure INKBOX_SLACK_ENABLED first")
        if name in {tool["name"] for tool in THREAD_TOOLS} and not cfg.imessage_threaded_replies:
            raise ValueError("Native iMessage replies are disabled")
        spec = next((tool for tool in VAULT_TOOLS + THREAD_TOOLS if tool["name"] == name), None)
        if spec:
            schema = spec["parameters"]
            if set(args) - set(schema["properties"]) or any(key not in args for key in schema["required"]):
                raise ValueError("Invalid tool arguments")
        # Validate identifiers before constructing a client or reading a secret.
        if name in {"inkbox_get_vault_secret", "inkbox_get_totp_code"}:
            try:
                secret_id = str(UUID(str(args.get("secret_id") or "")))
            except ValueError:
                raise ValueError("secret_id must be a UUID from inkbox_list_vault_secrets") from None
        if name in {tool["name"] for tool in VAULT_TOOLS}:
            from inkbox import Inkbox
            if not cfg.api_key or not cfg.identity:
                raise ValueError("Invalid configuration: INKBOX_API_KEY and INKBOX_IDENTITY are required")
            if name == "inkbox_list_vault_secrets" and args.get("secret_type") not in {None, "login", "api_key", "key_pair", "ssh_key", "other"}:
                raise ValueError("Invalid secret_type")
            if name != "inkbox_list_vault_secrets" and not os.getenv("INKBOX_HERMES_VAULT_KEY"):
                raise ValueError("Vault is locked. Set INKBOX_HERMES_VAULT_KEY locally and restart; never send the key in chat.")
            client = Inkbox(**inkbox_client_kwargs(cfg.api_key, cfg.base_url))
            identity = client.get_identity(cfg.identity)
        else:
            _cfg, client, identity = _client_and_identity()
        if name.startswith("inkbox_slack_"):
            result = run_tool(client, cfg.identity, name, args)
        elif name == "inkbox_list_vault_secrets":
            if args.get("secret_type") not in {None, "login", "api_key", "key_pair", "ssh_key", "other"}:
                raise ValueError("Invalid secret_type")
            result = []
            for secret in client.vault.list_secrets(secret_type=args.get("secret_type")):
                access = getattr(secret, "access", None)
                if access is None:
                    access = client.vault.list_access_rules(secret.id)
                if _vault_access(access, identity.id, secret.id):
                    result.append(secret)
        elif name in {"inkbox_get_vault_secret", "inkbox_get_totp_code"}:
            if not _vault_access(client.vault.list_access_rules(secret_id), identity.id, secret_id):
                raise PermissionError("Configured identity does not have access to this credential")
            key = os.getenv("INKBOX_HERMES_VAULT_KEY")
            # Always honor this plugin's selected key, even if SDK-global config
            # unlocked the client during construction. Never reuse its cache.
            vault = client.vault.unlock(key)
            result = _json_safe(vault.get_totp_code(secret_id) if name == "inkbox_get_totp_code" else vault.get_secret(secret_id))
            if name == "inkbox_get_vault_secret" and result.get("secret_type") == "login":
                result["has_totp"] = result["payload"].pop("totp", None) is not None
        else:
            from .imessage_state import require_threading, active_context, validate_target
            require_threading(identity)
            context = active_context(session_id)
            if context and context.get("companion"):
                raise ValueError("Use supplied Companion history; native thread reads are unavailable in this turn")
            options = {key: args[key] for key in ("limit", "cursor") if key in args}
            if "limit" in options and (type(options["limit"]) is not int or not 1 <= options["limit"] <= 200):
                raise ValueError("limit must be an integer from 1 to 200")
            if "cursor" in options and (not isinstance(options["cursor"], str) or not 1 <= len(options["cursor"]) <= 1024):
                raise ValueError("cursor must be 1–1024 characters")
            if name == "inkbox_get_imessage_thread":
                message_id = str(args.get("message_id") or "").strip()
                if not message_id:
                    raise ValueError("message_id is required")
                if context:
                    message = identity.get_imessage(message_id)
                    validate_target(context, str(getattr(message, "conversation_id", "")), None)
                result = identity.get_imessage_thread(message_id, **options)
            elif name == "inkbox_get_imessage_conversation_thread":
                conversation = str(args.get("conversation_id") or "").strip()
                thread = str(args.get("thread_id") or "").strip()
                if not conversation or not thread:
                    raise ValueError("conversation_id and thread_id are required")
                validate_target(context, conversation, None)
                result = identity.get_imessage_conversation_thread(conversation, thread, **options)
            else:
                raise ValueError("Unknown tool")
        return json.dumps({"ok": True, "result": _json_safe(result)}, ensure_ascii=False)
    except Exception as exc:
        # SDK errors can contain request material; never expose it for secrets.
        if name in {tool["name"] for tool in VAULT_TOOLS} and not isinstance(exc, ValueError):
            error = f"Vault operation failed ({type(exc).__name__}); verify local configuration and access."
        elif name in {tool["name"] for tool in VAULT_TOOLS} and str(exc).startswith(("Vault is locked.", "secret_id must", "Invalid")):
            error = str(exc)
        elif name in {tool["name"] for tool in VAULT_TOOLS}:
            error = "Vault could not decrypt this credential; verify the local key and access."
        else:
            error = str(exc)
        result = {"error": error}
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            result["status_code"] = status
        return json.dumps(result)
    finally:
        if client is not None and callable(getattr(client, "close", None)):
            client.close()


def register(ctx, configured):
    specs = [*VAULT_TOOLS, *THREAD_TOOLS,
             *[{"name": item["name"], "description": item["description"], "parameters": item["inputSchema"]} for item in SLACK_TOOLS]]
    thread_names = {tool["name"] for tool in THREAD_TOOLS}
    for spec in specs:
        def handler(args, _name=spec["name"], **kwargs):
            return dispatch(_name, args, **kwargs)
        def available(_name=spec["name"]):
            cfg = read_runtime_config()
            return bool(configured() and (not _name.startswith("inkbox_slack_") or cfg.slack_enabled)
                        and (_name not in thread_names or cfg.imessage_threaded_replies))
        ctx.register_tool(spec["name"], "inkbox", spec, handler, check_fn=available)
