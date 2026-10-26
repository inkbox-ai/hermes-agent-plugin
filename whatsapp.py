"""Approved one-to-one WhatsApp on the native Hermes message and delivery lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

EVENTS = (
    "whatsapp.received",
    "whatsapp.sent",
    "whatsapp.delivered",
    "whatsapp.delivery_failed",
    "whatsapp.delivery_unconfirmed",
)
MAX_MEDIA_BYTES = 20 * 1024 * 1024


def field(value: Any, key: str):
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def method(identity, name):
    result = getattr(identity, name, None)
    if not callable(result):
        raise RuntimeError("WhatsApp requires Inkbox SDK 0.7.21 or newer")
    return result


def unsupported(exc):
    detail = str(getattr(exc, "detail", exc)).lower()
    return (
        (isinstance(exc, ValueError) or getattr(exc, "status_code", None) in {400, 422})
        and "whatsapp" in detail
        and any(part in detail for part in ("event", "prefix", "unknown", "unsupported"))
    )


async def receive(adapter, envelope):
    from aiohttp import web
    from gateway.platforms.base import MessageEvent, MessageType
    from .adapter import (
        _chat_id_for_route,
        _single_agent_identity,
        _webhook_list,
        _parse_inkbox_timestamp,
        _escape_contact_memory_tokens,
        format_contact_memories,
    )

    data = envelope.get("data") or {}
    message = data.get("message") or {}
    message_id = str(message.get("id") or "")
    conversation = str(message.get("conversation_id") or "")
    remote = str(message.get("sender_number") or "")
    participants = message.get("participants") or []
    if (
        not message_id
        or not conversation
        or message.get("direction") != "inbound"
        or message.get("is_group")
        or len(participants) != 1
        or participants[0] != remote
        or remote != message.get("remote_number")
        or remote == message.get("local_number")
        or str(message.get("agent_identity_id") or "") != str(adapter._identity_id)
    ):
        return web.Response(status=200, text="Unsupported WhatsApp message")
    native = getattr(adapter, "_native_turns", None)
    if native is None:
        return web.Response(status=503, text="WhatsApp receiver is not ready")
    if native.acknowledge_control(message_id) or native.acknowledge_source(message_id):
        return web.Response(status=200, text="WhatsApp receipt retained")
    key = f"whatsapp:{message_id}"
    if adapter._dedup_begin(key):
        return web.Response(status=200, text="duplicate")
    try:
        media = message.get("media") or []
        raw = str(message.get("content") or "")
        if len(media) > 1 or (not raw.strip() and not media):
            adapter._dedup_commit(key)
            return web.Response(status=200, text="Empty WhatsApp message")
        contact = await adapter._resolve_contact_full(kind="phone", value=remote)
        sender = None if contact else _single_agent_identity(_webhook_list(data, "agent_identities", "agentIdentities"))
        chat_id = _chat_id_for_route(contact, f"whatsapp:{conversation}", remote)
        name = (contact or {}).get("name") or (sender or {}).get("name") or remote
        source = adapter.build_source(
            chat_id=str(chat_id),
            chat_name=name,
            chat_type="dm",
            user_id=str(chat_id),
            user_name=name,
            user_id_alt=remote,
            thread_id=f"whatsapp:{conversation}",
            message_id=message_id,
        )
        memories = adapter._contact_memories(envelope, contact, channel="whatsapp", sender=remote, is_group=False)
        marker = _escape_contact_memory_tokens(
            f"[inkbox:whatsapp from={remote} conversation_id={conversation} | {adapter._contact_marker(contact, sender)}]"
        )
        text = "\n".join(filter(None, [marker, format_contact_memories(memories), _escape_contact_memory_tokens(raw)]))
        prompt, skill = adapter._resolve_channel_overrides("whatsapp", chat_id, None)
        prompt = (
            (prompt or "")
            + "\nYour normal final reply is delivered here automatically. Do not also send it with a tool. Keep text under 4,000 characters. WhatsApp has no group, quoted-reply, reaction, typing, or call support."
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.COMMAND if raw.lstrip().startswith("/") else MessageType.TEXT,
            source=source,
            raw_message=envelope,
            message_id=message_id,
            metadata={},
            timestamp=_parse_inkbox_timestamp(message.get("created_at")),
            channel_prompt=prompt,
            auto_skill=skill,
            media_urls=[],
            media_types=[],
        )
        adapter._last_inbound_modality[str(chat_id)] = "whatsapp"
        if adapter._prepare_conversation_event(event):
            route = event.metadata["inkbox_reply_route"]
            route.update(
                raw_text=raw,
                identity_id=str(adapter._identity_id),
                base_url=str(adapter._base_url or "https://inkbox.ai").rstrip("/"),
                whatsapp_media_ids=[str(item["id"]) for item in media],
                imessage_event_ids=[str(envelope.get("id") or message_id)],
            )
            await adapter._accept_native_imessage(event)
    except BaseException:
        adapter._dedup_rollback(key)
        raise
    adapter._dedup_commit(key)
    return web.Response(status=202, text="WhatsApp receipt saved")


async def refresh_media(adapter, event, identifiers):
    """Resolve temporary access immediately before native host download, including recovery."""
    event.media_urls, event.media_types = [], []
    for media_id in identifiers:
        try:
            media = await asyncio.to_thread(method(adapter._reply_identity, "get_whatsapp_media"), media_id)
            if str(field(media, "status")) == "ready" and field(media, "url"):
                event.media_urls.append(field(media, "url"))
                event.media_types.append(field(media, "content_type") or "application/octet-stream")
                continue
        except Exception as exc:
            if getattr(exc, "status_code", None) in {401, 403}:
                raise
        event.text += "\n[WhatsApp attachment unavailable; its media ID remains retained]"


def record_status(adapter, envelope):
    if envelope.get("event_type") not in EVENTS[1:]:
        return
    message = (envelope.get("data") or {}).get("message") or {}
    native = getattr(adapter, "_native_turns", None)
    if (
        native is None
        or message.get("direction") != "outbound"
        or str(message.get("agent_identity_id") or "") != str(adapter._identity_id)
    ):
        return
    message_id = native._delivery_identifier(str(message.get("id") or ""))
    if not message_id:
        return
    with native._checkpoint_lock:
        prior = native.delivery_status.get(message_id, {})
        if prior.get("conversation_id") not in (None, message.get("conversation_id")):
            return
        from datetime import datetime, timezone

        def timestamp(value):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
            except ValueError:
                return None

        incoming, previous = timestamp(message.get("updated_at")), timestamp(prior.get("updated_at"))
        if incoming is None or (previous is not None and incoming < previous):
            return
        native.delivery_status[message_id] = {
            **prior,
            "mode": "whatsapp",
            "conversation_id": message.get("conversation_id"),
            "status": message.get("status"),
            "delivery_state": message.get("delivery_state"),
            "updated_at": message.get("updated_at"),
        }
        native._save_delivery_status()


async def send_media(adapter, chat_id, path, caption, metadata=None, filename=None):
    from gateway.platforms.base import SendResult

    native = getattr(adapter, "_native_turns", None)
    owned = native.media_owner(str(chat_id), metadata) if native else None
    if not owned:
        return SendResult(
            success=False, error="WhatsApp media requires its original native turn", raw_response={"inkbox_no_retry": True}
        )
    row, turn = owned
    if turn["route"]["mode"] != "whatsapp":
        return SendResult(
            success=False, error="WhatsApp media requires its original channel", raw_response={"inkbox_no_retry": True}
        )
    try:
        native.check_media_authority(row, turn)
        safe = adapter.validate_media_delivery_path(str(path))
        if not safe:
            raise ValueError("Local media path is not allowed")
        path = Path(safe)
        fingerprint = hashlib.sha256(json.dumps([str(path), filename, caption]).encode()).hexdigest()
        deliveries = turn.setdefault("media_deliveries", {})
        prior = deliveries.get(fingerprint)
        if prior and prior["state"] == "sent":
            return SendResult(success=True, message_id=prior["message_id"])
        if prior and prior["state"] in {"sending", "uncertain"}:
            return SendResult(
                success=False,
                error="WhatsApp media outcome is uncertain; use send lookup",
                raw_response={"inkbox_no_retry": True},
            )
        delivery = deliveries.setdefault(fingerprint, {"state": "uploading"})
        native._save(row)
        identity = adapter._reply_identity
        if not delivery.get("media_id"):

            def upload():
                native.check_media_authority(row, turn)
                with path.open("rb") as stream:
                    content = stream.read(MAX_MEDIA_BYTES + 1)
                if len(content) > MAX_MEDIA_BYTES:
                    raise ValueError("WhatsApp media must be at most 20 MiB")
                native.check_media_authority(row, turn)
                return method(identity, "upload_whatsapp_media")(
                    content=content,
                    filename=filename or path.name,
                    content_type=mimetypes.guess_type(filename or path.name)[0] or "application/octet-stream",
                )

            task = asyncio.create_task(asyncio.to_thread(upload))
            native.outbound.add(task)
            task.add_done_callback(native.outbound.discard)
            uploaded = await asyncio.shield(task)
            delivery.update(state="uploaded", media_id=str(field(uploaded, "id")))
            native._save(row)
        native.check_media_authority(row, turn)
        key = "hermes-wa-media:" + hashlib.sha256((turn["id"] + fingerprint).encode()).hexdigest()
        delivery.update(state="sending", idempotency_key=key)
        native._save(row)

        def checked():
            native.check_media_authority(row, turn, admitted_send=True)
            return method(identity, "send_whatsapp")(
                conversation_id=turn["route"]["conversation_id"],
                text=caption,
                media_ids=[delivery["media_id"]],
                idempotency_key=key,
            )

        task = asyncio.create_task(asyncio.to_thread(checked))
        native.outbound.add(task)
        task.add_done_callback(native.outbound.discard)
        result = await asyncio.shield(task)
        delivery.update(state="sent", message_id=str(result.id))
        native._save(row)
        native._record_outbound(row, turn, result)
        return SendResult(success=True, message_id=str(result.id))
    except Exception as exc:
        if "delivery" in locals() and delivery.get("state") == "sending":
            delivery["state"] = "uncertain"
            turn["state"] = "uncertain"
            native._save(row)
        return SendResult(
            success=False,
            error=f"WhatsApp media unavailable or unconfirmed ({type(exc).__name__}); do not resend",
            raw_response={"inkbox_no_retry": True},
        )


def public_quota_error(exc):
    """Keep only the public WhatsApp policy envelope, never arbitrary error bodies."""
    import re
    detail = getattr(exc, "detail", None)
    if getattr(exc, "status_code", None) not in {402, 403, 413, 422, 429}:
        return {}
    if not isinstance(detail, dict) or not re.fullmatch(r"whatsapp_[a-z0-9_]{1,120}", str(detail.get("error", ""))):
        return {}
    result = {"error": detail["error"]}
    quota = detail.get("quota")
    if isinstance(quota, str) and re.fullmatch(r"whatsapp\.[a-z0-9_.]{1,120}", quota):
        result["quota"] = quota
    for key in ("limit", "used"):
        if isinstance(detail.get(key), int) and not isinstance(detail[key], bool):
            result[key] = detail[key]
    condition = detail.get("retry_condition")
    if isinstance(condition, str):
        result["retry_condition"] = condition[:1000]
    reset = detail.get("reset_at")
    if isinstance(reset, str) and re.fullmatch(r"[0-9T:.+Z-]{20,40}", reset):
        result["reset_at"] = reset
    retry = getattr(exc, "retry_after_seconds", None)
    if isinstance(retry, int) and not isinstance(retry, bool) and retry >= 0:
        result["retry_after_seconds"] = retry
    return result
