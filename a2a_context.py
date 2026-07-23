"""Trusted turn context for inbound Inkbox A2A tasks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


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


def write_a2a_turn_context(session_id: str, context: Dict[str, Any]) -> None:
    """Atomically bind a verified A2A task to one Hermes session."""
    path = _context_path(session_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(context, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    path.chmod(0o600)


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
    context = read_a2a_turn_context(session_id)
    if context is None:
        return
    context["reply_intent_committed"] = True
    write_a2a_turn_context(session_id, context)
