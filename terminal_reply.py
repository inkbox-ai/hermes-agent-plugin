"""Terminal reply intent tied to an admitted message and durable receipt."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from inkbox.remote_mcp import RemoteToolError

NAME = "inkbox_reply_current"
DESCRIPTOR = {
    "name": NAME,
    "description": "Send the final text reply to this turn's original conversation. The destination comes from the incoming message. Use ordinary send tools for progress or unrelated messages. Reuse the same text when reconciling an uncertain reply.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 12000}},
        "required": ["text"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
}


def source(mode, metadata):
    if not isinstance(metadata, dict) or metadata.get("companion"):
        return None
    source_id = metadata.get("message_id") or metadata.get("imessage_source_id") or metadata.get("event_id")
    if not isinstance(source_id, str) or not 1 <= len(source_id) <= 256:
        return None
    if mode not in {"email", "sms"}:
        return None
    return {
        "channel": mode,
        "source_id": source_id,
        **{
            key: metadata[key]
            for key in ("to", "sender", "conversation_id", "conversation_kind", "recipients")
            if key in metadata
        },
    }


def source_hash(bound):
    return hashlib.sha256(json.dumps([bound["channel"], bound["source_id"]], separators=(",", ":")).encode()).hexdigest()


def blocked(remote, bound, journal_path):
    if not bound:
        return False
    try:
        receipt = remote.terminal_replies(journal_path=journal_path).lookup(source_hash(bound))
    except Exception:
        logging.getLogger(__name__).warning("Terminal reply receipt unavailable; automatic reply withheld")
        return True
    return bool(receipt and (receipt["state"] == "accepted" or receipt["state"] == "pending" and receipt["submitted"]))


async def send(remote, client, identity_handle, bound, arguments, journal_path, validate):
    if not bound or set(arguments) != {"text"} or not isinstance(arguments["text"], str) or not arguments["text"].strip():
        raise RemoteToolError("This terminal reply needs an admitted message and nonempty text")
    text = arguments["text"]
    if len(text) > 12000:
        raise RemoteToolError("The terminal reply is too long")
    validate()
    identifier = source_hash(bound)
    operation = "terminal:" + identifier + ":" + hashlib.sha256(text.encode()).hexdigest()[:32]
    journal = remote.terminal_replies(journal_path=journal_path)
    prior = journal.lookup(identifier)
    if prior and prior["state"] == "accepted":
        return {
            "content": [{"type": "text", "text": "The terminal reply was already accepted."}],
            "structuredContent": {"accepted_id": prior["accepted_id"]},
        }
    if bound["channel"] == "email":
        identity = await asyncio.wait_for(asyncio.to_thread(client.get_identity, identity_handle), 5)
        original = await asyncio.wait_for(asyncio.to_thread(identity.get_message, bound["source_id"]), 5)
        recipients = getattr(original, "reply_all_recipients", None)
        if (
            str(getattr(original, "id", "")) != bound["source_id"]
            or str(getattr(original, "direction", "")) != "inbound"
            or recipients is None
        ):
            raise RemoteToolError("The original email reply destination is unavailable")
        payload = {
            "message_id": bound["source_id"],
            "reply_all": True,
            "approved_recipients": {"to": list(recipients.to), "cc": list(recipients.cc), "bcc": []},
            "body_text": text,
            "idempotency_key": operation,
        }
        tool = "inkbox_email_reply"
    else:
        recipients = bound.get("recipients") or (
            [bound.get("to") or bound.get("sender")] if bound.get("conversation_kind") != "group" else []
        )
        if not recipients or any(not isinstance(value, str) or not value.startswith("+") for value in recipients):
            raise RemoteToolError("The original text-message recipients are unavailable")
        payload = {"to": recipients, "text": text, "idempotency_key": operation}
        tool = "inkbox_text_send"
    digest = hashlib.sha256(json.dumps([tool, payload], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    validate()
    journal.reserve(identifier, operation, digest)

    def submit():
        validate()
        journal.mark_submitted(identifier, operation)

    try:
        result = await remote.call(
            tool, payload, call_identity=operation, journal_path=journal_path, before_execute=validate, before_submit=submit
        )
        if result.get("isError"):
            if not journal.lookup(identifier)["submitted"]:
                journal.reject(identifier, operation)
            return result
        receipt = result.get("structuredContent")
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("id"), str)
            or receipt.get("direction") != "outbound"
            or receipt.get("is_blocked") is True
        ):
            raise RemoteToolError("The reply outcome is unconfirmed; retain the original operation")
        if bound["channel"] == "email":
            same = all(
                {str(value).lower() for value in receipt.get(field) or []}
                == {str(value).lower() for value in payload["approved_recipients"][bucket]}
                for field, bucket in (("to_addresses", "to"), ("cc_addresses", "cc"))
            )
        else:
            actual = [item.get("recipient_phone_number") for item in receipt.get("recipients") or []]
            actual = actual or [receipt.get("remote_phone_number")]
            same = set(actual) == set(payload["to"])
        if not same:
            raise RemoteToolError("The reply receipt does not confirm the original destination")
        journal.accept(identifier, operation, receipt["id"])
        return result
    except BaseException:
        current = journal.lookup(identifier)
        if current and current["state"] == "pending" and not current["submitted"]:
            journal.reject(identifier, operation)
        raise


def route_source(route):
    return source(route.get("mode"), {**route, "message_id": route.get("stored_message_id") or route.get("message_id")})


def journal_path(chat_id):
    from .config import inkbox_state_path

    return (
        inkbox_state_path().parent
        / "remote_calls"
        / ("reply-" + hashlib.sha256(str(chat_id).encode()).hexdigest() + ".sqlite")
    )


def adapter_blocked(adapter, route):
    from inkbox.remote_mcp import RemoteMcp

    bound = route_source(route)
    if not bound or not getattr(adapter, "_identity_id", None):
        return False
    path = journal_path(route["chat_id"])
    remote = RemoteMcp(
        api_key=adapter._api_key,
        base_url=adapter._base_url or "https://inkbox.ai",
        identity=adapter._identity_id,
        journal_path=path,
    )
    return blocked(remote, bound, path)
