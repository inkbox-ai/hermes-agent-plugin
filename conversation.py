"""Conversation gates and durable context without a model turn."""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextvars import ContextVar
from email.utils import getaddresses
from pathlib import Path
from typing import Any

reply_route: ContextVar[dict | None] = ContextVar("inkbox_reply_route", default=None)


def mode(adapter: Any, name: str) -> str:
    """Read and validate an independently configurable response gate."""
    choices, default = ({"auto", "mention"}, "auto") if name == "group_reply_mode" else ({"safe", "relaxed"}, "safe")
    value = str(getattr(getattr(adapter, "config", None), "extra", {}).get(name) or os.getenv("INKBOX_" + name.upper()) or default).strip().lower()
    if value not in choices:
        raise ValueError(f"INKBOX_{name.upper()} must be {' or '.join(sorted(choices))}")
    return value


def mentions(text: str, handle: str) -> bool:
    """Match whole mentions in text, never an email address or a URL."""
    text = re.sub(r"(?:https?://|www\.)\S+", " ", text, flags=re.I)
    return any(re.search(r"(?<![\w@.+-])@" + re.escape(token) + r"(?![\w-])(?!\.\w)", text, re.I)
               for token in {"agent", handle.strip().lstrip("@")} if token)


def same_author(channel: str, left: str, right: str) -> bool:
    """Email identity is case-insensitive; phone identity remains exact."""
    if not left or not right:
        return False
    if channel in {"mail", "email"}:
        return left.strip().casefold() == right.strip().casefold()
    return left == right


def raw_text(item: dict) -> str:
    return str(item.get("body_text") or item.get("body") or item.get("text") or item.get("content") or "")


def control_text(text: str, handle: str) -> str:
    parts = text.strip().split(maxsplit=1)
    if parts and parts[0].casefold().rstrip(",:") in {"@agent", "@" + handle.lstrip("@").casefold()}:
        return parts[1] if len(parts) == 2 else ""
    return text.strip()


def wakes(adapter: Any, channel: str, item: dict) -> bool:
    """Admission and addressing describe the current message, never history."""
    if mode(adapter, "companion_response_mode") == "safe" and item.get("sender_access") != "direct":
        return False
    if mode(adapter, "group_reply_mode") == "auto" or mentions(raw_text(item), adapter._identity_handle):
        return True
    if channel == "mail":
        own = {address.casefold() for address in getattr(adapter, "_identity_email_addresses", set())}
        return any(address.casefold() in own for _, address in getaddresses(
            [value for value in (item.get("to_addresses") or []) if isinstance(value, str)]))
    return False


class ConversationState:
    """Keep quiet inputs and original routes across gateway restarts."""

    def __init__(self, root: Path):
        self.root = root
        self.rows: dict[str, dict] = {}

    def row(self, key: str) -> dict:
        if key not in self.rows:
            path = self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")
            self.rows[key] = json.loads(path.read_text()) if path.exists() else {"quiet": [], "routes": {}}
        return self.rows[key]

    def save(self, key: str) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        temp = path.with_suffix(".tmp")
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
            json.dump(self.row(key), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    def remember(self, key: str, message_id: str, route: dict) -> None:
        self.row(key)["routes"][message_id] = route
        self.save(key)

    def quiet(self, key: str, message_id: str, text: str) -> None:
        row = self.row(key)
        if not any(item["id"] == message_id for item in row["quiet"]):
            row["quiet"].append({"id": message_id, "text": text})
            self.save(key)

    def consume(self, key: str, message_id: str) -> list[dict]:
        """Reserve context for one waking receipt, clearing only after completion."""
        row = self.row(key)
        for item in row["quiet"]:
            item.setdefault("turn", message_id)
        self.save(key)
        return [item for item in row["quiet"] if item.get("turn") == message_id]

    def complete(self, key: str, message_id: str) -> None:
        row = self.row(key)
        row["quiet"] = [item for item in row["quiet"] if item.get("turn") != message_id]
        self.save(key)
