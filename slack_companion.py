"""Resolve Slack Companion receipts through identity-scoped SDK reads."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass
from decimal import Decimal
import re
from typing import Any, get_args
from uuid import UUID

from .slack import SLACK_INCOMING_EVENTS


def require_sdk_support() -> None:
    """Require the Slack-aware initialization contract without changing other channels."""
    try:
        from inkbox.companion import CompanionChannel, CompanionReplyContext

        supported = (
            "slack" in get_args(CompanionChannel)
            and is_dataclass(CompanionReplyContext)
            and {"connection_id", "slack_conversation_id", "thread_ts"}
            <= {field.name for field in fields(CompanionReplyContext)}
        )
    except (ImportError, AttributeError, TypeError):
        supported = False
    if not supported:
        raise ValueError("Slack Companion requires Inkbox Python SDK 0.7.14 or newer with Slack initialization support")


def _value(value: Any, name: str) -> Any:
    """Read SDK dataclasses while accepting equivalent dictionary responses."""
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _uuid(value: Any) -> str:
    """Return a canonical UUID without accepting missing or malformed identifiers."""
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Slack Companion requires valid identity, connection, and source identifiers") from exc


def _identifier(value: Any, pattern: str) -> str:
    """Validate native Slack coordinates before using them in API requests."""
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Slack Companion has invalid Slack coordinates")
    return value


def _timestamp(value: Any) -> str:
    """Preserve Slack timestamp precision and its supported wire syntax."""
    return _identifier(value, r"[0-9]{1,12}\.[0-9]{1,6}")


def _thread_scope(message_ts: str, thread_ts: Any) -> str | None:
    """Keep thread parents on the timeline and replies in their exact thread."""
    if thread_ts is None:
        return None
    thread_ts = _timestamp(thread_ts)
    return None if thread_ts == message_ts else thread_ts


def _home_workspace(profile: Any, actor_id: str) -> str | None:
    """Use only matching provider-derived home-workspace evidence."""
    if not isinstance(profile, dict) or profile.get("id") != actor_id:
        return None
    team = profile.get("team_id")
    return team if isinstance(team, str) and re.fullmatch(r"T[A-Z0-9]{1,63}", team) else None


def prepare_envelope(client: Any, identity_handle: str, envelope: dict) -> dict:
    """Bind a signed Slack receipt to its current source and canonical author.

    The caller verifies the webhook signature. Local normalization data is
    always rebuilt; it is never accepted from the received envelope.
    """
    if not isinstance(envelope, dict) or envelope.get("event_type") not in SLACK_INCOMING_EVENTS:
        raise ValueError("Slack Companion requires an incoming Slack message")
    result = deepcopy(envelope)
    result.pop("_hermes_slack_source", None)
    data = result.get("data")
    if not isinstance(data, dict):
        raise ValueError("Slack Companion requires message data")
    identity_id = _uuid(data.get("identity_id"))
    connection_id = _uuid(data.get("connection_id"))
    workspace = _identifier(data.get("workspace_id"), r"T[A-Z0-9]{1,63}")
    conversation = _identifier(data.get("conversation_id"), r"[CGD][A-Z0-9]{1,63}")
    actor = _identifier(data.get("actor_id"), r"[UW][A-Z0-9]{1,63}")
    timestamp = _timestamp(data.get("message_ts"))
    thread = _thread_scope(timestamp, data.get("thread_ts"))

    identity = client.get_identity(identity_handle)
    if _uuid(_value(identity, "id")) != identity_id:
        raise ValueError("Slack Companion message does not belong to the configured identity")
    resource = getattr(client, "slack", None)
    if resource is None or not all(callable(getattr(resource, name, None)) for name in (
        "list_connections", "list_archived_messages", "get_user",
    )):
        raise ValueError("Slack Companion requires an Inkbox SDK with Slack archive and user reads")
    connections = _value(resource.list_connections(_value(identity, "id")), "connections")
    if not isinstance(connections, list):
        raise ValueError("Slack Companion could not verify its connection")
    matches = [item for item in connections if str(_value(item, "id")) == connection_id]
    if len(matches) != 1:
        raise ValueError("Slack Companion connection does not belong uniquely to this identity")
    connection = matches[0]
    if (_uuid(_value(connection, "identity_id")) != identity_id
            or _value(connection, "workspace_id") != workspace
            or _value(connection, "status") != "connected"):
        raise ValueError("Slack Companion connection does not match the active workspace and identity")
    bot = _identifier(_value(connection, "bot_user_id"), r"[UW][A-Z0-9]{1,63}")

    # Both bounds are exclusive; this window admits only the exact timestamp.
    instant, microsecond = Decimal(timestamp), Decimal("0.000001")
    lower, upper = instant - microsecond, instant + microsecond
    if lower < 0 or upper >= Decimal("1000000000000"):
        raise ValueError("Slack Companion message timestamp is outside the supported lookup range")
    page = resource.list_archived_messages(
        connection_id, conversation_id=conversation,
        after_ts=format(lower, ".6f"), before_ts=format(upper, ".6f"), limit=2,
    )
    messages = _value(page, "messages")
    if not isinstance(messages, list) or len(messages) != 1 or _value(page, "next_cursor") is not None:
        raise ValueError("Slack Companion source lookup is missing, ambiguous, or incomplete")
    source = messages[0]
    source_id = _uuid(_value(source, "id"))
    source_timestamp = _timestamp(_value(source, "message_ts"))
    if (_uuid(_value(source, "connection_id")) != connection_id
            or _value(source, "conversation_id") != conversation
            or Decimal(source_timestamp) != instant
            or _value(source, "user_id") != actor
            or _value(source, "source") != "event"
            or _thread_scope(source_timestamp, _value(source, "thread_ts")) != thread):
        raise ValueError("Slack Companion archived source does not match the received message")

    home = _home_workspace(data.get("actor_profile"), actor)
    if home is None:
        home = _home_workspace(resource.get_user(connection_id, actor), actor)
    if home is None:
        raise ValueError("Slack Companion could not verify the sender's home workspace")
    result["_hermes_slack_source"] = {
        "id": source_id, "author": f"{home}:{actor}", "thread_ts": thread, "bot_user_id": bot,
    }
    return result
