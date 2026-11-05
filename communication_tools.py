"""Identity-scoped email, call history, and message recovery tools."""
from __future__ import annotations

import base64
from itertools import islice
import json
import mimetypes
import os
from pathlib import Path
import tempfile
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import httpx

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
REQUEST_KEY = {"type": "string", "minLength": 1, "maxLength": 255,
               "description": "Stable logical-send key. Reuse only for the identical request; inspect uncertain sends before retrying."}
ATTACHMENTS = {"type": "array", "maxItems": 20, "items": {
    "type": "object", "additionalProperties": False, "required": ["path"], "properties": {
        "path": {"type": "string", "description": "Host-approved local attachment path."},
        "content_id": {"type": "string", "description": "Optional inline image CID referenced by body_html."},
    },
}}


def request_key(args):
    key = args.get("idempotency_key", str(uuid4()))
    if not isinstance(key, str) or not key.strip() or len(key) > 255 or not key.isascii() or not key.isprintable():
        raise ValueError("idempotency_key must contain 1–255 printable ASCII characters")
    return key


def send_error(exc, key, operation):
    from .tools import _safe_tool_error
    result = _safe_tool_error(exc)
    if key:
        result.update(idempotency_key=key, operation=operation,
                      recovery="Look up this request key before retrying. A missing lookup does not prove nothing was sent. Never use a new key to recover this send.")
    return result


def send_result(message, key, operation):
    from .tools import _json_safe
    return {"ok": True, "message_id": str(message.id), "idempotency_key": key, "operation": operation,
            "status": _json_safe(getattr(message, "delivery_status", None) or getattr(message, "status", None)),
            "accepted": True, "delivery_note": "Accepted is not confirmation of delivery. Read the message for current status."}


def email_attachments(args, *, allow_inline=True):
    from .tools import _safe_local_media_path
    entries = args.get("attachments", [])
    if not isinstance(entries, list) or len(entries) > 20:
        raise ValueError("attachments must contain at most 20 local files")
    result, total, cids = [], 0, set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"path", "content_id"}:
            raise ValueError("Each attachment requires path and an optional content_id")
        path = _safe_local_media_path(entry.get("path", ""), max_bytes=MAX_ATTACHMENT_BYTES, channel="email")
        with path.open("rb") as stream:
            content = stream.read(MAX_ATTACHMENT_BYTES - total + 1)
        total += len(content)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("Email attachments exceed 25 MiB total")
        item = {"filename": path.name, "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "content_base64": base64.b64encode(content).decode("ascii")}
        if "content_id" in entry:
            cid = entry["content_id"]
            if (not allow_inline or not isinstance(cid, str) or not cid.strip() or cid in cids
                    or not (args.get("body_html") or args.get("bodyHtml")) or not item["content_type"].startswith("image/")):
                raise ValueError("Inline attachments require HTML, an image, and a unique content_id; forwards do not support them")
            cids.add(cid)
            item["content_id"] = cid
        result.append(item)
    return result


def _schema(name, description, properties, required=()):
    return {"name": "inkbox_" + name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False,
    }}


_ID = {"type": "string", "format": "uuid"}
_PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
         "offset": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0}}
