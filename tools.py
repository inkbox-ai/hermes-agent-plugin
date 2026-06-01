"""Inkbox tools registered by the Hermes plugin."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict
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

        to = args.get("to")
        conversation_id = args.get("conversation_id") or args.get("conversationId")
        if bool(to) == bool(conversation_id):
            return _json({"error": "Specify exactly one of `to` or `conversation_id`."})

        payload: dict[str, Any] = {"text": text}
        if conversation_id:
            payload["conversation_id"] = str(conversation_id).strip()
        else:
            if isinstance(to, list):
                payload["to"] = [str(x).strip() for x in to if str(x).strip()]
            else:
                payload["to"] = str(to).strip()
        media_urls = args.get("media_urls") or args.get("mediaUrls")
        if media_urls:
            payload["media_urls"] = media_urls

        def _send():
            try:
                return identity.send_text(**payload)
            except TypeError:
                return identity.send_text(payload)

        msg = _send()
        return _json({
            "ok": True,
            "message_id": str(getattr(msg, "id", "")),
            "status": object_summary(getattr(msg, "delivery_status", None) or getattr(msg, "status", None)),
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
    "description": "Send an SMS/MMS from the configured Inkbox identity phone number.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"description": "One E.164 recipient or a list for group MMS.", "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "conversation_id": {"type": "string", "description": "Existing Inkbox text conversation id. Mutually exclusive with `to`."},
            "text": {"type": "string", "description": "Message body, max 1600 chars."},
            "media_urls": {"type": "array", "items": {"type": "string"}, "description": "Optional public MMS media URLs."},
        },
        "required": ["text"],
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
    ctx.register_tool("inkbox_place_call", "inkbox", PLACE_CALL_SCHEMA, inkbox_place_call, check_fn=_configured)
