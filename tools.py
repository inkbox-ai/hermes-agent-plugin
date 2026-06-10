"""Inkbox tools registered by the Hermes plugin."""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

try:
    from .config import object_summary, public_call_ws_url, read_config
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import object_summary, public_call_ws_url, read_config


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _configured() -> bool:
    cfg = read_config()
    return bool(cfg.api_key and cfg.identity)


def _client_and_identity():
    from inkbox import Inkbox

    cfg = read_config()
    if not cfg.api_key:
        raise RuntimeError("INKBOX_API_KEY is not set")
    if not cfg.identity:
        raise RuntimeError("INKBOX_IDENTITY is not set")
    client = Inkbox(api_key=cfg.api_key, base_url=cfg.base_url)
    return cfg, client, client.get_identity(cfg.identity)


def _normalize_recipients(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        return [trimmed] if trimmed else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _identity_method(identity: Any, snake_name: str, camel_name: Optional[str] = None):
    method = getattr(identity, snake_name, None)
    if callable(method):
        return method
    if camel_name:
        method = getattr(identity, camel_name, None)
        if callable(method):
            return method
    raise RuntimeError(f"Inkbox SDK identity has no {snake_name} method")


def _call_with_kwargs_or_payload(method, payload: Dict[str, Any], camel_payload: Optional[Dict[str, Any]] = None):
    try:
        return method(**payload)
    except TypeError:
        return method(camel_payload or payload)


def _call_with_key_and_options(method, key: str, options: Dict[str, Any], camel_options: Optional[Dict[str, Any]] = None):
    try:
        return method(key, **options)
    except TypeError:
        try:
            return method(key, camel_options or options)
        except TypeError:
            return method(key)


def _json_safe(value: Any) -> Any:
    """Convert SDK dataclasses (UUIDs, datetimes, enums) into JSON-safe data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_safe(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    return str(getattr(value, "value", value))


def _text_conversation_key(args: dict) -> Tuple[str, str, Optional[str]]:
    conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
    remote_phone = str(args.get("remotePhoneNumber") or args.get("remote_phone_number") or "").strip()
    if bool(conversation_id) == bool(remote_phone):
        return "", "", "Specify exactly one of `conversationId` or `remotePhoneNumber`."
    if conversation_id:
        return conversation_id, f"conversation {conversation_id}", None
    return remote_phone, f"conversation with {remote_phone}", None


def _append_query_param(raw_url: str, key: str, value: str) -> str:
    parts = urlparse(raw_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    return urlunparse(parts._replace(query=urlencode(query)))


def _write_outbound_call_context(params: Dict[str, Any]) -> str:
    from hermes_cli.config import get_hermes_home

    token = secrets.token_urlsafe(18)
    root = Path(get_hermes_home()) / "inkbox_call_contexts"
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": time.time(),
        "purpose": str(params.get("purpose") or "").strip(),
        "opening_message": str(params.get("opening_message") or params.get("openingMessage") or "").strip(),
        "context": str(params.get("context") or "").strip(),
        "to_number": str(params.get("to_number") or params.get("toNumber") or "").strip(),
    }
    (root / f"{token}.json").write_text(json.dumps(payload, indent=2) + "\n")
    return token


def inkbox_whoami(args: dict, **kwargs) -> str:
    del args, kwargs
    try:
        cfg, client, identity = _client_and_identity()
        return _json({
            "ok": True,
            "base_url": cfg.base_url,
            "whoami": object_summary(client.whoami()),
            "identity": object_summary(identity),
            "call_websocket_url": public_call_ws_url(cfg, identity),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_send_email(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        to = args.get("to") or []
        if isinstance(to, str):
            to = [to]
        to = [str(x).strip() for x in to if str(x).strip()]
        if not to:
            return _json({"error": "`to` must contain at least one email address"})
        subject = str(args.get("subject") or "(no subject)")
        body_text = str(args.get("body_text") or args.get("bodyText") or "")
        body_html = args.get("body_html") or args.get("bodyHtml")
        in_reply_to = args.get("in_reply_to_message_id") or args.get("inReplyToMessageId")

        def _send():
            return identity.send_email(
                to=to,
                subject=subject,
                body_text=body_text or None,
                body_html=body_html or None,
                cc=args.get("cc") or None,
                bcc=args.get("bcc") or None,
                in_reply_to_message_id=in_reply_to or None,
            )

        msg = _send()
        return _json({
            "ok": True,
            "message_id": str(getattr(msg, "id", "")),
            "to": to,
            "subject": subject,
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_send_sms(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        text = str(args.get("text") or "")
        if not text:
            return _json({"error": "`text` is required"})
        if len(text) > 1600:
            return _json({"error": "SMS text exceeds Inkbox 1600 character limit", "char_count": len(text)})

        conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
        to_list = _normalize_recipients(args.get("to"))
        has_to = to_list is not None and len(to_list) > 0
        has_conversation = bool(conversation_id)
        if has_to == has_conversation:
            return _json({"error": "Specify exactly one of `to` or `conversationId`."})
        if to_list is not None and len(to_list) == 0:
            return _json({"error": "`to` must include at least one recipient."})
        if to_list and len(to_list) > 8:
            return _json({"error": "Inkbox group texts support at most 8 recipients."})

        payload: dict[str, Any] = {"text": text}
        camel_payload: dict[str, Any] = {"text": text}
        if conversation_id:
            payload["conversation_id"] = str(conversation_id).strip()
            camel_payload["conversationId"] = str(conversation_id).strip()
        else:
            payload["to"] = to_list[0] if to_list and len(to_list) == 1 else to_list
            camel_payload["to"] = payload["to"]
        media_urls = args.get("mediaUrls") or args.get("media_urls")
        if media_urls:
            payload["media_urls"] = media_urls
            camel_payload["mediaUrls"] = media_urls

        msg = _call_with_kwargs_or_payload(
            _identity_method(identity, "send_text", "sendText"),
            payload,
            camel_payload,
        )
        return _json({
            "ok": True,
            "message_id": str(getattr(msg, "id", "")),
            "conversation_id": conversation_id or object_summary(
                getattr(msg, "conversation_id", None) or getattr(msg, "conversationId", None)
            ),
            "to": None if conversation_id else payload.get("to"),
            "status": object_summary(getattr(msg, "delivery_status", None) or getattr(msg, "status", None)),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_text_conversations(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        payload = {
            "limit": int(args.get("limit") or 25),
            "offset": int(args.get("offset") or 0),
            "include_groups": args.get("includeGroups") if "includeGroups" in args else args.get("include_groups", True),
        }
        camel_payload = {
            "limit": payload["limit"],
            "offset": payload["offset"],
            "includeGroups": payload["include_groups"],
        }
        convos = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_text_conversations", "listTextConversations"),
            payload,
            camel_payload,
        )
        return _json({"ok": True, "count": len(convos or []), "conversations": object_summary(convos or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_get_text_conversation(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        key, label, error = _text_conversation_key(args)
        if error:
            return _json({"error": error})
        options = {"limit": int(args.get("limit") or 50), "offset": int(args.get("offset") or 0)}
        msgs = _call_with_key_and_options(
            _identity_method(identity, "get_text_conversation", "getTextConversation"),
            key,
            options,
            options,
        )
        return _json({"ok": True, "conversation": label, "count": len(msgs or []), "texts": object_summary(msgs or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_texts(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        payload = {"limit": int(args.get("limit") or 25), "offset": int(args.get("offset") or 0)}
        if "isRead" in args or "is_read" in args:
            payload["is_read"] = args.get("isRead") if "isRead" in args else args.get("is_read")
        camel_payload = dict(payload)
        if "is_read" in camel_payload:
            camel_payload["isRead"] = camel_payload.pop("is_read")
        texts = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_texts", "listTexts"),
            payload,
            camel_payload,
        )
        return _json({"ok": True, "count": len(texts or []), "texts": object_summary(texts or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_get_text(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        text_id = str(args.get("textId") or args.get("text_id") or "").strip()
        if not text_id:
            return _json({"error": "`textId` is required"})
        text = _identity_method(identity, "get_text", "getText")(text_id)
        return _json({"ok": True, "text": object_summary(text)})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_mark_text_read(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        text_id = str(args.get("textId") or args.get("text_id") or "").strip()
        if not text_id:
            return _json({"error": "`textId` is required"})
        _identity_method(identity, "mark_text_read", "markTextRead")(text_id)
        return _json({"ok": True, "text_id": text_id, "status": "marked_read"})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_mark_text_conversation_read(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        key, label, error = _text_conversation_key(args)
        if error:
            return _json({"error": error})
        result = _identity_method(
            identity,
            "mark_text_conversation_read",
            "markTextConversationRead",
        )(key)
        updated = (
            getattr(result, "updated_count", None)
            or getattr(result, "updatedCount", None)
        )
        return _json({
            "ok": True,
            "conversation": label,
            "updated_count": object_summary(updated),
            "result": object_summary(result),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_imessage_triage_number(args: dict, **kwargs) -> str:
    del args, kwargs
    try:
        _cfg, client, _identity = _client_and_identity()
        imessages = getattr(client, "imessages", None)
        if imessages is None:
            return _json({"error": "Installed Inkbox SDK has no iMessage support; upgrade with: pip install -U inkbox"})
        triage = imessages.get_triage_number()
        return _json({
            "ok": True,
            "number": str(getattr(triage, "number", "")),
            "connect_command": str(getattr(triage, "connect_command", "")),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_send_imessage(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        text = str(args.get("text") or "")
        media_urls = _normalize_recipients(args.get("mediaUrls") or args.get("media_urls"))
        if not text and not media_urls:
            return _json({"error": "Provide `text`, `mediaUrls`, or both."})
        if media_urls and len(media_urls) > 1:
            return _json({"error": "Inkbox iMessage supports at most one media URL per message."})

        conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
        to = str(args.get("to") or "").strip()
        if bool(conversation_id) == bool(to):
            return _json({"error": "Specify exactly one of `to` or `conversationId`."})

        payload: dict[str, Any] = {"text": text or None}
        camel_payload: dict[str, Any] = {"text": text or None}
        if conversation_id:
            payload["conversation_id"] = conversation_id
            camel_payload["conversationId"] = conversation_id
        else:
            payload["to"] = to
            camel_payload["to"] = to
        if media_urls:
            payload["media_urls"] = media_urls
            camel_payload["mediaUrls"] = media_urls
        send_style = str(args.get("sendStyle") or args.get("send_style") or "").strip()
        if send_style:
            payload["send_style"] = send_style
            camel_payload["sendStyle"] = send_style

        msg = _call_with_kwargs_or_payload(
            _identity_method(identity, "send_imessage", "sendImessage"),
            payload,
            camel_payload,
        )
        return _json({
            "ok": True,
            "message_id": str(getattr(msg, "id", "")),
            "conversation_id": _json_safe(
                getattr(msg, "conversation_id", None) or getattr(msg, "conversationId", None)
            ),
            "service": _json_safe(getattr(msg, "service", None)),
            "status": _json_safe(getattr(msg, "status", None)),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_imessage_conversations(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        options = {"limit": int(args.get("limit") or 25), "offset": int(args.get("offset") or 0)}
        convos = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_imessage_conversations", "listImessageConversations"),
            options,
        )
        return _json({"ok": True, "count": len(convos or []), "conversations": _json_safe(convos or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_imessage_assignments(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        options = {"limit": int(args.get("limit") or 25), "offset": int(args.get("offset") or 0)}
        assignments = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_imessage_assignments", "listImessageAssignments"),
            options,
        )
        return _json({"ok": True, "count": len(assignments or []), "assignments": _json_safe(assignments or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_get_imessage_conversation(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
        if not conversation_id:
            return _json({"error": "`conversationId` is required"})
        payload = {
            "conversation_id": conversation_id,
            "limit": int(args.get("limit") or 50),
            "offset": int(args.get("offset") or 0),
        }
        camel_payload = {
            "conversationId": conversation_id,
            "limit": payload["limit"],
            "offset": payload["offset"],
        }
        msgs = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_imessages", "listImessages"),
            payload,
            camel_payload,
        )
        return _json({
            "ok": True,
            "conversation_id": conversation_id,
            "count": len(msgs or []),
            "messages": _json_safe(msgs or []),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_send_imessage_reaction(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        message_id = str(args.get("messageId") or args.get("message_id") or "").strip()
        reaction = str(args.get("reaction") or "").strip().lower()
        if not message_id:
            return _json({"error": "`messageId` is required"})
        if not reaction:
            return _json({"error": "`reaction` is required"})
        payload = {
            "message_id": message_id,
            "reaction": reaction,
            "part_index": int(args.get("partIndex") or args.get("part_index") or 0),
        }
        camel_payload = {
            "messageId": message_id,
            "reaction": reaction,
            "partIndex": payload["part_index"],
        }
        result = _call_with_kwargs_or_payload(
            _identity_method(identity, "send_imessage_reaction", "sendImessageReaction"),
            payload,
            camel_payload,
        )
        return _json({"ok": True, "reaction": _json_safe(result)})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_mark_imessage_conversation_read(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
        if not conversation_id:
            return _json({"error": "`conversationId` is required"})
        result = _identity_method(
            identity,
            "mark_imessage_conversation_read",
            "markImessageConversationRead",
        )(conversation_id)
        updated = (
            getattr(result, "updated_count", None)
            or getattr(result, "updatedCount", None)
        )
        return _json({
            "ok": True,
            "conversation_id": conversation_id,
            "updated_count": _json_safe(updated),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_place_call(args: dict, **kwargs) -> str:
    del kwargs
    try:
        cfg, _client, identity = _client_and_identity()
        to_number = str(args.get("to_number") or args.get("toNumber") or "").strip()
        purpose = str(args.get("purpose") or "").strip()
        if not to_number:
            return _json({"error": "`to_number` is required"})
        if not purpose:
            return _json({"error": "`purpose` is required so the live call starts with the right context"})

        ws_url = str(args.get("client_websocket_url") or args.get("clientWebsocketUrl") or "").strip()
        if not ws_url:
            ws_url = public_call_ws_url(cfg, identity)
        if not ws_url:
            return _json({"error": "No call WebSocket URL available. Run `hermes inkbox setup` and start the gateway, or pass client_websocket_url."})

        token = _write_outbound_call_context({
            "to_number": to_number,
            "purpose": purpose,
            "opening_message": args.get("opening_message") or args.get("openingMessage") or "",
            "context": args.get("context") or "",
        })
        decorated_ws_url = _append_query_param(ws_url, "context_token", token)

        def _place():
            if hasattr(identity, "place_call"):
                try:
                    return identity.place_call(to_number=to_number, client_websocket_url=decorated_ws_url)
                except TypeError:
                    return identity.place_call({"to_number": to_number, "client_websocket_url": decorated_ws_url})
            if hasattr(identity, "placeCall"):
                return identity.placeCall({"toNumber": to_number, "clientWebsocketUrl": decorated_ws_url})
            raise RuntimeError("Inkbox SDK identity has no place_call method")

        call = _place()
        rate = object_summary(getattr(call, "rate_limit", None) or getattr(call, "rateLimit", None))
        return _json({
            "ok": True,
            "call_id": str(getattr(call, "id", "")),
            "status": object_summary(getattr(call, "status", None)),
            "to_number": to_number,
            "context_token": token,
            "rate_limit": rate,
        })
    except Exception as exc:
        return _json({"error": str(exc)})


WHOAMI_SCHEMA = {
    "name": "inkbox_whoami",
    "description": "Return the configured Inkbox identity, mailbox, phone number, auth scope, and call bridge URL.",
    "parameters": {"type": "object", "properties": {}},
}

SEND_EMAIL_SCHEMA = {
    "name": "inkbox_send_email",
    "description": "Send an email from the configured Inkbox identity.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses."},
            "subject": {"type": "string", "description": "Email subject."},
            "body_text": {"type": "string", "description": "Plain text body."},
            "body_html": {"type": "string", "description": "Optional HTML body."},
            "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}},
            "in_reply_to_message_id": {"type": "string", "description": "RFC 5322 Message-ID for threading replies."},
        },
        "required": ["to", "subject"],
    },
}

SEND_SMS_SCHEMA = {
    "name": "inkbox_send_sms",
    "description": "Send a text from the configured Inkbox identity phone number. Use conversationId to reply into an existing 1:1 or group conversation, or to for one E.164 recipient or a 2-8 recipient group MMS.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "description": "One E.164 recipient or a list of 1-8 recipients. Two or more sends a group MMS.",
                "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}, "maxItems": 8}],
            },
            "conversationId": {"type": "string", "description": "Existing Inkbox text conversation UUID from inkbox_list_text_conversations. Preferred for replies and group chats. Mutually exclusive with `to`."},
            "text": {"type": "string", "description": "Message body, max 1600 chars."},
            "mediaUrls": {"type": "array", "items": {"type": "string"}, "maxItems": 10, "description": "Optional public MMS media URLs."},
        },
        "required": ["text"],
    },
}

LIST_TEXT_CONVERSATIONS_SCHEMA = {
    "name": "inkbox_list_text_conversations",
    "description": "List text conversation summaries for the configured Inkbox identity phone number. Includes group chats by default and returns conversation IDs for replies.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "includeGroups": {"type": "boolean", "default": True, "description": "Include group conversations."},
        },
    },
}

GET_TEXT_CONVERSATION_SCHEMA = {
    "name": "inkbox_get_text_conversation",
    "description": "Fetch messages in a specific text conversation. Use conversationId for canonical rows and group chats; remotePhoneNumber is the legacy 1:1 fallback.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Inkbox text conversation UUID from inkbox_list_text_conversations."},
            "remotePhoneNumber": {"type": "string", "description": "Legacy 1:1 remote E.164 phone number identifying the conversation."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
    },
}

LIST_TEXTS_SCHEMA = {
    "name": "inkbox_list_texts",
    "description": "List individual SMS/MMS messages. Prefer inkbox_list_text_conversations for triage; this is low-level access.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "isRead": {"type": "boolean", "description": "Filter by read state."},
        },
    },
}

GET_TEXT_SCHEMA = {
    "name": "inkbox_get_text",
    "description": "Fetch a single SMS/MMS message by text message UUID.",
    "parameters": {
        "type": "object",
        "properties": {
            "textId": {"type": "string", "description": "UUID of the text message."},
        },
        "required": ["textId"],
    },
}

MARK_TEXT_READ_SCHEMA = {
    "name": "inkbox_mark_text_read",
    "description": "Mark a single SMS/MMS message as read.",
    "parameters": {
        "type": "object",
        "properties": {
            "textId": {"type": "string", "description": "UUID of the text message."},
        },
        "required": ["textId"],
    },
}

MARK_TEXT_CONVERSATION_READ_SCHEMA = {
    "name": "inkbox_mark_text_conversation_read",
    "description": "Mark every message in a text conversation as read. Use conversationId for canonical rows and group chats; remotePhoneNumber is the legacy 1:1 fallback.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Inkbox text conversation UUID from inkbox_list_text_conversations."},
            "remotePhoneNumber": {"type": "string", "description": "Legacy 1:1 remote E.164 phone number identifying the conversation."},
        },
    },
}

IMESSAGE_TRIAGE_NUMBER_SCHEMA = {
    "name": "inkbox_imessage_triage_number",
    "description": "Return the Inkbox iMessage router number and the connect command a person texts to it (from an iPhone) to reach this agent over iMessage. Share these when someone asks how to iMessage the agent.",
    "parameters": {"type": "object", "properties": {}},
}

SEND_IMESSAGE_SCHEMA = {
    "name": "inkbox_send_imessage",
    "description": "Send an iMessage from the configured Inkbox identity. Recipient-first channel: a person must have connected via the iMessage router and messaged this agent before outbound sends work, so prefer conversationId from an inbound message or inkbox_list_imessage_conversations.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Existing Inkbox iMessage conversation UUID. Preferred for replies. Mutually exclusive with `to`."},
            "to": {"type": "string", "description": "Recipient phone number in E.164 format. Only works after that person has messaged this agent. Mutually exclusive with `conversationId`."},
            "text": {"type": "string", "description": "Message body."},
            "mediaUrls": {"type": "array", "items": {"type": "string"}, "maxItems": 1, "description": "Optional media URL (at most one per message)."},
            "sendStyle": {
                "type": "string",
                "enum": ["celebration", "shooting_star", "fireworks", "lasers", "love", "confetti", "balloons", "spotlight", "echo", "invisible", "gentle", "loud", "slam"],
                "description": "Optional expressive iMessage send style.",
            },
        },
    },
}

LIST_IMESSAGE_ASSIGNMENTS_SCHEMA = {
    "name": "inkbox_list_imessage_assignments",
    "description": "List the people actively connected to this agent over iMessage (one row per recipient, newest first). Released connections are not returned. Use to answer who the agent can currently iMessage.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
    },
}

LIST_IMESSAGE_CONVERSATIONS_SCHEMA = {
    "name": "inkbox_list_imessage_conversations",
    "description": "List iMessage conversation summaries for the configured Inkbox identity. Returns conversation IDs for replies, latest-message previews, unread counts, and assignment_status (released = that person disconnected; replies fail until they reconnect).",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
    },
}

GET_IMESSAGE_CONVERSATION_SCHEMA = {
    "name": "inkbox_get_imessage_conversation",
    "description": "Fetch messages in one iMessage conversation, newest first. Messages include any live tapback reactions.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Inkbox iMessage conversation UUID from inkbox_list_imessage_conversations."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["conversationId"],
    },
}

SEND_IMESSAGE_REACTION_SCHEMA = {
    "name": "inkbox_send_imessage_reaction",
    "description": "Send a tapback reaction to an iMessage the agent received.",
    "parameters": {
        "type": "object",
        "properties": {
            "messageId": {"type": "string", "description": "UUID of the iMessage being reacted to."},
            "reaction": {
                "type": "string",
                "enum": ["love", "like", "dislike", "laugh", "emphasize", "question"],
                "description": "Tapback kind.",
            },
            "partIndex": {"type": "integer", "minimum": 0, "default": 0, "description": "Part of a multi-part message to react to."},
        },
        "required": ["messageId", "reaction"],
    },
}

MARK_IMESSAGE_CONVERSATION_READ_SCHEMA = {
    "name": "inkbox_mark_imessage_conversation_read",
    "description": "Send a read receipt and mark every inbound message in an iMessage conversation as read.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Inkbox iMessage conversation UUID."},
        },
        "required": ["conversationId"],
    },
}

PLACE_CALL_SCHEMA = {
    "name": "inkbox_place_call",
    "description": "Place an outbound call from the configured Inkbox identity phone number. Always include purpose.",
    "parameters": {
        "type": "object",
        "properties": {
            "to_number": {"type": "string", "description": "Recipient phone number in E.164 format."},
            "purpose": {"type": "string", "description": "Why the call is being placed; loaded into the live call before greeting."},
            "opening_message": {"type": "string", "description": "Optional first thing to say when the call connects."},
            "context": {"type": "string", "description": "Optional concise background for the voice agent."},
            "client_websocket_url": {"type": "string", "description": "Optional explicit call media WebSocket URL."},
        },
        "required": ["to_number", "purpose"],
    },
}


def register_tools(ctx) -> None:
    ctx.register_tool("inkbox_whoami", "inkbox", WHOAMI_SCHEMA, inkbox_whoami, check_fn=_configured)
    ctx.register_tool("inkbox_send_email", "inkbox", SEND_EMAIL_SCHEMA, inkbox_send_email, check_fn=_configured)
    ctx.register_tool("inkbox_send_sms", "inkbox", SEND_SMS_SCHEMA, inkbox_send_sms, check_fn=_configured)
    ctx.register_tool("inkbox_list_text_conversations", "inkbox", LIST_TEXT_CONVERSATIONS_SCHEMA, inkbox_list_text_conversations, check_fn=_configured)
    ctx.register_tool("inkbox_get_text_conversation", "inkbox", GET_TEXT_CONVERSATION_SCHEMA, inkbox_get_text_conversation, check_fn=_configured)
    ctx.register_tool("inkbox_list_texts", "inkbox", LIST_TEXTS_SCHEMA, inkbox_list_texts, check_fn=_configured)
    ctx.register_tool("inkbox_get_text", "inkbox", GET_TEXT_SCHEMA, inkbox_get_text, check_fn=_configured)
    ctx.register_tool("inkbox_mark_text_read", "inkbox", MARK_TEXT_READ_SCHEMA, inkbox_mark_text_read, check_fn=_configured)
    ctx.register_tool("inkbox_mark_text_conversation_read", "inkbox", MARK_TEXT_CONVERSATION_READ_SCHEMA, inkbox_mark_text_conversation_read, check_fn=_configured)
    ctx.register_tool("inkbox_imessage_triage_number", "inkbox", IMESSAGE_TRIAGE_NUMBER_SCHEMA, inkbox_imessage_triage_number, check_fn=_configured)
    ctx.register_tool("inkbox_send_imessage", "inkbox", SEND_IMESSAGE_SCHEMA, inkbox_send_imessage, check_fn=_configured)
    ctx.register_tool("inkbox_list_imessage_assignments", "inkbox", LIST_IMESSAGE_ASSIGNMENTS_SCHEMA, inkbox_list_imessage_assignments, check_fn=_configured)
    ctx.register_tool("inkbox_list_imessage_conversations", "inkbox", LIST_IMESSAGE_CONVERSATIONS_SCHEMA, inkbox_list_imessage_conversations, check_fn=_configured)
    ctx.register_tool("inkbox_get_imessage_conversation", "inkbox", GET_IMESSAGE_CONVERSATION_SCHEMA, inkbox_get_imessage_conversation, check_fn=_configured)
    ctx.register_tool("inkbox_send_imessage_reaction", "inkbox", SEND_IMESSAGE_REACTION_SCHEMA, inkbox_send_imessage_reaction, check_fn=_configured)
    ctx.register_tool("inkbox_mark_imessage_conversation_read", "inkbox", MARK_IMESSAGE_CONVERSATION_READ_SCHEMA, inkbox_mark_imessage_conversation_read, check_fn=_configured)
    ctx.register_tool("inkbox_place_call", "inkbox", PLACE_CALL_SCHEMA, inkbox_place_call, check_fn=_configured)
