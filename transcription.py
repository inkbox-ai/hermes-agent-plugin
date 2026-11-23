"""Wait for voice-note text without turning completion callbacks into new turns."""
from __future__ import annotations

import asyncio
import os
import copy
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .slack import SLACK_INCOMING_EVENTS, TRANSCRIPTION_FINISHED

RECEIVED = {"text.received": "text", "imessage.received": "imessage", "whatsapp.received": "whatsapp",
            **{event: "slack" for event in SLACK_INCOMING_EVENTS}}
FINISHED = TRANSCRIPTION_FINISHED


def message_of(envelope: dict[str, Any], channel: str) -> dict[str, Any]:
    """Read the original channel-specific message wrapper."""
    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        return {}
    if channel == "slack":
        return slack_message(data)
    value = data.get("text_message" if channel == "text" else "message")
    return value if isinstance(value, dict) else {}


def slack_message(data: dict[str, Any]) -> dict[str, Any]:
    """Present a Slack event's retained message ID and files as the shared message shape."""
    event = data.get("event")
    files = event.get("files") if isinstance(event, dict) else None
    text = event.get("text") if isinstance(event, dict) else None
    return {"id": data.get("message_id"), "connection_id": data.get("connection_id"),
            "conversation_id": data.get("conversation_id"), "text": text if isinstance(text, str) else "",
            "media": files if isinstance(files, list) else []}


def with_message(envelope: dict[str, Any], channel: str, message: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the envelope carrying the resolved message; Slack files keep their native place."""
    data = dict(envelope.get("data") or {})
    if channel == "slack":
        event = data.get("event")
        if isinstance(event, dict) and isinstance(event.get("files"), list):
            data["event"] = {**event, "files": message.get("media") or []}
    else:
        data["text_message" if channel == "text" else "message"] = message
    return {**envelope, "data": data}


def completion_channel(envelope: dict[str, Any]) -> str | None:
    """Correlate the shared callback with its original channel and message."""
    data = envelope.get("data")
    if not isinstance(data, dict) or data.get("channel") not in RECEIVED.values():
        return None
    channel = data["channel"]
    message_id = data.get("message_id")
    if isinstance(message_id, str) and message_id and message_of(envelope, channel).get("id") == message_id:
        return channel
    return None


def pending(message: dict[str, Any]) -> bool:
    """Only explicit pending metadata delays delivery; legacy audio does not."""
    return any(isinstance(item, dict) and isinstance(item.get("transcription"), dict)
               and item["transcription"].get("status") == "pending" for item in message.get("media") or [])


def merge_results(original: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Refresh only attachment results; never replace sender text or routing."""
    if not original.get("id") or str(current.get("id")) != str(original["id"]):
        return original
    for name in ("conversation_id", "connection_id", "agent_identity_id", "direction"):
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
        if not isinstance(value, dict) or value.get("error_code") == "not_audio":
            continue
        prefix = f"[Voice note {index + 1}: "
        text = value.get("text")
        if value.get("status") == "completed" and isinstance(text, str) and text.strip():
            rows.append(f"[Voice note {index + 1} — automatically transcribed]\n" + text)
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
        if os.getenv("INKBOX_HOSTED_MODE") == "1":
            return message
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
    if channel == "slack":
        return await poll_slack_message(adapter, message)
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


async def poll_slack_message(adapter: Any, message: dict[str, Any]) -> dict[str, Any]:
    """Read one retained Slack message by its ID; its files carry the current results."""
    from urllib.parse import quote

    connection_id = quote(str(message["connection_id"]), safe="")
    result = await asyncio.to_thread(
        adapter._inkbox._api_http.get, f"/slack/connections/{connection_id}/archive/messages",
        params={"message_id": str(message["id"])}, timeout=8,
    )
    rows = result.get("messages") if isinstance(result, dict) else None
    current = next((row for row in rows or [] if isinstance(row, dict) and str(row.get("id")) == str(message["id"])), None)
    if current is None:
        raise LookupError("Slack message is no longer retained")
    files = current.get("files")
    return {"id": current["id"], "connection_id": current.get("connection_id"),
            "conversation_id": current.get("conversation_id"), "media": files if isinstance(files, list) else []}


def receiver_starting(detail):
    """This specific refusal occurs before native admission or a durable reservation."""
    from aiohttp import web
    response = web.Response(status=503, text=detail)
    response["native_receiver_starting"] = True
    return response
