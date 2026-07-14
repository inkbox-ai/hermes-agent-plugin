"""Prevent explicit Inkbox replies from being auto-delivered twice.

Hermes automatically delivers the final model response back to the inbound
thread.  If an agent also calls ``inkbox_send_imessage`` for that same thread,
the tool delivery is already complete and the final response must be silent.
This module correlates those three lifecycle points without changing Hermes
core or suppressing confirmations for messages sent to a different recipient.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional


_ROUTE_TTL_SECONDS = 15 * 60
_SUPPRESSION_TTL_SECONDS = 5 * 60
_lock = threading.Lock()


@dataclass
class _InboundRoute:
    session_key: str
    conversation_id: str
    remote_number: str
    session_store: Any
    expires_at: float


_routes: dict[str, _InboundRoute] = {}
_suppress_final_for_session: dict[str, float] = {}


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _conversation_from_thread(thread_id: Any) -> str:
    value = str(thread_id or "").strip()
    if value.startswith("imessage:conversation:"):
        return value.split(":", 2)[2].strip()
    if value.startswith("imessage:"):
        return value.split(":", 1)[1].strip()
    return ""


def _prune(now: float) -> None:
    for key, route in list(_routes.items()):
        if route.expires_at <= now:
            _routes.pop(key, None)
    for session_id, expires_at in list(_suppress_final_for_session.items()):
        if expires_at <= now:
            _suppress_final_for_session.pop(session_id, None)


def record_inbound_route(*, event: Any, gateway: Any, session_store: Any, **_kwargs: Any) -> None:
    """Remember the current authorized iMessage route before agent dispatch."""
    source = getattr(event, "source", None)
    if source is None or _platform_value(getattr(source, "platform", None)) != "inkbox":
        return None
    conversation_id = _conversation_from_thread(getattr(source, "thread_id", None))
    if not conversation_id:
        return None
    try:
        session_key = str(gateway._session_key_for_source(source) or "").strip()
    except Exception:
        return None
    if not session_key:
        return None

    now = time.monotonic()
    route = _InboundRoute(
        session_key=session_key,
        conversation_id=conversation_id,
        remote_number=str(getattr(source, "user_id_alt", None) or "").strip(),
        session_store=session_store,
        expires_at=now + _ROUTE_TTL_SECONDS,
    )
    with _lock:
        _prune(now)
        _routes[session_key] = route
    return None


def _route_session_id(route: _InboundRoute) -> str:
    store = route.session_store
    try:
        store._ensure_loaded()
        with store._lock:
            entry = store._entries.get(route.session_key)
            return str(getattr(entry, "session_id", None) or "").strip()
    except Exception:
        return ""


def _successful_tool_result(result: Any) -> Optional[dict[str, Any]]:
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return None
    if not isinstance(parsed, dict) or not parsed.get("ok") or parsed.get("error"):
        return None
    return parsed


def note_imessage_tool_delivery(
    *,
    tool_name: str,
    args: Any,
    result: Any,
    session_id: str = "",
    status: str = "",
    **_kwargs: Any,
) -> None:
    """Arm one final-response suppression after a same-thread tool send."""
    if tool_name != "inkbox_send_imessage" or status not in {"", "ok"}:
        return None
    parsed = _successful_tool_result(result)
    if parsed is None or not isinstance(args, dict):
        return None
    session_id = str(session_id or "").strip()
    if not session_id:
        return None

    target_conversation = str(
        args.get("conversationId")
        or args.get("conversation_id")
        or parsed.get("conversation_id")
        or ""
    ).strip()
    target_number = str(args.get("to") or "").strip()

    now = time.monotonic()
    with _lock:
        _prune(now)
        routes = list(_routes.values())

    same_thread = False
    for route in routes:
        if _route_session_id(route) != session_id:
            continue
        same_thread = bool(
            (target_conversation and target_conversation == route.conversation_id)
            or (target_number and route.remote_number and target_number == route.remote_number)
        )
        break

    if same_thread:
        with _lock:
            _prune(now)
            _suppress_final_for_session[session_id] = now + _SUPPRESSION_TTL_SECONDS
    return None


def suppress_duplicate_final(
    *,
    response_text: str,
    session_id: str = "",
    platform: Any = "",
    **_kwargs: Any,
) -> Optional[str]:
    """Replace only the armed same-thread final response with ``[SILENT]``."""
    del response_text
    if _platform_value(platform) != "inkbox":
        return None
    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    now = time.monotonic()
    with _lock:
        _prune(now)
        expires_at = _suppress_final_for_session.pop(session_id, None)
    if expires_at is not None and expires_at > now:
        return "[SILENT]"
    return None


def _reset_for_tests() -> None:
    with _lock:
        _routes.clear()
        _suppress_final_for_session.clear()
