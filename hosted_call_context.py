"""Durable, sanitized settlement state for hosted-call follow-up turns."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_LOCK = threading.Lock()
_BINDING_FAILURES: Dict[tuple[str, int], str] = {}


def _root() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        home = Path(get_hermes_home())
    except ImportError:  # pragma: no cover - local import/test fallback
        configured = os.getenv("HERMES_HOME")
        home = Path(configured).expanduser() if configured else Path.home() / ".hermes"
    root = home / "inkbox_hosted_call_contexts"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _context_path(session_id: str) -> Path:
    return _root() / f"session-{_digest(session_id)}.json"


def _queue_path(session_id: str) -> Path:
    return _context_path(session_id).with_suffix(".queue.json")


def _settlement_path(call_id: str, attempt: int) -> Path:
    return _root() / f"call-{_digest(call_id)}-{attempt}.json"


def _atomic_write(path: Path, value: Any) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    path.chmod(0o600)


def _binding_failure_path(call_id: str, attempt: int) -> Path:
    return _root() / f"binding-{_digest(call_id)}-{attempt}.json"


def mark_hosted_binding_failure(
    call_id: str,
    attempt: int,
    remote_phone: str,
) -> None:
    """Fail closed when trusted hosted context cannot bind to a session."""
    with _LOCK:
        _BINDING_FAILURES[(_digest(call_id), attempt)] = _digest(remote_phone.strip())
        _atomic_write(_binding_failure_path(call_id, attempt), {
            "terminal": True,
            "target_digest": _digest(remote_phone.strip()),
        })


def _binding_failure_matches(args: Any) -> bool:
    arguments = args if isinstance(args, dict) else {}
    target = arguments.get("to")
    if isinstance(target, list):
        target = target[0] if len(target) == 1 else ""
    target = str(target or "").strip()
    target_digest = _digest(target) if target else ""
    with _LOCK:
        active_targets = set(_BINDING_FAILURES.values())
    if target_digest and target_digest in active_targets:
        return True
    for path in _root().glob("binding-*.json"):
        try:
            value = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if (
            target
            and isinstance(value, dict)
            and value.get("target_digest") == _digest(target)
        ):
            return True
    return False


def enqueue_hosted_turn_context(session_id: str, context: Dict[str, Any]) -> None:
    """Queue trusted call context until Hermes begins the matching turn."""
    path = _queue_path(session_id)
    with _LOCK:
        try:
            loaded = json.loads(path.read_text())
            queue = loaded if isinstance(loaded, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            queue = []
        identity = (
            str(context.get("call_id") or ""),
            int(context.get("attempt") or 1),
        )
        if any(
            isinstance(item, dict)
            and (
                str(item.get("call_id") or ""),
                int(item.get("attempt") or 1),
            ) == identity
            for item in queue
        ):
            return
        queue.append(context)
        _atomic_write(path, queue)


def activate_next_hosted_turn_context(session_id: str) -> Optional[Dict[str, Any]]:
    """Activate the next hosted context at Hermes' real turn boundary."""
    path = _queue_path(session_id)
    with _LOCK:
        try:
            loaded = json.loads(path.read_text())
            queue = loaded if isinstance(loaded, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not queue:
            return None
        context = queue.pop(0)
        if not isinstance(context, dict):
            return None
        if queue:
            _atomic_write(path, queue)
        else:
            path.unlink(missing_ok=True)
        _atomic_write(_context_path(session_id), context)
        return context


def _read_context(session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    try:
        value = json.loads(_context_path(session_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _failure_kind(payload: Dict[str, Any], status: str) -> str:
    """Classify only deterministic, pre-send errors as recoverable."""
    code = str(payload.get("error_code") or "").strip().lower()
    rule = str(payload.get("rule") or "").strip().lower()
    message = str(payload.get("error") or "").strip().lower()
    try:
        status_code = int(payload.get("status_code"))
    except (TypeError, ValueError):
        status_code = None
    terminal_codes = {
        "recipient_not_opted_in", "recipient_opted_out", "recipient_blocked",
        "invalid_phone_number", "carrier_rejected", "sender_sms_pending",
        "sender_sms_assignment_failed", "sender_not_registered",
        "sender_registration_required", "messaging_profile_disabled",
        "toll_free_sms_unsupported", "unauthorized", "forbidden",
    }
    if code in terminal_codes or rule == "duplicate_body":
        return "terminal"
    if status_code is not None and (status_code >= 500 or status_code in {408, 425, 429}):
        return "terminal"
    recoverable_content_rules = {
        "tool_progress_narration", "adult_content", "trading_financial",
        "crypto_content", "profanity", "test_message", "markdown_artifacts",
        "emoji_overload",
    }
    if (
        code == "message_blocked_spam_filter"
        and rule in recoverable_content_rules
    ):
        return "recoverable"
    if code == "sms_too_long":
        return "recoverable"
    local_argument_errors = (
        "`text` is required",
        "specify exactly one of `to` or `conversationid`",
        "`to` must include at least one recipient",
        "inkbox group texts support at most 8 recipients",
    )
    if any(marker in message for marker in local_argument_errors):
        return "recoverable"
    return "terminal"


def observe_hosted_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    status: str = "",
    **_kwargs: Any,
) -> None:
    """Record one sanitized final ``inkbox_send_sms`` observation."""
    if tool_name != "inkbox_send_sms":
        return
    context = _read_context(str(session_id))
    if context is None:
        context = _read_context(str(task_id))
    if not context or not context.get("sms_required"):
        return
    call_id = str(context.get("call_id") or "")
    attempt = int(context.get("attempt") or 1)
    if not call_id:
        return
    parsed: Dict[str, Any] = {}
    if isinstance(result, str):
        try:
            value = json.loads(result)
            parsed = value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass
    elif isinstance(result, dict):
        parsed = result
    arguments = args if isinstance(args, dict) else {}
    target = arguments.get("to")
    if isinstance(target, list):
        target = target[0] if len(target) == 1 else ""
    target = str(target or "").strip()
    ok = parsed.get("ok") is True and str(status or "ok").lower() == "ok"
    observation = {
        "tool_call_id": _digest(str(tool_call_id or "missing")),
        "target_matches": target == str(context.get("remote_phone") or "").strip(),
        "ok": ok,
        "error_kind": "none" if ok else _failure_kind(parsed, status),
    }
    path = _settlement_path(call_id, attempt)
    with _LOCK:
        try:
            loaded = json.loads(path.read_text())
            observations = loaded if isinstance(loaded, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            observations = []
        pending_index = next((
            index
            for index, item in enumerate(observations)
            if isinstance(item, dict)
            and item.get("tool_call_id") == observation["tool_call_id"]
            and item.get("pending") is True
        ), None)
        if pending_index is None:
            observations.append(observation)
        else:
            observations[pending_index] = observation
        _atomic_write(path, observations[:10])


def observe_hosted_tool_start(
    *,
    tool_name: str = "",
    args: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Persist a sanitized pending marker before an SMS side effect starts."""
    if tool_name != "inkbox_send_sms":
        return
    if _binding_failure_matches(args):
        return {
            "action": "block",
            "message": "Hosted-call SMS context is unavailable; send blocked safely.",
        }
    context = _read_context(str(session_id))
    if context is None:
        context = _read_context(str(task_id))
    if not context or not context.get("sms_required"):
        return
    call_id = str(context.get("call_id") or "")
    attempt = int(context.get("attempt") or 1)
    if not call_id:
        return
    arguments = args if isinstance(args, dict) else {}
    target = arguments.get("to")
    if isinstance(target, list):
        target = target[0] if len(target) == 1 else ""
    target_matches = (
        str(target or "").strip()
        == str(context.get("remote_phone") or "").strip()
        and not (
            arguments.get("conversationId")
            or arguments.get("conversation_id")
        )
    )
    observation = {
        "tool_call_id": _digest(str(tool_call_id or "missing")),
        "target_matches": target_matches,
        "pending": True,
    }
    path = _settlement_path(call_id, attempt)
    with _LOCK:
        try:
            loaded = json.loads(path.read_text())
            observations = loaded if isinstance(loaded, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            observations = []
        if observations:
            observations.append({
                **observation,
                "pending": False,
                "ok": False,
                "error_kind": "terminal",
            })
            _atomic_write(path, observations[:10])
            return {
                "action": "block",
                "message": "Hosted-call SMS already attempted; duplicate blocked.",
            }
        if not target_matches:
            observation.update({
                "pending": False,
                "ok": False,
                "error_kind": "terminal",
            })
            observations.append(observation)
            _atomic_write(path, observations[:10])
            return {
                "action": "block",
                "message": "Hosted-call SMS recipient mismatch; send blocked safely.",
            }
        observations.append(observation)
        _atomic_write(path, observations[:10])


def hosted_sms_settlement(call_id: str, attempt: int) -> str:
    """Return success, missing, recoverable, or terminal for one model turn."""
    with _LOCK:
        failed_in_process = (_digest(call_id), attempt) in _BINDING_FAILURES
    if failed_in_process:
        return "terminal"
    if _binding_failure_path(call_id, attempt).exists():
        return "terminal"
    try:
        loaded = json.loads(_settlement_path(call_id, attempt).read_text())
        observations = loaded if isinstance(loaded, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        observations = []
    if not observations:
        return "missing"
    if len(observations) != 1:
        return "terminal"
    observation = observations[0]
    if observation.get("ok") is True:
        return "success" if observation.get("target_matches") is True else "terminal"
    return "recoverable" if observation.get("error_kind") == "recoverable" else "terminal"


def clear_hosted_call_context(call_id: str) -> None:
    """Remove sanitized per-call attempt observations after terminal settlement."""
    with _LOCK:
        for attempt in (1, 2):
            _BINDING_FAILURES.pop((_digest(call_id), attempt), None)
            _settlement_path(call_id, attempt).unlink(missing_ok=True)
            _binding_failure_path(call_id, attempt).unlink(missing_ok=True)
        for path in _root().glob("session-*.json"):
            try:
                loaded = json.loads(path.read_text())
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
            if path.name.endswith(".queue.json"):
                queue = loaded if isinstance(loaded, list) else []
                remaining = [
                    item for item in queue
                    if not (
                        isinstance(item, dict)
                        and str(item.get("call_id") or "") == call_id
                    )
                ]
                if remaining:
                    _atomic_write(path, remaining)
                else:
                    path.unlink(missing_ok=True)
            elif isinstance(loaded, dict) and str(loaded.get("call_id") or "") == call_id:
                path.unlink(missing_ok=True)
