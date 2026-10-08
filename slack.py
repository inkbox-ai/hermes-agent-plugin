"""Slack channel routing and the small SDK-backed tool surface."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from copy import copy
from typing import Any


SLACK_INCOMING_EVENTS = (
    "slack.dm_received",
    "slack.group_dm_received",
    "slack.channel_message_received",
    "slack.mention_received",
    "slack.thread_reply_received",
)
SLACK_ATTENTION_EVENTS = tuple(
    event for event in SLACK_INCOMING_EVENTS if event != "slack.channel_message_received"
)
SLACK_STOP_EVENT = "slack.session_stopped"
SLACK_SUBSCRIPTION_EVENTS = (*SLACK_INCOMING_EVENTS, SLACK_STOP_EVENT)
SLACK_MAX_TEXT_LENGTH = 12000


def authorize_sender(check: Any, source: Any, aliases=()) -> bool:
    """Resolve validated spellings through native policy, preserving its denies."""
    candidates = list(dict.fromkeys([source.user_id, *aliases]))
    for alias in candidates:
        candidate = copy(source)
        candidate.user_id = candidate.user_id_alt = alias
        candidate.role_authorized = True
        if not check(candidate):
            return False
    for alias in candidates:
        candidate = copy(source)
        candidate.user_id = candidate.user_id_alt = alias
        if check(candidate):
            source.user_id = source.user_id_alt = alias
            return True
    return False


def slack_resource(client: Any) -> Any:
    resource = getattr(client, "slack", None)
    if resource is None:
        raise RuntimeError("Slack requires an Inkbox SDK with Slack support")
    return resource


def reconcile_subscription(client: Any, identity_id: Any, url: str) -> None:
    slack_resource(client)
    subscriptions = client.webhooks.subscriptions
    events = list(SLACK_SUBSCRIPTION_EVENTS)
    covered = set()
    message_subscriptions = []
    for sub in subscriptions.list(agent_identity_id=identity_id, scope="identity", url=url):
        overlap = set(sub.event_types) & set(events)
        if sub.url == url and overlap:
            if sub.status != "active":
                raise RuntimeError("The Slack subscription is paused; resume it before starting")
            covered.update(overlap)
            if overlap.intersection(SLACK_INCOMING_EVENTS):
                message_subscriptions.append(sub)
    missing = [event for event in events if event not in covered]
    if not missing:
        return
    if message_subscriptions:
        # Extend the existing receiver: a second channel-only subscription
        # would deliver mentions again under another event category.
        sub = max(message_subscriptions, key=lambda item: len(set(item.event_types).intersection(SLACK_INCOMING_EVENTS)))
        subscriptions.update(sub.id, event_types=list(dict.fromkeys([*sub.event_types, *missing])), scope="identity")
        return
    # A test receiver must not replace another receiver or another channel.
    subscriptions.create(
        agent_identity_id=identity_id, url=url, event_types=missing,
    )


def inbound_message(envelope: dict, identity_id: str) -> tuple[str, str, dict] | None:
    data = envelope.get("data")
    if not isinstance(data, dict) or data.get("identity_id") != identity_id:
        return None
    # Older deliveries omit the label. An explicit denial/unknown label must
    # never become an ordinary request, and ride-alongs need their scoped grant.
    if "sender_access" in data and (
        data["sender_access"] not in ("direct", "sponsored")
        or (data["sender_access"] == "sponsored" and not envelope.get("companion"))
    ):
        return None
    event = data.get("event")
    if not isinstance(event, dict) or not envelope.get("id"):
        return None
    fields = ("connection_id", "workspace_id", "conversation_id", "actor_id", "message_ts")
    if any(not isinstance(data.get(key), str) or not data[key] for key in fields):
        return None
    # Only human messages wake the agent; bot echoes must not form reply loops.
    if event.get("bot_id") or event.get("app_id") or event.get("subtype") == "bot_message":
        return None
    # The selected event type may represent only one of a message's categories.
    kinds = data.get("message_kinds") or []
    if not isinstance(kinds, list):
        return None
    thread_ts = data.get("thread_ts")
    if thread_ts is not None and not isinstance(thread_ts, str):
        return None
    direct = "dm" in kinds
    # Keep ordinary DMs in the main conversation; native mentions can start a thread.
    root = thread_ts or (None if direct and "mention" not in kinds else data["message_ts"])
    # Timestamps are opaque strings: converting them to floats loses precision.
    chat_id = "slack:" + ":".join(
        [identity_id, data["connection_id"], data["conversation_id"], root or "dm"]
    )
    raw = event.get("text") or ""
    if not isinstance(raw, str):
        return None
    body = raw
    files = event.get("files")
    if isinstance(files, list) and files:
        body += "\nAttachment references (not downloaded): " + json.dumps([
            {key: file[key] for key in ("id", "name", "mimetype", "size") if key in file}
            for file in files if isinstance(file, dict)
        ])
    if not body.strip():
        return None
    meta = {
        **{key: data[key] for key in fields},
        "thread_ts": root,
        "sender": f"{data['workspace_id']}:{data['actor_id']}",
        "conversation_kind": "direct" if direct else "group",
        "raw_text": raw,
        "source_event_id": envelope["id"],
        "slack_mentioned": "mention" in kinds,
        "slack_addressed": bool(set(kinds) & {"dm", "group_dm", "mention"}),
    }
    profile = data.get("actor_profile")
    if (isinstance(profile, dict) and profile.get("id") == data["actor_id"]
            and isinstance(profile.get("team_id"), str)
            and re.fullmatch(r"T[A-Z0-9]{1,63}", profile["team_id"])):
        meta["recipient_team_id"] = profile["team_id"]
    if "sender_access" in data:
        meta["sender_access"] = data["sender_access"]
    # Sender context never changes workspace/thread isolation or approval ownership.
    sender_context = {}
    if isinstance(data.get("contact_id"), str):
        sender_context["contact_id"] = data["contact_id"]
    actor = data.get("actor_profile")
    if isinstance(actor, dict) and actor.get("id") == data["actor_id"]:
        profile = actor.get("profile")
        profile = profile if isinstance(profile, dict) else {}
        sender_context.update({
            key: profile[key] for key in ("display_name", "real_name", "email", "phone", "title")
            if isinstance(profile.get(key), str) and profile[key]
        })
    if sender_context:
        meta["slack_sender_context"] = sender_context
    return chat_id, body, meta


def inbound_stop(envelope: dict, identity_id: str) -> tuple[str, str, dict] | None:
    data = envelope.get("data")
    if not isinstance(data, dict) or envelope.get("event_type") != SLACK_STOP_EVENT:
        return None
    event = data.get("event")
    if not isinstance(event, dict) or event.get("type") != "agent_session_stopped":
        return None
    thread = data.get("thread_ts")
    if not isinstance(thread, str) or not thread:
        return None
    # Native controls carry null access, not a contact-rule admission label.
    # Their authority comes from the active request's exact actor and route.
    control = {key: value for key, value in data.items() if key != "sender_access"}
    incoming = inbound_message({**envelope, "data": {
        **control, "message_ts": thread, "message_kinds": ["thread"],
        "event": {"type": "message", "text": "/stop"},
    }}, identity_id)
    if incoming is not None:
        incoming[2]["slack_native_stop"] = True
    return incoming


def send_reply(client: Any, meta: dict, text: str) -> Any:
    if not text or len(text) > SLACK_MAX_TEXT_LENGTH or "\x00" in text:
        raise ValueError("Slack text must be 1–12000 characters without NUL characters")
    coordinates = [meta["connection_id"], meta["conversation_id"], meta.get("thread_ts")]
    key = hashlib.sha256(json.dumps(
        [meta["source_event_id"], coordinates, text], separators=(",", ":")
    ).encode()).hexdigest()
    action = slack_resource(client).send_message(
        meta["connection_id"], conversation_id=meta["conversation_id"],
        thread_ts=meta.get("thread_ts"), text=text, idempotency_key=f"hermes:{key}",
    )
    if action.status != "sent":
        raise RuntimeError(
            f"Slack action {action.id} has status {action.status}; inspect it with "
            "inkbox_slack_get_action before deciding whether to send again"
        )
    return action


SLACK_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def file_payload(file_path: str, *, filename: str | None = None, title: str | None = None,
                 initial_comment: str | None = None, validator=None) -> dict:
    """Read a host-approved local file with a bounded read; never fetch URLs."""
    if validator is None:
        from gateway.platforms.base import validate_media_delivery_path
        validator = validate_media_delivery_path
    safe_path = validator(file_path)
    if not safe_path:
        raise ValueError("Local media path is missing, unsafe, or not allowed")
    path = Path(safe_path)
    filename = path.name if filename is None else filename
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 255 or filename in {".", ".."}
            or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in filename)):
        raise ValueError("Provide a plain filename of 1–255 characters without paths or controls")
    for key, value, maximum in (("title", title, 255), ("initial_comment", initial_comment, SLACK_MAX_TEXT_LENGTH)):
        if value is not None and (not isinstance(value, str) or len(value) > maximum or "\x00" in value):
            raise ValueError(f"{key} must be at most {maximum} characters without NUL characters")
    if not path.is_file() or not 1 <= path.stat().st_size <= SLACK_MAX_UPLOAD_BYTES:
        raise ValueError("Slack file must contain between 1 byte and 10 MiB")
    with path.open("rb") as stream:
        content = stream.read(SLACK_MAX_UPLOAD_BYTES + 1)
    if not 1 <= len(content) <= SLACK_MAX_UPLOAD_BYTES:
        raise ValueError("Slack file must contain between 1 byte and 10 MiB")
    return {"filename": filename, "content_base64": base64.b64encode(content).decode("ascii"),
            **({"title": title} if title is not None else {}),
            **({"initial_comment": initial_comment} if initial_comment is not None else {})}


def operation_summary(operation: Any) -> dict:
    """Keep the inspection coordinates and outcome; never return file content."""
    return {key: str(value) if key in {"id", "connection_id"} else value
            for key in ("id", "connection_id", "operation", "status", "conversation_id", "thread_ts",
                        "message_ts", "file_id", "error_code", "retry_after")
            if (value := getattr(operation, key, None)) is not None}


def upload_reply(client: Any, meta: dict, payload: dict, *, idempotency_key: str) -> Any:
    method = getattr(slack_resource(client), "upload_file", None)
    if not callable(method):
        raise RuntimeError("Slack files require inkbox>=0.7.15,<1.0.0; upgrade the installed SDK")
    return method(meta["connection_id"], conversation_id=meta["conversation_id"],
                  thread_ts=meta.get("thread_ts"), idempotency_key=idempotency_key, **payload)


SLACK_MAX_PREVIEW_BYTES = 2 * 1024 * 1024


def download_file_preview(resource: Any, connection_id: str, file_id: str) -> dict:
    """Save an authenticated cached preview, never a caller-supplied URL or path."""
    method = getattr(resource, "download_file_preview", None)
    if not callable(method):
        raise RuntimeError("Slack previews require inkbox>=0.7.15,<1.0.0; upgrade the installed SDK")
    data = method(connection_id, file_id)
    if not isinstance(data, bytes) or not 1 <= len(data) <= SLACK_MAX_PREVIEW_BYTES:
        raise ValueError("Slack preview must contain between 1 byte and 2 MiB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext, mimetype = ".png", "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext, mimetype = ".jpg", "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        ext, mimetype = ".gif", "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        ext, mimetype = ".webp", "image/webp"
    else:
        raise ValueError("Slack preview is not a supported PNG, JPEG, GIF or WebP image")
    from gateway.platforms.base import cache_image_from_bytes
    path = cache_image_from_bytes(data, ext=ext)
    return {"connection_id": connection_id, "file_id": file_id, "file_path": path,
            "mimetype": mimetype, "size_bytes": len(data), "preview_only": True,
            "next_step": "Open file_path with vision_analyze to inspect the image. This is a preview, not necessarily the original file."}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": "inkbox_slack_" + name, "description": description,
        "inputSchema": {"type": "object", "properties": properties,
                        "required": required, "additionalProperties": False},
    }


_STRING = {"type": "string", "minLength": 1}
_CONNECTION = {"connection_id": _STRING}
_CONVERSATION = {**_CONNECTION, "conversation_id": _STRING}
_PAGE = {"cursor": _STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}
SLACK_TOOLS = [
    _tool("list_connections", "List this identity's Slack workspace connections.", {}, []),
    _tool("list_conversations", "List accessible Slack conversations; follow next_cursor.",
          {**_CONNECTION, **_PAGE}, ["connection_id"]),
    _tool("list_messages", "Read a Slack conversation or thread; follow next_cursor.",
          {**_CONVERSATION, **_PAGE, "thread_ts": _STRING}, ["connection_id", "conversation_id"]),
    _tool("download_file_preview", "Download a Slack attachment's cached image preview to a local file. "
          "Use connection_id and file_id from Slack metadata, then open the returned file_path with vision_analyze. "
          "Accepts PNG/JPEG/GIF/WebP up to 2 MiB. A preview is not necessarily the original file; "
          "unavailable previews return an error. No URLs or output paths accepted.",
          {"connection_id": {**_STRING, "pattern": "^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"},
           "file_id": {**_STRING, "pattern": "^F[A-Z0-9]{1,63}$"}}, ["connection_id", "file_id"]),
    _tool("search", "Search retained Slack text across connected workspaces. Not complete workspace "
          "history or file contents. Follow next_cursor even on an empty page.",
          {**_CONVERSATION, **_PAGE, "q": _STRING}, ["q"]),
    _tool("send_message", "Send a Slack message only when explicitly requested. Ordinary replies "
          "are automatic. Reuse idempotency_key for retries of the exact same message. "
          "For sending/unknown outcomes inspect get_action; never blindly resend.",
          {**_CONVERSATION, "text": {"type": "string", "minLength": 1, "maxLength": SLACK_MAX_TEXT_LENGTH},
           "thread_ts": _STRING, "idempotency_key": {
               "type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"}},
          ["connection_id", "conversation_id", "text", "idempotency_key"]),
    _tool("upload_file", "Upload a local image or file (1 byte–10 MiB) only when explicitly requested. "
          "Use file_path, not a URL; ordinary reply attachments are automatic. Reuse idempotency_key "
          "only for the exact same payload. Inspect get_operation after in_progress/unknown; never blindly resend.",
          {**_CONVERSATION, "file_path": _STRING, "filename": {**_STRING, "maxLength": 255},
           "title": {"type": "string", "maxLength": 255},
           "initial_comment": {"type": "string", "maxLength": SLACK_MAX_TEXT_LENGTH},
           "thread_ts": _STRING, "idempotency_key": {
               "type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"}},
          ["connection_id", "conversation_id", "file_path", "idempotency_key"]),
    _tool("get_operation", "Inspect a Slack file upload by operation ID. Poll only in_progress; "
          "unknown is terminal uncertainty, not proof of failure or permission to resend.",
          {**_CONNECTION, "operation_id": _STRING}, ["connection_id", "operation_id"]),
    _tool("get_action", "Inspect a Slack send outcome by action ID; unknown is not proof of failure.",
          {**_CONNECTION, "action_id": _STRING}, ["connection_id", "action_id"]),
]


def run_tool(client: Any, identity_handle: str, name: str, args: dict) -> Any:
    spec = next((tool for tool in SLACK_TOOLS if tool["name"] == name), None)
    if spec is None:
        raise ValueError(f"Unknown Slack tool: {name}")
    schema = spec["inputSchema"]
    if set(args) - set(schema["properties"]) or any(key not in args for key in schema["required"]):
        raise ValueError("Invalid Slack tool arguments")
    for key, value in args.items():
        if key == "limit":
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError("limit must be an integer from 1 to 100")
        elif not isinstance(value, str) or (not value.strip() and key not in {"title", "initial_comment"}):
            raise ValueError(f"{key} must be a nonempty string")
        elif "\x00" in value:
            raise ValueError(f"{key} must not contain NUL characters")
    if "idempotency_key" in args and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", args["idempotency_key"]):
        raise ValueError("Invalid Slack idempotency_key")
    if name == "inkbox_slack_download_file_preview":
        for key in ("connection_id", "file_id"):
            if not re.fullmatch(schema["properties"][key]["pattern"], args[key]):
                raise ValueError(f"Invalid Slack {key}")
    resource = slack_resource(client)
    identity = client.get_identity(identity_handle)
    if name == "inkbox_slack_download_file_preview":
        validate_connection(resource, str(identity.id), args)
        return download_file_preview(resource, args["connection_id"], args["file_id"])
    if name == "inkbox_slack_list_connections":
        return resource.list_connections(identity.id)
    # Even an organization-scoped credential must stay on the configured identity.
    connection = args.get("connection_id")
    if connection is not None and name not in {"inkbox_slack_upload_file", "inkbox_slack_get_operation"}:
        owned = resource.list_connections(identity.id).connections
        if not any(str(item.id) == connection for item in owned):
            raise ValueError("Slack connection does not belong to this identity")
    if name in {"inkbox_slack_upload_file", "inkbox_slack_get_operation"}:
        validate_connection(resource, str(identity.id), args)
        method = getattr(resource, name.removeprefix("inkbox_slack_"), None)
        if not callable(method):
            raise RuntimeError("Slack files require inkbox>=0.7.15,<1.0.0; upgrade the installed SDK")
        if name == "inkbox_slack_upload_file":
            payload = file_payload(args["file_path"], **{key: args[key] for key in
                ("filename", "title", "initial_comment") if key in args})
            return upload_reply(client, args, payload, idempotency_key=args["idempotency_key"])
    if name == "inkbox_slack_search":
        return resource.search_messages(identity_id=identity.id, **args)
    if name == "inkbox_slack_send_message":
        if len(args["text"]) > SLACK_MAX_TEXT_LENGTH:
            raise ValueError("Slack text must not exceed 12000 characters")
    return getattr(resource, name.removeprefix("inkbox_slack_"))(**args)


def validate_connection(resource: Any, identity_id: str, meta: dict) -> None:
    """Recheck current installation authority immediately before an effect."""
    connections = resource.list_connections(identity_id).connections
    matches = [connection for connection in connections if str(connection.id) == meta["connection_id"]]
    if (len(matches) != 1 or str(matches[0].identity_id) != str(identity_id)
            or matches[0].status != "connected"
            or meta.get("workspace_id") and matches[0].workspace_id != meta["workspace_id"]):
        raise PermissionError("Slack connection is no longer active for the original identity/workspace")
