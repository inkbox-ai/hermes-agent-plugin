"""Trusted turn context for inbound Inkbox A2A tasks."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_LOCK = threading.Lock()


def _context_root() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        home = Path(get_hermes_home())
    except ImportError:  # pragma: no cover - local import/test fallback
        configured = os.getenv("HERMES_HOME")
        home = Path(configured).expanduser() if configured else Path.home() / ".hermes"
    root = home / "inkbox_a2a_turn_contexts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _context_path(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return _context_root() / f"{digest}.json"


def _queue_path(session_id: str) -> Path:
    return _context_path(session_id).with_suffix(".queue.json")


def _atomic_write(path: Path, value: Any) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    path.chmod(0o600)


def write_a2a_turn_context(session_id: str, context: Dict[str, Any]) -> None:
    """Atomically bind a verified A2A task to one Hermes session."""
    with _LOCK:
        _atomic_write(_context_path(session_id), context)


def enqueue_a2a_turn_context(session_id: str, context: Dict[str, Any]) -> None:
    """Queue verified context until Hermes starts the corresponding turn."""
    path = _queue_path(session_id)
    with _LOCK:
        try:
            loaded = json.loads(path.read_text())
            queue = loaded if isinstance(loaded, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            queue = []
        identity = (
            str(context.get("task_id") or ""),
            str(context.get("message_id") or ""),
        )
        if any(
            (
                str(item.get("task_id") or ""),
                str(item.get("message_id") or ""),
            ) == identity
            for item in queue
            if isinstance(item, dict)
        ):
            return
        queue.append(context)
        _atomic_write(path, queue)


def activate_next_a2a_turn_context(session_id: str) -> Optional[Dict[str, Any]]:
    """Activate the next verified context at Hermes' real turn boundary."""
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


def remove_queued_a2a_turn_context(session_id: str, task_id: str) -> None:
    """Remove canceled work that has not reached the Hermes turn boundary."""
    path = _queue_path(session_id)
    with _LOCK:
        try:
            loaded = json.loads(path.read_text())
            queue = loaded if isinstance(loaded, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        remaining = [
            item
            for item in queue
            if not (
                isinstance(item, dict)
                and str(item.get("task_id") or "") == task_id
            )
        ]
        if remaining:
            _atomic_write(path, remaining)
        else:
            path.unlink(missing_ok=True)


def read_a2a_turn_context(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the verified A2A context for a Hermes session, when present."""
    if not session_id:
        return None
    path = _context_path(session_id)
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def mark_a2a_reply_committed(session_id: str) -> None:
    """Record that an explicit A2A intent tool already replied."""
    with _LOCK:
        path = _context_path(session_id)
        try:
            context = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(context, dict):
            return
        context["reply_intent_committed"] = True
        _atomic_write(path, context)
