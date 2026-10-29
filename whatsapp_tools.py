"""Native WhatsApp tools use the configured identity and the current host turn lease."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid

from .imessage_state import active_context, explicit_send, record_explicit
from .whatsapp import MAX_MEDIA_BYTES, auto_reply_kwargs, method, public_quota_error


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
                guard(read_only=True) if operation == "lookup" or operation.startswith(("get_", "list_")) else guard()

        authorize()
        args = dict(args)
        if (operation == "send_whatsapp" and owner and owner.get("mode") == "whatsapp"
                and "conversation_id" not in args and "to" not in args):
            args["conversation_id"] = owner["conversation_id"]
        conversation, recipient = args.get("conversation_id"), args.get("to")
        direct_recipient = recipient[0] if isinstance(recipient, list) and len(recipient) == 1 else recipient
        canonical = re.sub(r"[\s().-]", "", direct_recipient) if isinstance(direct_recipient, str) else None
        if (owner and owner.get("mode") == "whatsapp" and owner.get("conversation_kind") != "group" and canonical == owner.get("author")
                and re.fullmatch(r"\+[1-9][0-9]{6,14}", canonical or "")):
            conversation, recipient = owner.get("conversation_id"), None
        with explicit_send(session_id, conversation, recipient, allow_other_destination=True) as context:
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
                    raise ValueError("Use 1–8 unique recipients or a conversation, with text or an owned media ID")
                if isinstance(selector.get("to"), str):
                    selector["to"] = re.sub(r"[\s().-]", "", selector["to"])
                elif isinstance(selector.get("to"), list):
                    selector["to"] = [re.sub(r"[\s().-]", "", phone) if isinstance(phone, str) else phone for phone in selector["to"]]
                payload = {
                    **selector,
                    **({"text": args["text"]} if "text" in args else {}),
                    **({"media_ids": [args["media_id"]]} if args.get("media_id") else {}),
                    **{name: args[name] for name in ("reply_to_message_id", "group_name") if name in args},
                }
                prior = (context or {}).get("whatsapp_send_attempt")
                if (prior and args.get("idempotency_key") == prior["key"]
                        and "reply_to_message_id" not in payload and "reply_to_message_id" in prior["payload"]):
                    payload["reply_to_message_id"] = prior["payload"]["reply_to_message_id"]
                # Caller-supplied keys may recover older plain requests.
                if (context and context.get("mode") == "whatsapp" and "conversation_id" in payload
                        and "reply_to_message_id" not in payload and not args.get("idempotency_key")):
                    payload.update(auto_reply_kwargs(context))
                # Prove the public request shape before recording a send attempt.
                recipients = [payload["to"]] if isinstance(payload.get("to"), str) else payload.get("to")
                if recipients is not None and (not isinstance(recipients, list) or not 1 <= len(recipients) <= 8
                        or any(not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone) for phone in recipients)
                        or len(set(recipients)) != len(recipients)):
                    raise ValueError("Provide 1–8 unique E.164 recipients")
                if "reply_to_message_id" in payload:
                    if "conversation_id" not in payload:
                        raise ValueError("Quoted replies require conversation_id")
                    uuid.UUID(str(payload["reply_to_message_id"]))
                name = payload.get("group_name")
                if name is not None and (not isinstance(payload.get("to"), list) or len(payload["to"]) < 2
                        or not isinstance(name, str) or not name.strip() or len(name) > 100):
                    raise ValueError("A group name requires 2–8 recipients and 1–100 characters")
                if "text" in payload and (not isinstance(payload["text"], str) or len(payload["text"]) > 4000):
                    raise ValueError("WhatsApp text must be at most 4000 characters")
                source = (owner or {}).get("turn_id") or uuid.uuid4().hex
                key = args.get("idempotency_key") or (
                    "hermes-wa-tool:"
                    + hashlib.sha256(json.dumps([str(identity.id), source, payload], sort_keys=True).encode()).hexdigest()
                )
                if prior and (prior["key"] == key or prior["state"] in {"sending", "uncertain", "rejected"}):
                    if prior["state"] in {"sending", "uncertain"} or prior["payload"] != payload or prior["key"] != key:
                        raise ValueError("Recover the exact original WhatsApp request and key; do not send a replacement")
                authorize()
                callback = (
                    (owner or {}).get("explicit_delivery_state") if context and context.get("mode") == "whatsapp" else None
                )
                attempt = {"payload": payload, "key": key, "state": "sending"}
                record_attempt = context.get("record_whatsapp_attempt") if context and context.get("mode") == "whatsapp" else None
                if context and context.get("mode") == "whatsapp":
                    context["whatsapp_send_attempt"] = attempt
                if record_attempt:
                    record_attempt(attempt)
                if callback:
                    callback("sending")
                try:
                    result = method(identity, operation)(**payload, idempotency_key=key)
                except Exception as exc:
                    exc.whatsapp_media_id = args.get("media_id")
                    exc.idempotency_key = key
                    attempt["state"] = "rejected" if public_quota_error(exc) else "uncertain"
                    if record_attempt:
                        record_attempt(attempt)
                    if callback:
                        callback(attempt["state"])
                    raise
                attempt["state"] = "sent"
                if record_attempt:
                    record_attempt(attempt)
                if callback:
                    callback("sent")
                if context and context.get("mode") == "whatsapp":
                    record_explicit(context, str(args.get("text") or ""), result)
                result = {"message": result, "idempotency_key": key, "media_id": args.get("media_id")}
            elif operation in {"send_whatsapp_reaction", "remove_whatsapp_reaction"}:
                key = args.get("idempotency_key")
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("Reaction writes require an explicit stable idempotency key")
                authorize()
                try:
                    result = method(identity, operation)(**args)
                except Exception as exc:
                    exc.idempotency_key = key
                    raise
                result = {"reaction": result, "idempotency_key": key}
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
            result["error"] = "WhatsApp requires Inkbox SDK 0.7.24 or newer"
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            result["status_code"] = status
        policy = public_quota_error(exc)
        result.update(policy)
        key = getattr(exc, "idempotency_key", None)
        if key and operation in {"send_whatsapp_reaction", "remove_whatsapp_reaction"}:
            result.update(idempotency_key=key, recovery="Inspect the original reaction receipt when available; retry only the exact original request and key. Never infer that an uncertain reaction was not applied.")
        elif key:
            result.update(
                idempotency_key=key,
                media_id=getattr(exc, "whatsapp_media_id", None),
                recovery=("Request not accepted. Follow the retry condition, then reuse the original key and owned media ID." if policy else "Use inkbox_lookup_whatsapp_send; do not send or upload a new copy."),
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
            "Send requested direct or group WhatsApp text and one owned media ID. Omit the destination to reply in the active conversation. Same-conversation sends quote the source automatically; explicit destinations and authorized quote targets remain authoritative. Normal final replies are automatic; do not duplicate them with this tool. Preserve the key and media ID after uncertainty.",
            {
                "to": {"description": "One E.164 recipient or 2–8 distinct E.164 recipients for a group", "oneOf": [string("E.164 recipient"), {"type": "array", "items": string("E.164 recipient"), "minItems": 1, "maxItems": 8, "uniqueItems": True}]},
                "reply_to_message_id": string("Optional authorized message UUID to quote; otherwise the active source is used. Requires a conversation."),
                "group_name": {**string("Creation-only name with 2–8 recipients; never renames an existing group"), "minLength": 1, "maxLength": 100},
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
            {**pagination, "include_groups": {"type": "boolean", "description": "Default false; true also includes group conversations"}},
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
            {**pagination, "conversation_id": string("Conversation ID"), "thread_id": string("Quote-chain ID; requires conversation_id"), "include_groups": {"type": "boolean"}},
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
            "inkbox_get_whatsapp_thread", "Read an authorized chronological quote chain; preserve its opaque cursor.",
            {"message_id": string("Visible message UUID"), "limit": pagination["limit"], "cursor": string("Opaque next cursor")},
            ["message_id"], "get_whatsapp_thread",
        ),
        (
            "inkbox_get_whatsapp_conversation_thread", "Read one conversation's authorized quote chain; never infer a hidden parent.",
            {"conversation_id": string("Conversation UUID"), "thread_id": string("Quote-chain UUID"), "limit": pagination["limit"], "cursor": string("Opaque next cursor")},
            ["conversation_id", "thread_id"], "get_whatsapp_conversation_thread",
        ),
        (
            "inkbox_send_whatsapp_reaction", "Set a whole-message reaction. Queued and uncertain receipts are not applied reactions.",
            {"message_id": string("Authorized target message UUID"), "reaction": {"type": "string", "enum": ["love", "like", "dislike", "laugh", "emphasize", "question", "eyes"]},
             "custom_emoji": {**string("Exclusive with reaction; 1–32 code points without whitespace or control characters"), "minLength": 1, "maxLength": 32}, "idempotency_key": string("Required stable key for this exact intended reaction")},
            ["message_id", "idempotency_key"], "send_whatsapp_reaction",
        ),
        (
            "inkbox_get_whatsapp_reaction", "Read a versioned reaction receipt without repeating its write.",
            {"reaction_id": string("Reaction UUID")}, ["reaction_id"], "get_whatsapp_reaction",
        ),
        (
            "inkbox_remove_whatsapp_reaction", "Remove your exact reaction version; never removes a later replacement.",
            {"reaction_id": string("Own reaction UUID"), "idempotency_key": string("Required stable removal key")},
            ["reaction_id", "idempotency_key"], "remove_whatsapp_reaction",
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
