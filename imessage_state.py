"""Trusted native-turn reply context, shared by the gateway and Hermes tools."""
from __future__ import annotations

from contextlib import contextmanager
import inspect
import threading
from typing import Any

_CONTEXTS: dict[str, dict] = {}
_LOCK = threading.RLock()


def source_metadata(message: Any, event_id: str | None = None) -> dict:
    def value(name):
        item = message.get(name) if isinstance(message, dict) else getattr(message, name, None)
        return str(item) if item not in (None, "") else None
    source = {key: value(key) for key in ("id", "reply_to_message_id", "thread_id", "thread_root_message_id")}
    receipt = str(event_id or source["id"] or "")
    return {"imessage_reply_target": source["id"], "source_message_id": source["id"],
            "imessage_event_id": receipt, "imessage_event_ids": [receipt], "imessage_sources": [source],
            **{key: source[key] for key in ("reply_to_message_id", "thread_id", "thread_root_message_id")}}


def auto_reply_kwargs(meta: dict) -> dict:
    target = meta.get("imessage_reply_target")
    return {"reply_to_message_id": target, "plain_reply_fallback": True} if target else {}


def require_threading(identity: Any) -> None:
    try:
        supported = all(callable(getattr(identity, name, None)) for name in (
            "get_imessage_thread", "get_imessage_conversation_thread", "get_imessage", "send_imessage",
        )) and {"reply_to_message_id", "plain_reply_fallback"} <= set(inspect.signature(identity.send_imessage).parameters)
    except (TypeError, ValueError):
        supported = False
    if not supported:
        raise RuntimeError("Native iMessage replies require Inkbox Python SDK 0.7.13 or newer with native reply and thread support")


def bind_context(session_id: str, context: dict) -> None:
    with _LOCK:
        prior = _CONTEXTS.get(session_id)
        if prior and prior.get("leases"):
            raise RuntimeError("Previous iMessage tool execution has not settled")
        _CONTEXTS[session_id] = {**context, "active": True, "leases": 0, "explicit_sends": []}


def active_context(session_id: str) -> dict | None:
    with _LOCK:
        context = _CONTEXTS.get(str(session_id))
        try:
            from gateway.session_context import get_session_env
            native_source = get_session_env("HERMES_SESSION_MESSAGE_ID")
        except ImportError:
            native_source = ""
        if context and native_source and native_source != context.get("turn_id"):
            raise RuntimeError("The tool belongs to a different native iMessage turn")
        if context and not context.get("active"):
            raise RuntimeError("The original iMessage turn is no longer active")
        return context


def clear_context(session_id: str, turn_id: str) -> bool:
    with _LOCK:
        context = _CONTEXTS.get(session_id)
        if not context or context.get("turn_id") != turn_id:
            return True
        context["active"] = False
        # Keep the tombstone until the next native turn; a late old tool cannot
        # reinterpret cancellation as permission for an untargeted send.
        return not context["leases"]


def validate_target(context: dict | None, conversation_id: str | None, to: Any) -> None:
    if context is None:
        return
    if to is not None or str(conversation_id or "") != context.get("conversation_id"):
        raise ValueError("This iMessage turn may only access its original conversation; do not change its destination")


@contextmanager
def explicit_send(session_id: str, conversation_id: str | None, to: Any):
    with _LOCK:
        context = active_context(session_id)
        validate_target(context, conversation_id, to)
        if context:
            context["leases"] += 1
    try:
        yield context
    finally:
        if context:
            with _LOCK:
                context["leases"] -= 1


def record_explicit(context: dict | None, content: str, message: Any) -> None:
    if context is not None:
        message_id = str(getattr(message, "id", "") or "")
        if message_id:
            with _LOCK:
                context["explicit_sends"].append({"content": content, "message_id": message_id})
            callback = context.get("record_explicit")
            if callback:
                callback(content, message)


def lease_count(session_id: str) -> int:
    with _LOCK:
        return int((_CONTEXTS.get(str(session_id)) or {}).get("leases", 0))
