"""Wait for voice-note text without turning completion callbacks into new turns."""
from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

RECEIVED = {"text.received": "text", "imessage.received": "imessage", "whatsapp.received": "whatsapp"}
FINISHED = {f"{channel}.transcription_finished": channel for channel in RECEIVED.values()}


def message_of(envelope: dict[str, Any], channel: str) -> dict[str, Any]:
    """Read the original channel-specific message wrapper."""
    data = envelope.get("data") or {}
    value = data.get("text_message" if channel == "text" else "message")
    return value if isinstance(value, dict) else {}


def pending(message: dict[str, Any]) -> bool:
    """Only explicit pending metadata delays delivery; legacy audio does not."""
    return any(isinstance(item, dict) and isinstance(item.get("transcription"), dict)
               and item["transcription"].get("status") == "pending" for item in message.get("media") or [])


def merge_results(original: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Refresh only attachment results; never replace sender text or routing."""
    if not original.get("id") or str(current.get("id")) != str(original["id"]):
        return original
    for name in ("conversation_id", "agent_identity_id", "direction"):
        if original.get(name) is not None and current.get(name) is not None and str(original[name]) != str(current[name]):
            return original
    result = copy.deepcopy(original)
    media = current.get("media") or []
    by_id = {str(item["id"]): item for item in media if isinstance(item, dict) and item.get("id")}
    for index, item in enumerate(result.get("media") or []):
        if not isinstance(item, dict):
            continue
        match = by_id.get(str(item.get("id")))
        if match is None and not item.get("id") and len(media) == len(result["media"]):
            candidate = media[index]
            if isinstance(candidate, dict) and candidate.get("content_type") == item.get("content_type"):
                match = candidate
        value = match.get("transcription") if match else None
        if isinstance(value, dict) and value.get("status") in {"pending", "completed", "failed", "skipped"}:
            item["transcription"] = copy.deepcopy(value)
    return result


def voice_note_context(message: dict[str, Any]) -> str:
    """Label generated speech separately from the sender's original message."""
    rows = []
    for index, item in enumerate(message.get("media") or []):
        if not isinstance(item, dict):
            continue
        value = item.get("transcription")
        if not isinstance(value, dict):
            continue
        prefix = f"[Voice note {index + 1}: "
        text = value.get("text")
        if value.get("status") == "completed" and isinstance(text, str) and text.strip():
            rows.append(prefix + "automatically transcribed speech, not a typed message]\n" + text)
        elif value.get("status") == "completed":
            rows.append(prefix + "no speech detected; original attachment retained]")
        else:
            rows.append(prefix + "transcript unavailable; process the original attachment as usual]")
    return "\n".join(rows)


class VoiceNoteGate:
    """Share one bounded wait and poll across concurrent deliveries of a message."""

    def __init__(self, *, wait_seconds: float = 60, poll_seconds: float = 10) -> None:
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.entries: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    def _entry(self, channel: str, message_id: str) -> dict[str, Any]:
        now = time.monotonic()
        for key, entry in list(self.entries.items()):
            if now - entry["created"] > 300 and (entry.get("task") is None or entry["task"].done()):
                self.entries.pop(key)
        key = (channel, message_id)
        if key not in self.entries:
            if len(self.entries) >= 2048:
                oldest = next((key for key, entry in self.entries.items() if not entry.get("task") or entry["task"].done()), None)
                if oldest is None:
                    raise RuntimeError("Voice-note receiver is busy")
                self.entries.pop(oldest)
            self.entries[key] = {"created": now, "event": asyncio.Event()}
        return self.entries[key]

    def complete(self, channel: str, message: dict[str, Any]) -> None:
        """Retain out-of-order completion; it never creates an agent turn."""
        message_id = str(message.get("id") or "")
        if not message_id or pending(message):
            return
        entry = self._entry(channel, message_id)
        if entry.get("task") is not None and entry["task"].done():
            return
        entry["result"] = copy.deepcopy(message)
        entry["event"].set()

    def waiting(self, channel: str, message_id: str) -> bool:
        """A duplicate cannot acknowledge a receipt that has not been admitted yet."""
        task = (self.entries.get((channel, message_id)) or {}).get("task")
        return task is not None and not task.done()

    def remember(self, channel: str, message: dict[str, Any]) -> None:
        """Reuse a durable resolution when normal channel admission resumes."""
        entry = self._entry(channel, str(message["id"]))
        future = asyncio.get_running_loop().create_future()
        future.set_result(message)
        entry["task"] = future

    async def resolve(self, channel: str, message: dict[str, Any], poll: Callable[[], Awaitable[dict[str, Any]]],
                      *, deadline: float | None = None) -> dict[str, Any]:
        """Wait at most one minute, then make one bounded read before fallback."""
        if not pending(message) or not message.get("id"):
            return message
        entry = self._entry(channel, str(message["id"]))
        if "task" not in entry:
            entry["task"] = asyncio.create_task(self._resolve(entry, message, poll, deadline=deadline))
        return await asyncio.shield(entry["task"])

    async def _resolve(self, entry: dict[str, Any], message: dict[str, Any], poll: Callable[[], Awaitable[dict[str, Any]]],
                       *, deadline: float | None = None) -> dict[str, Any]:
        """Resolve the timeout/completion race in one owning coroutine."""
        wait_seconds = self.wait_seconds
        if deadline is not None:
            wait_seconds = min(wait_seconds, max(0, deadline - time.time()))
        created_at = message.get("created_at")
        if deadline is None and isinstance(created_at, str):
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created.tzinfo is not None:
                    wait_seconds = min(wait_seconds, max(0, created.timestamp() + self.wait_seconds - time.time()))
            except ValueError:
                pass
        try:
            if not entry["event"].is_set():
                await asyncio.wait_for(entry["event"].wait(), timeout=wait_seconds)
            candidate = merge_results(message, entry.get("result") or {})
            if not pending(candidate):
                return candidate
        except TimeoutError:
            pass
        try:
            result = await asyncio.wait_for(poll(), timeout=self.poll_seconds)
            refreshed = merge_results(message, result)
            return merge_results(refreshed, entry.get("result") or {})
        except Exception:
            return merge_results(message, entry.get("result") or {})

    async def close(self) -> None:
        """Release all pending waits when the receiver shuts down."""
        tasks = [entry["task"] for entry in self.entries.values() if entry.get("task") is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()


async def poll_message(adapter: Any, channel: str, message: dict[str, Any]) -> dict[str, Any]:
    """Use the authenticated JSON transport so older typed parsers lose no fields."""
    from urllib.parse import quote

    message_id = quote(str(message["id"]), safe="")
    if channel == "text":
        identity = getattr(adapter, "_reply_identity", None)
        phone = getattr(identity, "phone_number", None)
        phone_id = getattr(phone, "id", None)
        if phone_id is None:
            raise ValueError("No phone number available")
        path = f"/phone/numbers/{quote(str(phone_id), safe='')}/texts/{message_id}"
        params = None
    else:
        path = f"/{channel}/messages/{message_id}"
        params = {"agent_identity_id": str(adapter._identity_id)}
    return await asyncio.to_thread(adapter._inkbox._api_http.get, path, params=params, timeout=8)
