"""Native WhatsApp tools use the configured identity and the current host turn lease."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid

from .imessage_state import active_context, explicit_send, record_explicit
from .whatsapp import MAX_MEDIA_BYTES, method


def invoke(operation, args, session_id=""):
    from .tools import _client_and_identity, _json_safe, _safe_local_media_path

    client = None
    try:
        owner = active_context(session_id)

        def authorize():
            current = active_context(session_id)
            if owner is not None and current is not owner:
                raise PermissionError("The originating turn has ended")
            if owner and owner.get("mode") == "whatsapp":
                guard = owner.get("authorize_whatsapp")
                if not callable(guard):
                    raise PermissionError("The originating WhatsApp turn is unavailable")
                guard()

        authorize()
        with explicit_send(session_id, args.get("conversation_id"), args.get("to"), allow_other_destination=True) as context:
            cfg, client, identity = _client_and_identity()
            authorize()
            if (
                owner
                and owner.get("mode") == "whatsapp"
                and (
                    str(identity.id) != owner.get("identity_id")
                    or str(cfg.base_url or "https://inkbox.ai").rstrip("/") != owner.get("base_url")
                )
            ):
                raise PermissionError("The originating WhatsApp identity has changed")
            if operation == "lookup":
                result = {
                    "message_id": str(
                        client.message_sends.lookup(
                            sender_kind="whatsapp_identity",
                            sender_id=identity.id,
                            operation="whatsapp.send",
                            idempotency_key=args["idempotency_key"],
                        )
                    )
                }
            elif operation == "get_whatsapp_status":
                result = {"status": method(identity, operation)()}
                try:
                    result["limits"] = method(identity, "get_whatsapp_limits")()
                except Exception:
                    result["limits"] = None
            elif operation == "send_whatsapp":
                selector = {name: args[name] for name in ("to", "conversation_id") if name in args}
                if len(selector) != 1 or not (str(args.get("text") or "").strip() or args.get("media_id")):
                    raise ValueError("Use one recipient or conversation and text or an owned media ID")
                payload = {
                    **selector,
                    **({"text": args["text"]} if "text" in args else {}),
                    **({"media_ids": [args["media_id"]]} if args.get("media_id") else {}),
                }
                source = (owner or {}).get("turn_id") or uuid.uuid4().hex
                key = args.get("idempotency_key") or (
                    "hermes-wa-tool:"
                    + hashlib.sha256(json.dumps([str(identity.id), source, payload], sort_keys=True).encode()).hexdigest()
                )
                authorize()
                callback = (
                    (owner or {}).get("explicit_delivery_state") if context and context.get("mode") == "whatsapp" else None
                )
                if callback:
                    callback("sending")
                try:
                    result = method(identity, operation)(**payload, idempotency_key=key)
                except Exception as exc:
                    exc.whatsapp_media_id = args.get("media_id")
                    if callback:
                        callback("uncertain")
                    raise
                if callback:
                    callback("sent")
                if context and context.get("mode") == "whatsapp":
                    record_explicit(context, str(args.get("text") or ""), result)
                result = {"message": result, "idempotency_key": key, "media_id": args.get("media_id")}
            elif operation == "upload_whatsapp_media":
                path = _safe_local_media_path(args["media_path"], max_bytes=MAX_MEDIA_BYTES, channel="WhatsApp")
                with path.open("rb") as stream:
                    content = stream.read(MAX_MEDIA_BYTES + 1)
                if len(content) > MAX_MEDIA_BYTES:
                    raise ValueError("WhatsApp media must be at most 20 MiB")
                authorize()
                result = method(identity, operation)(
                    content=content,
                    filename=path.name,
                    content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
            else:
                authorize()
                result = method(identity, operation)(**args)
            return json.dumps({"ok": True, "result": _json_safe(result)}, ensure_ascii=False)
    except Exception as exc:
        result = {"error": "WhatsApp operation unavailable. Check access and the originating turn."}
        if "WhatsApp requires Inkbox SDK" in str(exc):
            result["error"] = "WhatsApp requires Inkbox SDK 0.7.21 or newer"
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            result["status_code"] = status
        key = getattr(exc, "idempotency_key", None)
        if key:
            result.update(
                idempotency_key=key,
                media_id=getattr(exc, "whatsapp_media_id", None),
                recovery="Use inkbox_lookup_whatsapp_send; do not send or upload a new copy.",
            )
        return json.dumps(result)
    finally:
        if client is not None:
            client.close()


def register(ctx, check_fn):
    def string(description):
        return {"type": "string", "description": description}

    pagination = {"limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}
    specs = [
        (
            "inkbox_get_whatsapp_status",
            "Read fresh dedicated WhatsApp availability. Approval is requested in the console, not granted by tools.",
            {},
            [],
            "get_whatsapp_status",
        ),
        (
            "inkbox_send_whatsapp",
            "Send requested one-to-one WhatsApp text and one owned media ID. Normal final replies are automatic; do not duplicate them with this tool. Preserve the key and media ID after uncertainty.",
            {
                "to": string("One E.164 recipient"),
                "conversation_id": string("Existing conversation, mutually exclusive with to"),
                "text": {**string("Message text"), "maxLength": 4000},
                "media_id": string("Owned attachment ID"),
                "idempotency_key": string("Optional original key when recovering the same logical send"),
            },
            [],
            "send_whatsapp",
        ),
        (
            "inkbox_list_whatsapp_conversations",
            "List WhatsApp conversations with local unread counts.",
            pagination,
            [],
            "list_whatsapp_conversations",
        ),
        (
            "inkbox_get_whatsapp_conversation",
            "Read one WhatsApp conversation's metadata.",
            {"conversation_id": string("Conversation ID")},
            ["conversation_id"],
            "get_whatsapp_conversation",
        ),
        (
            "inkbox_list_whatsapp_messages",
            "List WhatsApp messages, newest first.",
            {**pagination, "conversation_id": string("Conversation ID")},
            ["conversation_id"],
            "list_whatsapp_messages",
        ),
        (
            "inkbox_get_whatsapp_message",
            "Read current sending and delivery state without resending.",
            {"message_id": string("Message ID")},
            ["message_id"],
            "get_whatsapp_message",
        ),
        (
            "inkbox_upload_whatsapp_media",
            "Upload one local file (at most 20 MiB) and retain its owned ID. Never re-upload to recover an uncertain send.",
            {"media_path": string("Authorized local file")},
            ["media_path"],
            "upload_whatsapp_media",
        ),
        (
            "inkbox_get_whatsapp_media",
            "Renew temporary attachment access by its owned media ID.",
            {"media_id": string("Owned attachment ID")},
            ["media_id"],
            "get_whatsapp_media",
        ),
        (
            "inkbox_mark_whatsapp_read",
            "Mark local history read through an exact visible message; sends no remote receipt.",
            {"conversation_id": string("Conversation ID"), "through_message_id": string("Boundary message ID")},
            ["conversation_id", "through_message_id"],
            "mark_whatsapp_read",
        ),
        (
            "inkbox_lookup_whatsapp_send",
            "Recover the original message ID by logical-send key; read-only, no send or retry.",
            {"idempotency_key": string("Original request key")},
            ["idempotency_key"],
            "lookup",
        ),
    ]
    for name, description, properties, required, operation in specs:

        def handler(args, session_id="", operation=operation, allowed=frozenset(properties), **_kwargs):
            if set(args) - allowed:
                return json.dumps({"error": "Unsupported WhatsApp arguments"})
            return invoke(operation, args, session_id)

        schema = {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        }
        ctx.register_tool(name, "inkbox", schema, handler, check_fn=check_fn)