_DATES = {key: {"type": "string"} for key in ("start_datetime", "end_datetime", "tz")}
_RECIPIENTS = {key: {"type": "array", "items": {"type": "string"}} for key in ("to", "cc", "bcc")}
TOOLS = [
    _schema("list_emails", "Read a bounded email page, newest first. Unread and text filters apply within that page; follow next_offset even for no matches. Search covers sender, subject and snippet, not full bodies. Date bounds are applied by the API. Listing does not mark mail read.", {
        **_PAGE, **_DATES, "direction": {"type": "string", "enum": ["inbound", "outbound"]},
        "unread_only": {"type": "boolean"}, "q": {"type": "string"},
    }),
    _schema("get_email", "Read a full email and attachment metadata from this identity's mailbox. Reading inbound mail marks it read.", {"message_id": _ID}, ["message_id"]),
    _schema("get_email_thread", "Read thread metadata and chronological message summaries without marking mail read. Use get_email for full message bodies.", {"thread_id": _ID}, ["thread_id"]),
    _schema("download_email_attachment", "Save one email attachment locally (up to 25 MiB). Use a filename from email attachment metadata.", {
        "message_id": _ID, "filename": {"type": "string", "minLength": 1},
    }, ["message_id", "filename"]),
    _schema("forward_email", "Forward a stored email, including original attachments by default. Preserve the request key for recovery.", {
        "message_id": _ID, **_RECIPIENTS, "mode": {"type": "string", "enum": ["inline", "wrapped"]},
        "subject": {"type": "string"}, "body_text": {"type": "string"}, "body_html": {"type": "string"},
        "include_original_attachments": {"type": "boolean", "default": True},
        "attachments": ATTACHMENTS, "idempotency_key": REQUEST_KEY,
    }, ["message_id", "to"]),
    _schema("list_calls", "List this identity's call history, newest first, including current status and available action items.", {**_PAGE, **_DATES}),
    _schema("get_call", "Read an accessible call's status, action items and transfer/conference connection history.", {"call_id": _ID}, ["call_id"]),
    _schema("get_call_transcripts", "Read a call's full ordered transcript, preserving available speaker phone-number attribution.", {"call_id": _ID}, ["call_id"]),
    _schema("hangup_call", "Request hangup of an active call. Teardown is asynchronous; read the call to confirm it ended.", {"call_id": _ID}, ["call_id"]),
    _schema("lookup_message_send", "Recover a message ID by its original request key without sending. A missing result does not prove the original request failed. Read the message to check delivery.", {
        "operation": {"type": "string", "enum": ["mail.send", "mail.reply_all", "mail.forward", "text.send", "imessage.send", "whatsapp.send"]},
        "idempotency_key": REQUEST_KEY,
    }, ["operation", "idempotency_key"]),
]


def _download_attachment(client, email, message_id, filename):
    if not isinstance(filename, str) or not filename or filename in {".", ".."} or any(c in filename for c in "/\\\x00"):
        raise ValueError("Provide a plain attachment filename from the message metadata")
    attachment = client.messages.get_attachment(email, message_id, quote(filename, safe=""))
    url = attachment["url"]
    if urlsplit(url).scheme != "https":
        raise ValueError("Attachment download requires an HTTPS URL")
    # Only the authenticated SDK supplies the URL; never forward API credentials to it.
    fd, output = tempfile.mkstemp(prefix="inkbox-attachment-", suffix=Path(filename).suffix[:20])
    try:
        size = 0
        with os.fdopen(fd, "wb") as target, httpx.stream("GET", url, timeout=30, follow_redirects=False) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    raise ValueError("Attachment exceeds 25 MiB")
                target.write(chunk)
        return {"file_path": output, "filename": filename, "size_bytes": size}
    except Exception:
        Path(output).unlink(missing_ok=True)
        raise


