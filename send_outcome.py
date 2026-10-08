"""Bounded, read-only delivery observation after an accepted send."""
from __future__ import annotations

import asyncio
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any

FAILURES = {"declined", "error", "delivery_failed", "sending_failed", "blocked_spam_filter"}


def value(message: Any, *names: str):
    for name in names:
        item = message.get(name) if isinstance(message, dict) else getattr(message, name, None)
        if item is not None:
            return getattr(item, "value", item)
    return None


def outcome(message: Any, kind: str, *, group: bool = False) -> dict:
    status = value(message, "delivery_status", "deliveryStatus", "status") or "unknown"
    service = value(message, "service") if kind == "imessage" else (value(message, "type") or "sms")
    if kind == "imessage" and status in {"registered", "pending", "unknown"}:
        service = None
    group = group or value(message, "is_group", "isGroup") is True
    final = value(message, "delivery_final", "deliveryFinal")
    if not isinstance(final, bool):
        final = status in FAILURES or status == "delivered" or (
            kind == "sms" and status == "delivery_unconfirmed"
        ) or (kind == "imessage" and status == "sent" and (service == "sms" or group))
    detail = value(message, "error_detail", "errorDetail", "error_message", "errorMessage")
    if status in FAILURES:
        note = "Delivery failed: " + (str(detail) if detail else "the message could not be delivered.")
    elif status == "delivery_unconfirmed":
        note = "No delivery receipt was received; the outcome is unknown. Do not resend."
    elif status == "delivered":
        label = {"imessage": "iMessage", "rcs": "RCS", "sms": "SMS", "mms": "MMS"}.get(service, service)
        note = "Delivered" + (f" via {label}." if label else ".")
    elif status == "sent" and kind == "imessage" and (group or service == "sms"):
        note = "Sent" + (" to the group" if group else " as a text message") + ". No device delivery receipt is available."
    elif status == "sent":
        note = "Sent; a delivery receipt has not yet been received. Do not resend."
    else:
        note = "Still in flight. Re-read the message for the outcome; do not resend."
    return {"status": status, "service": service, "delivery_final": final,
            "error_code": value(message, "error_code", "errorCode"), "error_detail": detail, "note": note}


def _setting(name: str, default: float, maximum: float, minimum: float = 0) -> float:
    try:
        result = float(os.getenv(name, str(default)))
        return min(maximum, max(minimum, result)) if math.isfinite(result) else default
    except ValueError:
        return default


def _marker_path(message_id: str) -> Path:
    try:
        from .config import inkbox_state_path
    except ImportError:
        from config import inkbox_state_path
    root = inkbox_state_path().parent
    root = root / "send_outcomes"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / (hashlib.sha256(message_id.encode()).hexdigest() + ".json")


def _mark(message_id: str, state: str) -> None:
    if not message_id:
        return
    try:
        path = _marker_path(message_id)
        # Markers contain no message bodies or recipient information.
        temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with (path.parent / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = _state(message_id)
            if (state != "webhook" and previous == "webhook") or (state == "webhook" and previous == "inline"):
                return
            temp.write_text(json.dumps({"state": state, "at": time.time()}))
            temp.chmod(0o600)
            temp.replace(path)
        for old in path.parent.glob("*.json"):
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink(missing_ok=True)
    except OSError:
        pass  # An accepted send must never become a resendable tool error.


def _state(message_id: str) -> str | None:
    if not message_id:
        return None
    try:
        data = json.loads(_marker_path(message_id).read_text())
        age = time.time() - data["at"]
        if age > (11 if data["state"] == "polling" else 86400):
            return None
        return data["state"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


async def reported_inline(message_id: str) -> bool:
    """Let an in-progress send finish before deciding whether to wake a retry."""
    deadline = time.monotonic() + 10
    while _state(message_id) == "polling" and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if _state(message_id) == "inline":
        return True
    _mark(message_id, "webhook")
    return _state(message_id) == "inline"


def _read_with_deadline(getter, message_id: str, seconds: float):
    result = queue.Queue(maxsize=1)
    def run():
        try:
            result.put((True, getter(message_id)))
        except Exception as exc:
            result.put((False, exc))
    # SDK reads may have a longer network timeout than the observation window.
    # A timed-out read is abandoned; it can never initiate a second send.
    threading.Thread(target=run, daemon=True).start()
    ok, item = result.get(timeout=seconds)
    if not ok:
        raise item
    return item


def poll_send_outcome(client, identity, kind: str, message: Any, *, group: bool = False) -> dict:
    message_id = str(value(message, "id") or "")
    group = group or value(message, "is_group", "isGroup") is True
    result = outcome(message, kind, group=group)
    seconds = _setting("INKBOX_SEND_POLL_SECONDS", 5, 10)
    interval = _setting("INKBOX_SEND_POLL_INTERVAL_SECONDS", 0.5, 10, 0.05)
    deadline = time.monotonic() + seconds
    getter = getattr(identity, "get_text" if kind == "sms" else "get_imessage", None)
    if kind == "imessage" and not callable(getter):
        resource_get = getattr(getattr(client, "imessages", None), "get", None)
        identity_id = value(identity, "id")
        if callable(resource_get):
            getter = (lambda key: resource_get(key, agent_identity_id=identity_id)) if identity_id else resource_get
    _mark(message_id, "polling")
    try:
        if not message_id or not callable(getter):
            return result
        for _ in range(20):
            if result["delivery_final"]:
                break
            remaining = deadline - time.monotonic()
            if remaining <= interval:
                break
            time.sleep(interval)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                latest = _read_with_deadline(getter, message_id, remaining)
                if latest is None or (value(latest, "id") is not None and str(value(latest, "id")) != message_id):
                    break
                if value(latest, "direction") == "inbound":
                    break
                result = outcome(latest, kind, group=group)
            except Exception:
                break
        return result
    finally:
        _mark(message_id, "inline" if result["status"] in FAILURES else "done")
        if result["status"] in FAILURES and _state(message_id) == "webhook":
            result["note"] += " A delivery-failure update was already reported; do not start another retry from this result."