def dispatch(name, args, **_kwargs):
    from .tools import _client_and_identity, _json_safe, _safe_tool_error
    client, key = None, None
    try:
        spec = next(item for item in TOOLS if item["name"] == name)["parameters"]
        if set(args) - set(spec["properties"]) or any(k not in args for k in spec["required"]):
            raise ValueError("Invalid tool arguments")
        args = dict(args)
        for field in ("message_id", "thread_id", "call_id"):
            if field in args:
                args[field] = str(UUID(args[field]))
        if name in {"inkbox_list_emails", "inkbox_list_calls"}:
            for field, default, low, high in (("limit", 50, 1, 100), ("offset", 0, 0, 10000)):
                args.setdefault(field, default)
                if type(args[field]) is not int or not low <= args[field] <= high:
                    raise ValueError(f"{field} must be between {low} and {high}")
        _cfg, client, identity = _client_and_identity()
        if name in {"inkbox_list_emails", "inkbox_get_email", "inkbox_get_email_thread", "inkbox_download_email_attachment", "inkbox_forward_email"}:
            if identity.mailbox is None:
                raise ValueError("This identity has no mailbox")
            email = identity.mailbox.email_address
        if name == "inkbox_list_emails":
            if args.get("direction") not in {None, "inbound", "outbound"}:
                raise ValueError("Invalid direction")
            if "unread_only" in args and type(args["unread_only"]) is not bool:
                raise ValueError("unread_only must be boolean")
            query = args.get("q", "").casefold()
            iterator = identity.iter_emails(page_size=100, **{k: args[k] for k in (*_DATES, "direction") if k in args})
            offset, limit = args["offset"], args["limit"]
            page = list(islice(iterator, offset, offset + limit + 1))
            rows = page[:limit]
            matches = [row for row in rows if (not args.get("unread_only") or not row.is_read)
                       and (not query or query in " ".join(str(getattr(row, k, "") or "") for k in ("from_address", "subject", "snippet")).casefold())]
            result = {"messages": matches, "scanned_count": len(rows), "next_offset": offset + limit if len(page) > limit else None}
            if result["next_offset"] is not None and result["next_offset"] > 10000:
                result.update(next_offset=None, truncated=True, next_step="Narrow the date bounds to continue beyond the scan limit.")
        elif name == "inkbox_get_email":
            result = client.messages.get(email, args["message_id"])
        elif name == "inkbox_get_email_thread":
            result = client.threads.get(email, args["thread_id"])
        elif name == "inkbox_download_email_attachment":
            result = _download_attachment(client, email, args["message_id"], args["filename"])
        elif name == "inkbox_forward_email":
            if not isinstance(args["to"], list) or not args["to"]:
                raise ValueError("to must contain recipient email addresses")
            if args.get("mode", "inline") not in {"inline", "wrapped"}:
                raise ValueError("Invalid forward mode")
            attachments = email_attachments(args, allow_inline=False)
            key = request_key(args)
            message = identity.forward_email(args["message_id"], **{k: args[k] for k in (
                "to", "cc", "bcc", "mode", "subject", "body_text", "body_html", "include_original_attachments") if k in args},
                additional_attachments=attachments or None, idempotency_key=key)
            return json.dumps(send_result(message, key, "mail.forward"))
        elif name == "inkbox_list_calls":
            result = identity.list_calls(**args)
        elif name in {"inkbox_get_call", "inkbox_get_call_transcripts", "inkbox_hangup_call"}:
            if name == "inkbox_get_call_transcripts":
                result = identity.list_transcripts(args["call_id"])
            elif name == "inkbox_hangup_call":
                result = {"hangup_requested": True, "call": identity.hangup_call(args["call_id"])}
            else:
                result = client.calls.get(args["call_id"])
        else:
            operation = args["operation"]
            if operation not in spec["properties"]["operation"]["enum"]:
                raise ValueError("Invalid send operation")
            key = request_key(args)
            if operation.startswith("mail."):
                message_id = client.message_sends.lookup_email(identity.mailbox.email_address, operation=operation, idempotency_key=key)
            else:
                kind = {"text.send": "phone_number", "imessage.send": "imessage_identity", "whatsapp.send": "whatsapp_identity"}[operation]
                sender = identity.phone_number.id if kind == "phone_number" else identity.id
                message_id = client.message_sends.lookup(sender_kind=kind, sender_id=sender, operation=operation, idempotency_key=key)
            result = {"message_id": str(message_id), "operation": operation, "idempotency_key": key}
        return json.dumps({"ok": True, "result": _json_safe(result)}, ensure_ascii=False)
    except Exception as exc:
        if name == "inkbox_forward_email" and key:
            error = send_error(exc, key, "mail.forward")
        elif name == "inkbox_download_email_attachment" and isinstance(exc, httpx.HTTPError):
            error = {"error": "Attachment download failed; request fresh attachment access and try again."}
        else:
            error = _safe_tool_error(exc)
        return json.dumps(error)
    finally:
        if client is not None:
            client.close()


def register(ctx, configured):
    for spec in TOOLS:
        def handler(args, _name=spec["name"], **kwargs):
            return dispatch(_name, args, **kwargs)
        ctx.register_tool(spec["name"], "inkbox", spec, handler, check_fn=configured)
