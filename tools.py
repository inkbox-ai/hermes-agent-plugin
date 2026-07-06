"""Inkbox tools registered by the Hermes plugin."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

try:
    from .config import inkbox_client_kwargs, object_summary, public_call_ws_url, read_config
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import inkbox_client_kwargs, object_summary, public_call_ws_url, read_config

try:
    from . import inkbox_lineage as lineage
    from . import turn_context
except ImportError:  # pragma: no cover - direct local import/test fallback
    import inkbox_lineage as lineage
    import turn_context

SMS_MAX_LENGTH = 1600
IMESSAGE_MAX_LENGTH = 18995

# Spin-off lineage caps: at most this many concurrently-open spin-offs per
# originating conversation, and a hard character cap on a relayed answer so a
# distilled result never turns into a full transcript dump.
SPINOFF_FANOUT_CAP = 5
RELAY_SUMMARY_MAX_CHARS = 2000

# Non-terminal edge statuses — an edge in one of these is still "in flight"
# (counts against the fan-out cap and shows in the parent's active list).
_SPINOFF_OPEN_STATUSES = (
    lineage.STATUS_SPAWNING,
    lineage.STATUS_DELIVERED,
    lineage.STATUS_AWAITING_REPLY,
    lineage.STATUS_ANSWERED,
    lineage.STATUS_STALE_HELD,
)


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _message_too_long_payload(channel: str, content: str, max_chars: int) -> Dict[str, Any]:
    char_count = len(content or "")
    return {
        "error": (
            f"{channel} text is {char_count} characters; maximum is {max_chars}. "
            f"Shorten it or split it into smaller {channel} messages."
        ),
        "error_code": f"{channel.lower()}_too_long",
        "char_count": char_count,
        "max_chars": max_chars,
    }


def _configured() -> bool:
    cfg = read_config()
    return bool(cfg.api_key and cfg.identity)


def _client_and_identity():
    from inkbox import Inkbox

    cfg = read_config()
    if not cfg.api_key:
        raise RuntimeError("INKBOX_API_KEY is not set")
    if not cfg.identity:
        raise RuntimeError("INKBOX_IDENTITY is not set")
    client = Inkbox(**inkbox_client_kwargs(cfg.api_key, cfg.base_url))
    return cfg, client, client.get_identity(cfg.identity)


def _normalize_recipients(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        return [trimmed] if trimmed else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _identity_method(identity: Any, snake_name: str, camel_name: Optional[str] = None):
    method = getattr(identity, snake_name, None)
    if callable(method):
        return method
    if camel_name:
        method = getattr(identity, camel_name, None)
        if callable(method):
            return method
    raise RuntimeError(f"Inkbox SDK identity has no {snake_name} method")


def _call_with_kwargs_or_payload(method, payload: Dict[str, Any], camel_payload: Optional[Dict[str, Any]] = None):
    try:
        return method(**payload)
    except TypeError:
        return method(camel_payload or payload)


def _call_with_key_and_options(method, key: str, options: Dict[str, Any], camel_options: Optional[Dict[str, Any]] = None):
    try:
        return method(key, **options)
    except TypeError:
        try:
            return method(key, camel_options or options)
        except TypeError:
            return method(key)


def _json_safe(value: Any) -> Any:
    """Convert SDK dataclasses (UUIDs, datetimes, enums) into JSON-safe data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_safe(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    return str(getattr(value, "value", value))


def _text_conversation_key(args: dict) -> Tuple[str, str, Optional[str]]:
    conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
    remote_phone = str(args.get("remotePhoneNumber") or args.get("remote_phone_number") or "").strip()
    if bool(conversation_id) == bool(remote_phone):
        return "", "", "Specify exactly one of `conversationId` or `remotePhoneNumber`."
    if conversation_id:
        return conversation_id, f"conversation {conversation_id}", None
    return remote_phone, f"conversation with {remote_phone}", None


def _append_query_param(raw_url: str, key: str, value: str) -> str:
    parts = urlparse(raw_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    return urlunparse(parts._replace(query=urlencode(query)))


def _write_outbound_call_context(params: Dict[str, Any]) -> str:
    from hermes_cli.config import get_hermes_home

    token = secrets.token_urlsafe(18)
    root = Path(get_hermes_home()) / "inkbox_call_contexts"
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": time.time(),
        "purpose": str(params.get("purpose") or "").strip(),
        "opening_message": str(params.get("opening_message") or params.get("openingMessage") or "").strip(),
        "context": str(params.get("context") or "").strip(),
        "to_number": str(params.get("to_number") or params.get("toNumber") or "").strip(),
    }
    # Only stamp an edge id when this call is a spin-off, so a plain (non-spin-off)
    # call writes a byte-for-byte identical capsule to before.
    edge_id = str(params.get("edge_id") or params.get("edgeId") or "").strip()
    if edge_id:
        payload["edge_id"] = edge_id
    (root / f"{token}.json").write_text(json.dumps(payload, indent=2) + "\n")
    return token


# ----------------------------------------------------------------------------
# Spin-off lineage — an optional `spinoff` object on the send tools turns a
# fire-and-forget send into a durable spawn edge (see inkbox_lineage.py). When
# `spinoff` is absent none of the helpers below run, so an ordinary send keeps
# today's behavior exactly.
# ----------------------------------------------------------------------------
logger = logging.getLogger("inkbox.plugin.tools")


def _identity_digits(value: str) -> str:
    """Return the trailing 10 digits of a phone-bearing identity string.

    Args:
        value (str): any string that may embed a phone number.

    Returns:
        str: up to the last 10 digits, or ``""`` when there are none.
    """
    digits = "".join(c for c in (value or "") if c.isdigit())
    return digits[-10:] if len(digits) > 10 else digits


def _mask_identity(value: str) -> str:
    """Redact a session/identity string for logs, keeping only its shape.

    Args:
        value (str): the identity string to redact.

    Returns:
        str: the leading channel prefix (if any) plus the last 4 characters,
        with the middle elided — enough to diagnose a shape mismatch, not
        enough to expose a full address.
    """
    if not value:
        return ""
    prefix = f"{value.split(':', 1)[0]}:" if ":" in value else ""
    tail = value.split(":", 1)[-1]
    return f"{prefix}…{tail[-4:]}" if len(tail) > 4 else f"{prefix}…"


def _relay_authorized(caller: str, edge: Dict[str, Any]) -> bool:
    """Whether the current turn owns ``edge`` and may relay its answer.

    Only the child conversation that received the spawned message may relay it.
    The host's session-thread id and the value stamped on the edge at bind can
    carry the same identity in different shapes — a bare id vs a channel-prefixed
    one (``sms:<id>``), or a differently formatted phone number — so match on the
    core identity rather than requiring byte-for-byte equality.

    Args:
        caller (str): the current turn's session thread id.
        edge (Dict[str, Any]): the spawn edge being relayed.

    Returns:
        bool: True when the caller owns the edge.
    """
    child = str(edge.get("childSessionKey") or "")
    if not caller or not child:
        return False
    if caller == child:
        return True
    # Strip a leading channel prefix so "sms:<id>" and the bare "<id>" match.
    if caller.split(":", 1)[-1].strip().lower() == child.split(":", 1)[-1].strip().lower():
        return True
    # Phone-keyed sessions: the recipient the spawn was sent to is the one now
    # replying, so authorize when the caller resolves to the recipient's number.
    caller_digits = _identity_digits(caller)
    recipient_digits = _identity_digits(str(edge.get("recipientKey") or ""))
    if caller_digits and caller_digits == recipient_digits:
        return True
    return False


def _current_session_thread_id() -> str:
    """Return the current agent turn's session thread id, or ``""``.

    Reads the host-stamped per-turn env (falls back to ``os.environ`` for
    CLI/cron/tests). This is a *thread-within-chat* id — ``None`` for a flat DM —
    so it is used only for channel-modality hints, not caller identity.

    Returns:
        str: the ``HERMES_SESSION_THREAD_ID`` value, or ``""`` when unknown.
    """
    try:
        from gateway.session_context import get_session_env

        return (get_session_env("HERMES_SESSION_THREAD_ID", "") or "").strip()
    except Exception:
        return (os.environ.get("HERMES_SESSION_THREAD_ID", "") or "").strip()


def _current_session_chat_id() -> str:
    """Return the current turn's session chat id, or ``""``.

    The host stamps ``HERMES_SESSION_CHAT_ID`` = the source's ``chat_id`` — the
    same value the bind hook records as an edge's ``childSessionKey`` — so the
    relay tool authorizes a caller by matching it. (``HERMES_SESSION_THREAD_ID``
    is a per-thread id that is ``None`` for a flat DM, so it cannot serve here.)

    Returns:
        str: the ``HERMES_SESSION_CHAT_ID`` value, or ``""`` when unknown.
    """
    try:
        from gateway.session_context import get_session_env

        return (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    except Exception:
        return (os.environ.get("HERMES_SESSION_CHAT_ID", "") or "").strip()


def _all_edges() -> List[Dict[str, Any]]:
    """Return every persisted edge, tolerantly (empty if the ledger is absent).

    Returns:
        List[Dict[str, Any]]: all readable edge records.
    """
    try:
        edges_dir = lineage._edges_dir()
        if not edges_dir.exists():
            return []
        out: List[Dict[str, Any]] = []
        for path in edges_dir.glob("*.json"):
            edge = lineage._read_edge(path.stem)
            if edge is not None:
                out.append(edge)
        return out
    except Exception:
        return []


def _edges_for_parent(parent_session_key: str) -> List[Dict[str, Any]]:
    """Return all edges spawned by one originating (parent) session.

    Args:
        parent_session_key (str): the parent's session thread id.

    Returns:
        List[Dict[str, Any]]: matching edges (empty when the key is falsy).
    """
    if not parent_session_key:
        return []
    return [
        e for e in _all_edges()
        if str(e.get("parentSessionKey") or "") == str(parent_session_key)
    ]


def _spinoff_precheck(recipient: Dict[str, Any]) -> Optional[str]:
    """Refuse a spin-off that would loop or exceed the fan-out cap.

    Runs BEFORE the outbound send so a rejected spin-off sends nothing. Fails
    open (returns ``None``) if the ledger can't be consulted, so a storage hiccup
    never blocks a legitimate send.

    Args:
        recipient (Dict[str, Any]): the child destination descriptor.

    Returns:
        Optional[str]: an error message to surface, or ``None`` to allow.
    """
    try:
        route = _current_turn_route() or {}
        psk = str(route.get("sessionThreadId") or "")
        if not psk:
            return None  # no turn context to attribute against — allow
        # Single-hop cap: a conversation that is itself an open spun-off child
        # may not start further spin-offs (it should relay its answer instead).
        child_edges = lineage.index_edges("child", psk)
        if any(e.get("status") in _SPINOFF_OPEN_STATUSES for e in child_edges):
            return (
                "This conversation is itself a delegated sub-conversation; relay "
                "your answer with inkbox_relay_answer instead of starting another "
                "spin-off."
            )
        # Fan-out cap: bound how many spin-offs one conversation can have open.
        open_count = sum(
            1 for e in _edges_for_parent(psk)
            if e.get("status") in _SPINOFF_OPEN_STATUSES
        )
        if open_count >= SPINOFF_FANOUT_CAP:
            return (
                f"Too many open spin-offs ({open_count}); wait for some to resolve "
                "before starting another."
            )
        return None
    except Exception:
        return None  # never block a send on a lineage bookkeeping error


def _spinoff_facts(disclose: Any, route: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Transform the ``disclose[]`` allowlist into ``brief.facts`` records.

    Each string becomes ``{label, value, owner}``; a ``"label: value"`` prefix
    supplies the label, otherwise the first few words are slugged into one. The
    owner is the spawning side's principal so the return path can tell whose data
    a fact is.

    Args:
        disclose (Any): the raw ``disclose`` list from the spinoff object.
        route (Dict[str, Any]): the parent turn route (for the owner id).

    Returns:
        List[Dict[str, Any]]: the fact records.
    """
    owner = (route or {}).get("contactId") or "originator"
    facts: List[Dict[str, Any]] = []
    for item in disclose or []:
        value = str(item).strip()
        if not value:
            continue
        if ":" in value:
            label = value.partition(":")[0].strip()  # explicit "label: value"
        else:
            label = " ".join(value.split()[:4])  # short auto-slug
        facts.append({"label": label or value, "value": value, "owner": owner})
    return facts


def _spinoff_group_id(spinoff: Dict[str, Any]) -> Optional[str]:
    """Extract a fan-out group id from ``waitFor`` (``"group:<id>"``), if any.

    Args:
        spinoff (Dict[str, Any]): the spinoff object.

    Returns:
        Optional[str]: the group id, or ``None`` for an ungrouped spin-off.
    """
    wait_for = str(spinoff.get("waitFor") or spinoff.get("wait_for") or "").strip()
    if wait_for.startswith("group:"):
        return wait_for.split(":", 1)[1].strip() or None
    return None


def _next_call_index(parent_session_key: str, recipient_key_value: str, origin_turn_id: str) -> int:
    """Within-turn spawn sequence number for this parent/recipient/turn.

    Counting existing edges gives distinct ``spawnKey``s to two separate asks to
    the same recipient in one turn, while an identical redelivered send reuses the
    same index (and therefore dedups) via ``create_edge_cas``.

    Args:
        parent_session_key (str): the spawning session's key.
        recipient_key_value (str): the normalized recipient key.
        origin_turn_id (str): the triggering turn id.

    Returns:
        int: the next call index (0-based).
    """
    count = 0
    for e in lineage.index_edges("recipient", recipient_key_value):
        if str(e.get("parentSessionKey") or "") == str(parent_session_key) and str(
            e.get("originTurnId") or ""
        ) == str(origin_turn_id):
            count += 1
    return count


def _register_spinoff_edge(
    spinoff: Dict[str, Any],
    recipient: Dict[str, Any],
    outbound_message_id: Any,
    status: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Create (idempotently) the durable spawn edge for a spin-off send.

    Called AFTER the outbound send returns (except voice, which needs the edge id
    before dialing). Every failure is returned as an error string rather than
    raised, so a spin-off bookkeeping problem never masks a send that already went
    out.

    Args:
        spinoff (Dict[str, Any]): the spinoff object from the tool args.
        recipient (Dict[str, Any]): the child destination descriptor
            (``channel`` plus ``address``/``conversationId``).
        outbound_message_id (Any): the id of the message just sent (bind key).
        status (Optional[str]): the edge's starting status; defaults to
            ``delivered`` (voice passes ``spawning`` and promotes after dialing).

    Returns:
        Tuple[Optional[Dict[str, Any]], Optional[str]]: ``(edge, None)`` on
        success, or ``(None, error)`` if the edge could not be recorded.
    """
    if status is None:
        status = lineage.STATUS_DELIVERED
    try:
        route = _current_turn_route() or {}
        psk = str(route.get("sessionThreadId") or "")
        rkey = lineage.recipient_key(
            recipient["channel"],
            recipient.get("address") or recipient.get("conversationId") or "",
        )
        origin_turn_id = str(route.get("messageId") or "")
        call_index = _next_call_index(psk, rkey, origin_turn_id)
        skey = lineage.spawn_key(psk, rkey, origin_turn_id, call_index)
        now = time.time()

        def _builder() -> Dict[str, Any]:
            # Root single-hop spawn at MVP: no parent edge is threaded through.
            edge = lineage.derive_edge(None, psk, recipient, call_index)
            edge["brief"] = {
                "intent": str(spinoff.get("purpose") or "").strip(),
                "endState": str(spinoff.get("success") or "").strip(),
                "constraints": [
                    str(c).strip() for c in (spinoff.get("constraints") or []) if str(c).strip()
                ],
                "facts": _spinoff_facts(spinoff.get("disclose"), route),
                "disclose_identity": bool(spinoff.get("disclose_identity")),
            }
            # Explicit parent routing so the relay rebuilds A's source without
            # reverse-parsing a session key.
            edge["parentRoute"] = {
                "chatId": route.get("chatId"),
                "threadId": route.get("threadId"),
                "modality": route.get("modality"),
            }
            edge["parentContact"] = {"contactId": route.get("contactId"), "channels": []}
            edge["parentReplyTarget"] = {
                "to": route.get("replyTo"),
                "modality": route.get("modality"),
                "threadRef": route.get("threadId"),
            }
            edge["originTurnId"] = origin_turn_id or None
            edge["spawnKey"] = skey
            edge["groupId"] = _spinoff_group_id(spinoff)
            edge["status"] = status
            edge["recipientBinding"]["outboundMessageId"] = (
                str(outbound_message_id) if outbound_message_id else None
            )
            edge["recipientBinding"]["bindWindowUntil"] = now + lineage.AWAITING_REPLY_TIMEOUT_S
            edge["updatedAt"] = now
            return edge

        edge = lineage.create_edge_cas(skey, _builder)
        return edge, None
    except Exception as exc:  # noqa: BLE001 — surface, but never mask the send
        return None, f"spin-off edge not recorded: {exc}"


def _mask_address(address: Any) -> str:
    """Partially mask a recipient address for a metadata-only status readout.

    Args:
        address (Any): the raw address or conversation id.

    Returns:
        str: a masked form, e.g. ``a…@x.com`` or ``+1…4231``.
    """
    text = str(address or "").strip()
    if not text:
        return "(unknown)"
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{(local[0] if local else '')}…@{domain}"
    if len(text) <= 4:
        return f"…{text}"
    return f"{text[:2]}…{text[-4:]}"


def _edge_summary(edge: Dict[str, Any]) -> Dict[str, Any]:
    """Render one edge into a masked, metadata-only summary row.

    Args:
        edge (Dict[str, Any]): the edge record.

    Returns:
        Dict[str, Any]: a summary safe to show to either side (no recipient
        content, address masked).
    """
    binding = edge.get("recipientBinding") or {}
    created = edge.get("createdAt") or 0
    return {
        "edge_id": edge.get("edgeId"),
        "recipient": _mask_address(binding.get("address") or binding.get("conversationId")),
        "channel": edge.get("channelChild"),
        "intent": (edge.get("brief") or {}).get("intent"),
        "status": edge.get("status"),
        "age_seconds": int(max(0.0, time.time() - created)) if created else None,
    }


def _active_adapter():
    """Return the in-process adapter instance for the relay fast path, or ``None``.

    Reads the adapter module's global registry live (guarded) so a non-gateway
    context — CLI, tests, out-of-process tool runs — simply degrades to the
    durable relay drain instead of the in-process wake.

    Returns:
        Any: the active adapter, or ``None`` when unavailable.
    """
    try:
        from . import adapter as adapter_mod
    except ImportError:
        try:
            import adapter as adapter_mod  # type: ignore
        except Exception:
            return None
    except Exception:
        return None
    return getattr(adapter_mod, "_ACTIVE_ADAPTER", None)


def _relay_fields(raw: Any) -> List[Dict[str, str]]:
    """Normalize the optional structured ``fields`` on a relayed answer.

    Args:
        raw (Any): the raw fields list from the tool args.

    Returns:
        List[Dict[str, str]]: cleaned ``{label, value}`` records.
    """
    fields: List[Dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
            if value:
                label = str(item.get("label") or "").strip()
                fields.append({"label": label or value, "value": value})
    return fields


def _relay_attribution(edge: Dict[str, Any]) -> str:
    """Build the attribution line naming (masked) who the answer came from.

    Args:
        edge (Dict[str, Any]): the edge being relayed.

    Returns:
        str: a short attribution phrase.
    """
    binding = edge.get("recipientBinding") or {}
    who = _mask_address(binding.get("address") or binding.get("conversationId"))
    return f"Relayed answer from your spin-off to {who}"


def inkbox_whoami(args: dict, **kwargs) -> str:
    del args, kwargs
    try:
        cfg, client, identity = _client_and_identity()
        # Present the two lines with explicit labels so the agent describes
        # them correctly: its OWN dedicated phone line vs the SHARED iMessage
        # line. The dedicated number is the one for SMS + voice; the iMessage
        # line's number is managed by Inkbox and never surfaced.
        phone = getattr(identity, "phone_number", None)
        dedicated_number = getattr(phone, "number", None) if phone else None
        imessage_enabled = bool(getattr(identity, "imessage_enabled", False))
        lines = {
            "dedicated_phone_line": dedicated_number or "(none provisioned)",
            "dedicated_phone_line_note": (
                "Your own phone line for SMS and voice calls. Call from it with "
                "origination=dedicated_number."
            ),
            "shared_imessage_line": "enabled" if imessage_enabled else "disabled",
            "shared_imessage_line_note": (
                "Voice + iMessage with people connected to you over iMessage. Its "
                "number is managed by Inkbox and not shown. Call over it with "
                "origination=shared_imessage_number."
            ),
        }
        return _json({
            "ok": True,
            "base_url": cfg.base_url,
            "whoami": object_summary(client.whoami()),
            "identity": object_summary(identity),
            "lines": lines,
            "call_websocket_url": public_call_ws_url(cfg, identity),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def _contact_arg(args: dict, snake_name: str, camel_name: Optional[str] = None) -> Optional[str]:
    value = args.get(snake_name)
    if value is None and camel_name:
        value = args.get(camel_name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _contact_raw_arg(args: dict, snake_name: str, camel_name: Optional[str] = None) -> tuple[bool, Any]:
    if snake_name in args:
        return True, args.get(snake_name)
    if camel_name and camel_name in args:
        return True, args.get(camel_name)
    return False, None


def _contact_write_fields(args: dict) -> Dict[str, str]:
    fields = (
        ("preferred_name", "preferredName"),
        ("given_name", "givenName"),
        ("family_name", "familyName"),
        ("company_name", "companyName"),
        ("job_title", "jobTitle"),
        ("notes", "notes"),
    )
    payload: Dict[str, str] = {}
    for snake_name, camel_name in fields:
        value = _contact_arg(args, snake_name, camel_name)
        if value is not None:
            payload[snake_name] = value
    return payload


def _contact_entries(raw: Any, kind: str) -> list[Any]:
    from inkbox import ContactEmail, ContactPhone

    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(f"`{kind}` must be a list of strings or objects")

    cls = ContactEmail if kind == "emails" else ContactPhone
    value_key = "email" if kind == "emails" else "phone"
    entries = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            value = item.strip()
            label = None
            is_primary = index == 0
        elif isinstance(item, dict):
            value = str(item.get("value") or item.get(value_key) or "").strip()
            label_raw = item.get("label")
            label = str(label_raw).strip() if label_raw is not None else None
            if "isPrimary" in item:
                is_primary = bool(item.get("isPrimary"))
            elif "is_primary" in item:
                is_primary = bool(item.get("is_primary"))
            else:
                is_primary = index == 0
        else:
            raise ValueError(f"`{kind}` entries must be strings or objects")
        if value:
            entries.append(cls(label=label or None, value=value, is_primary=is_primary))
    return entries


def _contact_payload(args: dict, *, require_any: bool) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(_contact_write_fields(args))
    for key in ("emails", "phones"):
        provided, raw = _contact_raw_arg(args, key)
        if provided:
            payload[key] = _contact_entries(raw, key)
    if require_any and not payload:
        raise ValueError("Provide at least one contact field to write.")
    return payload


def inkbox_lookup_contact(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, client, _identity = _client_and_identity()
        fields = (
            ("email", "email"),
            ("phone", "phone"),
            ("email_domain", "emailDomain"),
            ("email_contains", "emailContains"),
            ("phone_contains", "phoneContains"),
        )
        supplied = {
            snake_name: value
            for snake_name, camel_name in fields
            if (value := _contact_arg(args, snake_name, camel_name))
        }
        if len(supplied) != 1:
            return _json({"error": "Specify exactly one of email, phone, emailDomain, emailContains, or phoneContains."})
        contacts = client.contacts.lookup(**supplied)
        return _json({
            "ok": True,
            "query": supplied,
            "count": len(contacts or []),
            "contacts": _json_safe(contacts or []),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_contacts(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, client, _identity = _client_and_identity()
        contacts = client.contacts.list(
            q=_contact_arg(args, "q"),
            order=_contact_arg(args, "order"),
            limit=int(args.get("limit") or 25),
            offset=int(args.get("offset") or 0),
        )
        return _json({
            "ok": True,
            "count": len(contacts or []),
            "contacts": _json_safe(contacts or []),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_get_contact(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, client, _identity = _client_and_identity()
        contact_id = _contact_arg(args, "contact_id", "contactId")
        if not contact_id:
            return _json({"error": "`contactId` is required"})
        contact = client.contacts.get(contact_id)
        return _json({"ok": True, "contact": _json_safe(contact)})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_create_contact(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, client, _identity = _client_and_identity()
        payload = _contact_payload(args, require_any=True)
        contact = client.contacts.create(**payload)
        return _json({"ok": True, "contact": _json_safe(contact)})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_update_contact(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, client, _identity = _client_and_identity()
        contact_id = _contact_arg(args, "contact_id", "contactId")
        if not contact_id:
            return _json({"error": "`contactId` is required"})
        payload = _contact_payload(args, require_any=True)
        contact = client.contacts.update(contact_id, **payload)
        return _json({"ok": True, "contact": _json_safe(contact)})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_delete_contact(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, client, _identity = _client_and_identity()
        contact_id = _contact_arg(args, "contact_id", "contactId")
        if not contact_id:
            return _json({"error": "`contactId` is required"})
        client.contacts.delete(contact_id)
        return _json({"ok": True, "deleted_contact_id": contact_id})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_send_email(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        to = args.get("to") or []
        if isinstance(to, str):
            to = [to]
        to = [str(x).strip() for x in to if str(x).strip()]
        if not to:
            return _json({"error": "`to` must contain at least one email address"})
        subject = str(args.get("subject") or "(no subject)")
        body_text = str(args.get("body_text") or args.get("bodyText") or "")
        body_html = args.get("body_html") or args.get("bodyHtml")
        in_reply_to = args.get("in_reply_to_message_id") or args.get("inReplyToMessageId")

        def _send():
            return identity.send_email(
                to=to,
                subject=subject,
                body_text=body_text or None,
                body_html=body_html or None,
                cc=args.get("cc") or None,
                bcc=args.get("bcc") or None,
                in_reply_to_message_id=in_reply_to or None,
            )

        # Spin-off: refuse a looping/over-cap spawn BEFORE anything is sent.
        spinoff = args.get("spinoff")
        recipient = None
        if spinoff:
            recipient = {"channel": "email", "address": to[0]}
            precheck_error = _spinoff_precheck(recipient)
            if precheck_error:
                return _json({"error": precheck_error, "error_code": "spinoff_refused"})

        msg = _send()
        result: Dict[str, Any] = {
            "ok": True,
            "message_id": str(getattr(msg, "id", "")),
            "to": to,
            "subject": subject,
        }
        # Record the durable edge AFTER a successful send; a bookkeeping failure
        # is surfaced distinctly and never masks the send.
        if spinoff:
            edge, edge_error = _register_spinoff_edge(spinoff, recipient, getattr(msg, "id", None))
            if edge_error:
                result["spinoff_warning"] = edge_error
            elif edge:
                result["spinoff_edge_id"] = edge.get("edgeId")
        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_send_sms(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        text = str(args.get("text") or "")
        if not text:
            return _json({"error": "`text` is required"})
        if len(text) > SMS_MAX_LENGTH:
            return _json(_message_too_long_payload("SMS", text, SMS_MAX_LENGTH))

        conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
        to_list = _normalize_recipients(args.get("to"))
        has_to = to_list is not None and len(to_list) > 0
        has_conversation = bool(conversation_id)
        if has_to == has_conversation:
            return _json({"error": "Specify exactly one of `to` or `conversationId`."})
        if to_list is not None and len(to_list) == 0:
            return _json({"error": "`to` must include at least one recipient."})
        if to_list and len(to_list) > 8:
            return _json({"error": "Inkbox group texts support at most 8 recipients."})

        payload: dict[str, Any] = {"text": text}
        camel_payload: dict[str, Any] = {"text": text}
        if conversation_id:
            payload["conversation_id"] = str(conversation_id).strip()
            camel_payload["conversationId"] = str(conversation_id).strip()
        else:
            payload["to"] = to_list[0] if to_list and len(to_list) == 1 else to_list
            camel_payload["to"] = payload["to"]
        media_urls = args.get("mediaUrls") or args.get("media_urls")
        if media_urls:
            payload["media_urls"] = media_urls
            camel_payload["mediaUrls"] = media_urls

        # Spin-off: refuse a looping/over-cap spawn BEFORE anything is sent.
        spinoff = args.get("spinoff")
        recipient = None
        if spinoff:
            addr = None if conversation_id else (to_list[0] if to_list else None)
            recipient = {"channel": "sms", "address": addr, "conversationId": conversation_id or None}
            precheck_error = _spinoff_precheck(recipient)
            if precheck_error:
                return _json({"error": precheck_error, "error_code": "spinoff_refused"})

        msg = _call_with_kwargs_or_payload(
            _identity_method(identity, "send_text", "sendText"),
            payload,
            camel_payload,
        )
        result: Dict[str, Any] = {
            "ok": True,
            "message_id": str(getattr(msg, "id", "")),
            "conversation_id": conversation_id or object_summary(
                getattr(msg, "conversation_id", None) or getattr(msg, "conversationId", None)
            ),
            "to": None if conversation_id else payload.get("to"),
            "status": object_summary(getattr(msg, "delivery_status", None) or getattr(msg, "status", None)),
        }
        # Record the durable edge AFTER a successful send (never masks it).
        if spinoff:
            edge, edge_error = _register_spinoff_edge(spinoff, recipient, getattr(msg, "id", None))
            if edge_error:
                result["spinoff_warning"] = edge_error
            elif edge:
                result["spinoff_edge_id"] = edge.get("edgeId")
        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_text_conversations(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        payload = {
            "limit": int(args.get("limit") or 25),
            "offset": int(args.get("offset") or 0),
            "include_groups": args.get("includeGroups") if "includeGroups" in args else args.get("include_groups", True),
        }
        camel_payload = {
            "limit": payload["limit"],
            "offset": payload["offset"],
            "includeGroups": payload["include_groups"],
        }
        convos = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_text_conversations", "listTextConversations"),
            payload,
            camel_payload,
        )
        return _json({"ok": True, "count": len(convos or []), "conversations": object_summary(convos or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_get_text_conversation(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        key, label, error = _text_conversation_key(args)
        if error:
            return _json({"error": error})
        options = {"limit": int(args.get("limit") or 50), "offset": int(args.get("offset") or 0)}
        msgs = _call_with_key_and_options(
            _identity_method(identity, "get_text_conversation", "getTextConversation"),
            key,
            options,
            options,
        )
        return _json({"ok": True, "conversation": label, "count": len(msgs or []), "texts": object_summary(msgs or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_texts(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        payload = {"limit": int(args.get("limit") or 25), "offset": int(args.get("offset") or 0)}
        if "isRead" in args or "is_read" in args:
            payload["is_read"] = args.get("isRead") if "isRead" in args else args.get("is_read")
        camel_payload = dict(payload)
        if "is_read" in camel_payload:
            camel_payload["isRead"] = camel_payload.pop("is_read")
        texts = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_texts", "listTexts"),
            payload,
            camel_payload,
        )
        return _json({"ok": True, "count": len(texts or []), "texts": object_summary(texts or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_get_text(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        text_id = str(args.get("textId") or args.get("text_id") or "").strip()
        if not text_id:
            return _json({"error": "`textId` is required"})
        text = _identity_method(identity, "get_text", "getText")(text_id)
        return _json({"ok": True, "text": object_summary(text)})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_mark_text_read(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        text_id = str(args.get("textId") or args.get("text_id") or "").strip()
        if not text_id:
            return _json({"error": "`textId` is required"})
        _identity_method(identity, "mark_text_read", "markTextRead")(text_id)
        return _json({"ok": True, "text_id": text_id, "status": "marked_read"})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_mark_text_conversation_read(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        key, label, error = _text_conversation_key(args)
        if error:
            return _json({"error": error})
        result = _identity_method(
            identity,
            "mark_text_conversation_read",
            "markTextConversationRead",
        )(key)
        updated = (
            getattr(result, "updated_count", None)
            or getattr(result, "updatedCount", None)
        )
        return _json({
            "ok": True,
            "conversation": label,
            "updated_count": object_summary(updated),
            "result": object_summary(result),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_imessage_triage_number(args: dict, **kwargs) -> str:
    del args, kwargs
    try:
        _cfg, client, _identity = _client_and_identity()
        imessages = getattr(client, "imessages", None)
        if imessages is None:
            return _json({"error": "Installed Inkbox SDK has no iMessage support; upgrade with: pip install -U inkbox"})
        triage = imessages.get_triage_number()
        return _json({
            "ok": True,
            "number": str(getattr(triage, "number", "")),
            "connect_command": str(getattr(triage, "connect_command", "")),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_send_imessage(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        text = str(args.get("text") or "")
        media_urls = _normalize_recipients(args.get("mediaUrls") or args.get("media_urls"))
        if not text and not media_urls:
            return _json({"error": "Provide `text`, `mediaUrls`, or both."})
        if len(text) > IMESSAGE_MAX_LENGTH:
            return _json(_message_too_long_payload("iMessage", text, IMESSAGE_MAX_LENGTH))
        if media_urls and len(media_urls) > 1:
            return _json({"error": "Inkbox iMessage supports at most one media URL per message."})

        conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
        to = str(args.get("to") or "").strip()
        if bool(conversation_id) == bool(to):
            return _json({"error": "Specify exactly one of `to` or `conversationId`."})

        payload: dict[str, Any] = {"text": text or None}
        camel_payload: dict[str, Any] = {"text": text or None}
        if conversation_id:
            payload["conversation_id"] = conversation_id
            camel_payload["conversationId"] = conversation_id
        else:
            payload["to"] = to
            camel_payload["to"] = to
        if media_urls:
            payload["media_urls"] = media_urls
            camel_payload["mediaUrls"] = media_urls
        send_style = str(args.get("sendStyle") or args.get("send_style") or "").strip()
        if send_style:
            payload["send_style"] = send_style
            camel_payload["sendStyle"] = send_style

        # Spin-off: refuse a looping/over-cap spawn BEFORE anything is sent.
        spinoff = args.get("spinoff")
        recipient = None
        if spinoff:
            recipient = {"channel": "imessage", "address": to or None, "conversationId": conversation_id or None}
            precheck_error = _spinoff_precheck(recipient)
            if precheck_error:
                return _json({"error": precheck_error, "error_code": "spinoff_refused"})

        msg = _call_with_kwargs_or_payload(
            _identity_method(identity, "send_imessage", "sendImessage"),
            payload,
            camel_payload,
        )
        resolved_conv = _json_safe(
            getattr(msg, "conversation_id", None) or getattr(msg, "conversationId", None)
        )
        result: Dict[str, Any] = {
            "ok": True,
            "message_id": str(getattr(msg, "id", "")),
            "conversation_id": resolved_conv,
            "service": _json_safe(getattr(msg, "service", None)),
            "status": _json_safe(getattr(msg, "status", None)),
        }
        # Record the durable edge AFTER a successful send (never masks it).
        # iMessage binds by conversation id, so prefer the one the send resolved.
        if spinoff:
            if resolved_conv and not recipient.get("conversationId"):
                recipient["conversationId"] = str(resolved_conv)
            edge, edge_error = _register_spinoff_edge(spinoff, recipient, getattr(msg, "id", None))
            if edge_error:
                result["spinoff_warning"] = edge_error
            elif edge:
                result["spinoff_edge_id"] = edge.get("edgeId")
        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_imessage_conversations(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        options = {"limit": int(args.get("limit") or 25), "offset": int(args.get("offset") or 0)}
        convos = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_imessage_conversations", "listImessageConversations"),
            options,
        )
        return _json({"ok": True, "count": len(convos or []), "conversations": _json_safe(convos or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_list_imessage_assignments(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        options = {"limit": int(args.get("limit") or 25), "offset": int(args.get("offset") or 0)}
        assignments = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_imessage_assignments", "listImessageAssignments"),
            options,
        )
        return _json({"ok": True, "count": len(assignments or []), "assignments": _json_safe(assignments or [])})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_get_imessage_conversation(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
        if not conversation_id:
            return _json({"error": "`conversationId` is required"})
        payload = {
            "conversation_id": conversation_id,
            "limit": int(args.get("limit") or 50),
            "offset": int(args.get("offset") or 0),
        }
        camel_payload = {
            "conversationId": conversation_id,
            "limit": payload["limit"],
            "offset": payload["offset"],
        }
        msgs = _call_with_kwargs_or_payload(
            _identity_method(identity, "list_imessages", "listImessages"),
            payload,
            camel_payload,
        )
        return _json({
            "ok": True,
            "conversation_id": conversation_id,
            "count": len(msgs or []),
            "messages": _json_safe(msgs or []),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_send_imessage_reaction(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        message_id = str(args.get("messageId") or args.get("message_id") or "").strip()
        reaction = str(args.get("reaction") or "").strip().lower()
        if not message_id:
            return _json({"error": "`messageId` is required"})
        if not reaction:
            return _json({"error": "`reaction` is required"})
        payload = {
            "message_id": message_id,
            "reaction": reaction,
            "part_index": int(args.get("partIndex") or args.get("part_index") or 0),
        }
        camel_payload = {
            "messageId": message_id,
            "reaction": reaction,
            "partIndex": payload["part_index"],
        }
        result = _call_with_kwargs_or_payload(
            _identity_method(identity, "send_imessage_reaction", "sendImessageReaction"),
            payload,
            camel_payload,
        )
        return _json({"ok": True, "reaction": _json_safe(result)})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_mark_imessage_conversation_read(args: dict, **kwargs) -> str:
    del kwargs
    try:
        _cfg, _client, identity = _client_and_identity()
        conversation_id = str(args.get("conversationId") or args.get("conversation_id") or "").strip()
        if not conversation_id:
            return _json({"error": "`conversationId` is required"})
        result = _identity_method(
            identity,
            "mark_imessage_conversation_read",
            "markImessageConversationRead",
        )(conversation_id)
        updated = (
            getattr(result, "updated_count", None)
            or getattr(result, "updatedCount", None)
        )
        return _json({
            "ok": True,
            "conversation_id": conversation_id,
            "updated_count": _json_safe(updated),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def _current_channel_hint() -> str | None:
    """Which Inkbox channel is the current agent turn happening on?

    The gateway stamps each inbound turn with a session thread-id; iMessage
    turns are ``imessage:<cid>`` and SMS/phone turns are ``sms:``/``text:``/
    ``phone:<cid>``.  We read that (concurrency-safe, per-turn) so an outbound
    call can follow the conversation's channel without the agent having to say
    so.  Returns ``"imessage"`` | ``"dedicated"`` | ``None`` (unknown / not in
    a gateway turn, e.g. CLI or tests).
    """
    thread_id = ""
    try:
        # Host-provided per-turn context var (falls back to os.environ for
        # CLI/cron).  Imported lazily + guarded so the plugin still works
        # standalone (unit tests, non-gateway hosts).
        from gateway.session_context import get_session_env

        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or ""
    except Exception:
        thread_id = os.environ.get("HERMES_SESSION_THREAD_ID", "") or ""
    t = thread_id.strip().lower()
    if t.startswith("imessage:"):
        return "imessage"
    if t.startswith(("sms:", "text:", "phone:")):
        return "dedicated"
    return None


def _current_turn_route() -> Optional[Dict[str, Any]]:
    """Best-effort parent route for the current agent turn.

    The routing-critical ids (chat id / thread / message) come from the
    host-stamped per-turn session env, which reliably reaches tool execution —
    unlike the plugin contextvar, which can be lost across the thread/process
    boundary where tools run, leaving the relay with no address to send to. The
    contextvar, when present, only supplies extra hints (modality / reply
    target). Returns ``None`` for a CLI/test invocation with no session at all,
    so the spin-off edge is still created with an empty route and the relay
    simply degrades.

    Returns:
        Optional[Dict[str, Any]]: the parent route descriptor, or ``None``.
    """
    ctx = turn_context.get_current_turn() or {}

    # Host session env — guarded + lazy, mirroring _current_channel_hint.
    try:
        from gateway.session_context import get_session_env as _gse

        def _env(key: str) -> str:
            return (_gse(key, "") or "").strip()
    except Exception:
        def _env(key: str) -> str:
            return (os.environ.get(key, "") or "").strip()

    # HERMES_SESSION_CHAT_ID is the contact/chat the relay must route back to and
    # the value send() resolves an address from; it is the reliable anchor here.
    chat_id = _env("HERMES_SESSION_CHAT_ID")
    thread_id = _env("HERMES_SESSION_THREAD_ID")
    message_id = _env("HERMES_SESSION_MESSAGE_ID") or _env("HERMES_ORIGIN_TURN_ID")

    route = {
        "sessionThreadId": ctx.get("sessionThreadId") or _env("HERMES_SESSION_KEY") or thread_id or None,
        "chatId": ctx.get("chatId") or chat_id or None,
        "contactId": ctx.get("contactId") or chat_id or None,
        "threadId": ctx.get("threadId") or thread_id or None,
        "modality": ctx.get("modality") or None,
        "messageId": ctx.get("messageId") or message_id or None,
        "replyTo": ctx.get("replyTo") or None,
    }
    if not any(route.values()):
        return None
    return route


def _resolve_call_origination(identity, explicit: str) -> str | None:
    """Pick which line an outbound call originates from.

    Calls can go out over two paths: the agent's own ``dedicated_number`` or
    the ``shared_imessage_number`` it's already messaging the recipient on.
    Resolution order:

    1. An explicit choice (from the agent) always wins.
    2. If only one path exists, use it (dedicated number but no iMessage →
       dedicated; iMessage enabled but no number → shared).
    3. If BOTH exist, follow the channel the current conversation is on — an
       iMessage turn calls over the shared iMessage line, an SMS/phone turn
       over the dedicated number.  This makes "call me" do the right thing
       without the agent having to specify the line.
    4. If both exist but we can't tell the channel, default to the dedicated
       number (the open line that can reach anyone).

    Returns ``None`` when neither path exists (nothing to call from).
    """
    explicit = (explicit or "").strip().lower()
    if explicit in {"dedicated_number", "shared_imessage_number"}:
        return explicit
    has_number = getattr(identity, "phone_number", None) is not None
    imessage_enabled = bool(getattr(identity, "imessage_enabled", False))
    if has_number and imessage_enabled:
        # Both lines available — follow the conversation's channel.
        return "shared_imessage_number" if _current_channel_hint() == "imessage" else "dedicated_number"
    if has_number:
        return "dedicated_number"
    if imessage_enabled:
        return "shared_imessage_number"
    return None


def inkbox_place_call(args: dict, **kwargs) -> str:
    del kwargs
    try:
        cfg, _client, identity = _client_and_identity()
        to_number = str(args.get("to_number") or args.get("toNumber") or "").strip()
        purpose = str(args.get("purpose") or "").strip()
        if not to_number:
            return _json({"error": "`to_number` is required"})
        if not purpose:
            return _json({"error": "`purpose` is required so the live call starts with the right context"})

        # Resolve the outbound line (dedicated number vs shared iMessage line).
        origination = _resolve_call_origination(
            identity, args.get("origination") or args.get("origination_type") or "",
        )
        if origination is None:
            return _json({"error": "This identity can't place calls: it has no dedicated phone number and iMessage is not enabled. Provision a number or enable iMessage first."})

        ws_url = str(args.get("client_websocket_url") or args.get("clientWebsocketUrl") or "").strip()
        if not ws_url:
            ws_url = public_call_ws_url(cfg, identity)
        if not ws_url:
            return _json({"error": "No call WebSocket URL available. Run `hermes inkbox setup` and start the gateway, or pass client_websocket_url."})

        # Spin-off: refuse a looping/over-cap spawn BEFORE dialing, then register
        # the edge first so its id can ride the voice seed capsule. Voice starts
        # in ``spawning`` and is promoted once the call is actually placed.
        spinoff = args.get("spinoff")
        spinoff_edge = None
        spinoff_warning = None
        if spinoff:
            recipient = {"channel": "voice", "address": to_number}
            precheck_error = _spinoff_precheck(recipient)
            if precheck_error:
                return _json({"error": precheck_error, "error_code": "spinoff_refused"})
            spinoff_edge, spinoff_warning = _register_spinoff_edge(
                spinoff, recipient, None, status=lineage.STATUS_SPAWNING
            )

        token = _write_outbound_call_context({
            "to_number": to_number,
            "purpose": purpose,
            "opening_message": args.get("opening_message") or args.get("openingMessage") or "",
            "context": args.get("context") or "",
            # Only present for a spin-off; a plain call writes an unchanged capsule.
            "edge_id": spinoff_edge.get("edgeId") if spinoff_edge else None,
        })
        decorated_ws_url = _append_query_param(ws_url, "context_token", token)

        def _place():
            if not hasattr(identity, "place_call"):
                raise RuntimeError("Inkbox SDK identity has no place_call method (upgrade inkbox to >=0.4.15)")
            try:
                return identity.place_call(
                    to_number=to_number,
                    origination=origination,
                    client_websocket_url=decorated_ws_url,
                )
            except TypeError:
                # Older SDK without ``origination`` support → dedicated only.
                return identity.place_call(
                    to_number=to_number,
                    client_websocket_url=decorated_ws_url,
                )

        try:
            call = _place()
        except Exception as exc:  # noqa: BLE001 — surface a legible reason to the agent
            # The call never went out — retire the spin-off edge so it doesn't
            # linger as an unbindable spawn.
            if spinoff_edge:
                lineage.cas_status(spinoff_edge["edgeId"], lineage.STATUS_SPAWNING, lineage.STATUS_FAILED)
            msg = str(exc)
            if "no_shared_connection" in msg:
                return _json({
                    "error": "Can't place a shared iMessage-line call: this person isn't connected to you over iMessage yet. They need to message your iMessage number first. To call from your own phone number instead, set origination to \"dedicated_number\".",
                    "detail": msg,
                })
            return _json({"error": msg})

        # Call placed — promote the edge and bind it to the call id.
        if spinoff_edge:
            def _bind_call(e: Dict[str, Any]) -> None:
                e["recipientBinding"]["outboundMessageId"] = str(getattr(call, "id", "") or "")
                e["recipientBinding"]["bindWindowUntil"] = time.time() + lineage.AWAITING_REPLY_TIMEOUT_S

            lineage.cas_status(
                spinoff_edge["edgeId"], lineage.STATUS_SPAWNING, lineage.STATUS_DELIVERED, _bind_call
            )

        rate = object_summary(getattr(call, "rate_limit", None) or getattr(call, "rateLimit", None))
        result: Dict[str, Any] = {
            "ok": True,
            "call_id": str(getattr(call, "id", "")),
            "status": object_summary(getattr(call, "status", None)),
            "to_number": to_number,
            "origination": origination,
            "context_token": token,
            "rate_limit": rate,
        }
        if spinoff_edge:
            result["spinoff_edge_id"] = spinoff_edge.get("edgeId")
        elif spinoff_warning:
            result["spinoff_warning"] = spinoff_warning
        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_relay_answer(args: dict, **kwargs) -> str:
    del kwargs
    try:
        edge_id = str(args.get("edge_id") or args.get("edgeId") or "").strip()
        if not edge_id:
            return _json({"error": "`edge_id` is required"})
        summary = str(args.get("summary") or "").strip()
        satisfied = bool(args.get("satisfied"))

        edge = lineage._read_edge(edge_id)
        if edge is None:
            return _json({"error": f"No spin-off edge {edge_id}"})

        # Caller authorization: only the child conversation that owns this edge
        # (its childSessionKey == the session chat id, stamped at bind) may relay.
        caller = _current_session_chat_id()
        if not _relay_authorized(caller, edge):
            # Log the redacted identities so a bind/relay id-shape mismatch is
            # diagnosable from failure logs without leaking full addresses.
            logger.warning(
                "[Inkbox] relay auth rejected for edge %s: caller=%r child=%r",
                edge_id, _mask_identity(caller), _mask_identity(str(edge.get("childSessionKey") or "")),
            )
            return _json({"error": "Not authorized to relay this spin-off: it is owned by a different conversation."})

        # Not-yet-satisfied: leave the edge awaiting_reply so its brief keeps
        # riding future turns until a real answer arrives.
        if not satisfied:
            return _json({
                "ok": True,
                "relayed": False,
                "status": edge.get("status"),
                "note": "Marked not-yet-satisfied; the spin-off stays open for a real answer.",
            })
        if not summary:
            return _json({"error": "`summary` is required when satisfied is true"})
        # Distillation cap: reject an over-long summary rather than silently
        # truncating the recipient's answer.
        if len(summary) > RELAY_SUMMARY_MAX_CHARS:
            return _json({
                "error": f"summary is {len(summary)} characters; condense it under {RELAY_SUMMARY_MAX_CHARS} before relaying.",
                "error_code": "relay_summary_too_long",
            })

        fields = _relay_fields(args.get("fields"))
        attribution = _relay_attribution(edge)

        def _write_result(e: Dict[str, Any]) -> None:
            e["result"] = {
                "fields": fields,
                "summary": summary,
                "status": "answered",
                "attribution": attribution,
            }

        # Exactly-once claim: only one caller can move awaiting_reply→answered
        # (writing the distilled result); a concurrent/duplicate call finds the
        # edge already out of awaiting_reply and no-ops.
        claimed = lineage.cas_status(
            edge_id, lineage.STATUS_AWAITING_REPLY, lineage.STATUS_ANSWERED, _write_result
        )
        if not claimed:
            current = lineage._read_edge(edge_id) or {}
            return _json({
                "ok": False,
                "error": "This spin-off is not awaiting a reply (already relayed or closed).",
                "status": current.get("status"),
            })

        # In-process fast path: mark relayed then wake the parent conversation.
        # If no adapter is reachable (out-of-process tool run) leave the edge in
        # 'answered' for the adapter's durable relay drain to complete — the
        # answered→relayed CAS keeps either path from double-relaying.
        adapter = _active_adapter()
        relayed = False
        if adapter is not None and lineage.cas_status(
            edge_id, lineage.STATUS_ANSWERED, lineage.STATUS_RELAYED
        ):
            try:
                adapter.schedule_relay(edge_id)
                relayed = True
            except Exception as exc:  # noqa: BLE001 — answer is recorded regardless
                return _json({
                    "ok": True,
                    "relayed": False,
                    "edge_id": edge_id,
                    "status": lineage.STATUS_RELAYED,
                    "warning": f"answer recorded but relay could not be scheduled in-process: {exc}",
                })
        return _json({
            "ok": True,
            "relayed": relayed,
            "edge_id": edge_id,
            "status": lineage.STATUS_RELAYED if relayed else lineage.STATUS_ANSWERED,
            "attribution": attribution,
        })
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_spinoff_list(args: dict, **kwargs) -> str:
    del kwargs
    try:
        include_terminal = bool(args.get("includeTerminal") or args.get("include_terminal"))
        # This conversation is the parent side; list the edges it spawned.
        route = _current_turn_route() or {}
        psk = str(route.get("sessionThreadId") or "") or _current_session_thread_id()
        rows: List[Dict[str, Any]] = []
        for edge in _edges_for_parent(psk):
            if not include_terminal and edge.get("status") not in _SPINOFF_OPEN_STATUSES:
                continue
            rows.append(_edge_summary(edge))
        # Most-recent-open first for readability.
        rows.sort(key=lambda r: r.get("age_seconds") or 0)
        return _json({"ok": True, "count": len(rows), "spinoffs": rows})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_lineage_status(args: dict, **kwargs) -> str:
    del kwargs
    try:
        edge_id = str(args.get("edgeId") or args.get("edge_id") or "").strip()
        if edge_id and edge_id.lower() != "open":
            edge = lineage._read_edge(edge_id)
            if edge is None:
                return _json({"error": f"No spin-off edge {edge_id}"})
            detail = _edge_summary(edge)
            result = edge.get("result") or {}
            detail["success"] = (edge.get("brief") or {}).get("endState")
            detail["answer"] = result.get("summary")  # distilled, agent-authored
            return _json({"ok": True, "spinoff": detail})
        # No specific edge (or "open"): list this conversation's open spin-offs.
        route = _current_turn_route() or {}
        psk = str(route.get("sessionThreadId") or "") or _current_session_thread_id()
        rows = [
            _edge_summary(edge)
            for edge in _edges_for_parent(psk)
            if edge.get("status") in _SPINOFF_OPEN_STATUSES
        ]
        rows.sort(key=lambda r: r.get("age_seconds") or 0)
        return _json({"ok": True, "count": len(rows), "spinoffs": rows})
    except Exception as exc:
        return _json({"error": str(exc)})


def inkbox_spinoff_origin(args: dict, **kwargs) -> str:
    del args, kwargs
    try:
        # This conversation is the child side; list the open edges it owes an
        # answer on (via the by_child index).
        child = _current_session_thread_id()
        if not child:
            return _json({"ok": True, "count": 0, "owes": []})
        rows: List[Dict[str, Any]] = []
        for edge in lineage.index_edges("child", child):
            if edge.get("status") not in _SPINOFF_OPEN_STATUSES:
                continue
            brief = edge.get("brief") or {}
            rows.append({
                "edge_id": edge.get("edgeId"),
                "intent": brief.get("intent"),
                "success": brief.get("endState"),
                "status": edge.get("status"),
                "may_name_originator": bool(brief.get("disclose_identity")),
            })
        return _json({"ok": True, "count": len(rows), "owes": rows})
    except Exception as exc:
        return _json({"error": str(exc)})


WHOAMI_SCHEMA = {
    "name": "inkbox_whoami",
    "description": "Return the configured Inkbox identity, mailbox, phone number, auth scope, and call bridge URL.",
    "parameters": {"type": "object", "properties": {}},
}

LOOKUP_CONTACT_SCHEMA = {
    "name": "inkbox_lookup_contact",
    "description": "Reverse-lookup Inkbox contacts by exactly one email/phone filter. Returns contacts visible to this configured identity.",
    "parameters": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "Exact email address."},
            "phone": {"type": "string", "description": "Exact E.164 phone number."},
            "emailDomain": {"type": "string", "description": "Email domain, e.g. example.com."},
            "emailContains": {"type": "string", "description": "Substring match on email address."},
            "phoneContains": {"type": "string", "description": "Substring match on phone number."},
        },
    },
}

LIST_CONTACTS_SCHEMA = {
    "name": "inkbox_list_contacts",
    "description": "Search/list Inkbox contacts visible to this configured identity. Use for name-based queries like 'who is Alex?'.",
    "parameters": {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Optional free-text search across contact names, emails, phones, company, and notes."},
            "order": {"type": "string", "enum": ["recent", "name"], "description": "Sort order. Defaults to the Inkbox SDK default."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
    },
}

GET_CONTACT_SCHEMA = {
    "name": "inkbox_get_contact",
    "description": "Fetch a single Inkbox contact by contact UUID, including names, emails, phones, company, and notes.",
    "parameters": {
        "type": "object",
        "properties": {
            "contactId": {"type": "string", "description": "UUID of the Inkbox contact."},
        },
        "required": ["contactId"],
    },
}

_CONTACT_EMAIL_ENTRY_SCHEMA = {
    "oneOf": [
        {"type": "string", "description": "Email address. The first string is marked primary."},
        {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "Email address."},
                "label": {"type": "string", "description": "Optional label, e.g. work or home."},
                "isPrimary": {"type": "boolean", "description": "Whether this is the primary email."},
            },
            "required": ["value"],
        },
    ],
}

_CONTACT_PHONE_ENTRY_SCHEMA = {
    "oneOf": [
        {"type": "string", "description": "Phone number. The first string is marked primary."},
        {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "E.164 phone number."},
                "label": {"type": "string", "description": "Optional label, e.g. mobile or work."},
                "isPrimary": {"type": "boolean", "description": "Whether this is the primary phone."},
            },
            "required": ["value"],
        },
    ],
}

_CONTACT_WRITE_PROPERTIES = {
    "preferredName": {"type": "string", "description": "Display/preferred name."},
    "givenName": {"type": "string", "description": "Given/first name."},
    "familyName": {"type": "string", "description": "Family/last name."},
    "companyName": {"type": "string", "description": "Company or organization."},
    "jobTitle": {"type": "string", "description": "Job title."},
    "notes": {"type": "string", "description": "Free-form contact notes."},
    "emails": {"type": "array", "items": _CONTACT_EMAIL_ENTRY_SCHEMA, "description": "Email addresses. Strings or objects are accepted."},
    "phones": {"type": "array", "items": _CONTACT_PHONE_ENTRY_SCHEMA, "description": "Phone numbers. Strings or objects are accepted."},
}

CREATE_CONTACT_SCHEMA = {
    "name": "inkbox_create_contact",
    "description": "Create an Inkbox address-book contact visible according to Inkbox contact access rules.",
    "parameters": {
        "type": "object",
        "properties": dict(_CONTACT_WRITE_PROPERTIES),
    },
}

UPDATE_CONTACT_SCHEMA = {
    "name": "inkbox_update_contact",
    "description": "Update an existing Inkbox contact by UUID. Omitted fields are left unchanged; provided emails/phones replace those lists.",
    "parameters": {
        "type": "object",
        "properties": {
            "contactId": {"type": "string", "description": "UUID of the Inkbox contact."},
            **_CONTACT_WRITE_PROPERTIES,
        },
        "required": ["contactId"],
    },
}

DELETE_CONTACT_SCHEMA = {
    "name": "inkbox_delete_contact",
    "description": "Delete an Inkbox contact by UUID. Use only after confirming the target contact.",
    "parameters": {
        "type": "object",
        "properties": {
            "contactId": {"type": "string", "description": "UUID of the Inkbox contact."},
        },
        "required": ["contactId"],
    },
}

# Optional, additive property shared by the four send tools. Present it only
# when a send starts a sub-conversation on behalf of the current one and you
# intend to relay the recipient's answer back; omit it for an ordinary send.
_SPINOFF_PROPERTY = {
    "type": "object",
    "description": (
        "Set this when the message starts a sub-conversation on behalf of the "
        "conversation you're in — you're reaching out to gather info or delegate, "
        "and you intend to relay the recipient's answer back. The message body "
        "you write is what the recipient sees; keep the originating party's "
        "private details out of it unless you mean to share them. Omit entirely "
        "for an ordinary send."
    ),
    "properties": {
        "purpose": {"type": "string", "description": "Why this sub-conversation exists — the follow-up agent's goal. Required when spinoff is set."},
        "disclose": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Allowlist of originating-conversation facts the follow-up agent "
                "may reason from; everything else about the originating "
                "conversation stays out of its context entirely. Optionally prefix "
                "an item with \"label: value\"."
            ),
        },
        "success": {"type": "string", "description": "What a complete answer looks like — the follow-up agent relays back once this is met."},
        "constraints": {"type": "array", "items": {"type": "string"}, "description": "Optional guardrails for the follow-up agent."},
        "disclose_identity": {"type": "boolean", "description": "Whether the follow-up agent may name the originating party. Defaults to false (act on their behalf without naming them)."},
        "waitFor": {"type": "string", "description": "Optional fan-out grouping key: \"this\" or \"group:<id>\"."},
    },
    "required": ["purpose"],
}

SEND_EMAIL_SCHEMA = {
    "name": "inkbox_send_email",
    "description": "Send an email from the configured Inkbox identity.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses."},
            "subject": {"type": "string", "description": "Email subject."},
            "body_text": {"type": "string", "description": "Plain text body."},
            "body_html": {"type": "string", "description": "Optional HTML body."},
            "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}},
            "in_reply_to_message_id": {"type": "string", "description": "RFC 5322 Message-ID for threading replies."},
            "spinoff": _SPINOFF_PROPERTY,
        },
        "required": ["to", "subject"],
    },
}

SEND_SMS_SCHEMA = {
    "name": "inkbox_send_sms",
    "description": "Send a text from the configured Inkbox identity phone number. Use conversationId to reply into an existing 1:1 or group conversation, or to for one E.164 recipient or a 2-8 recipient group MMS.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "description": "One E.164 recipient or a list of 1-8 recipients. Two or more sends a group MMS.",
                "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}, "maxItems": 8}],
            },
            "conversationId": {"type": "string", "description": "Existing Inkbox text conversation UUID from inkbox_list_text_conversations. Preferred for replies and group chats. Mutually exclusive with `to`."},
            "text": {"type": "string", "description": "Message body, max 1600 chars."},
            "mediaUrls": {"type": "array", "items": {"type": "string"}, "maxItems": 10, "description": "Optional public MMS media URLs."},
            "spinoff": _SPINOFF_PROPERTY,
        },
        "required": ["text"],
    },
}

LIST_TEXT_CONVERSATIONS_SCHEMA = {
    "name": "inkbox_list_text_conversations",
    "description": "List text conversation summaries for the configured Inkbox identity phone number. Includes group chats by default and returns conversation IDs for replies.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "includeGroups": {"type": "boolean", "default": True, "description": "Include group conversations."},
        },
    },
}

GET_TEXT_CONVERSATION_SCHEMA = {
    "name": "inkbox_get_text_conversation",
    "description": "Fetch messages in a specific text conversation. Use conversationId for canonical rows and group chats; remotePhoneNumber is the legacy 1:1 fallback.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Inkbox text conversation UUID from inkbox_list_text_conversations."},
            "remotePhoneNumber": {"type": "string", "description": "Legacy 1:1 remote E.164 phone number identifying the conversation."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
    },
}

LIST_TEXTS_SCHEMA = {
    "name": "inkbox_list_texts",
    "description": "List individual SMS/MMS messages. Prefer inkbox_list_text_conversations for triage; this is low-level access.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "isRead": {"type": "boolean", "description": "Filter by read state."},
        },
    },
}

GET_TEXT_SCHEMA = {
    "name": "inkbox_get_text",
    "description": "Fetch a single SMS/MMS message by text message UUID.",
    "parameters": {
        "type": "object",
        "properties": {
            "textId": {"type": "string", "description": "UUID of the text message."},
        },
        "required": ["textId"],
    },
}

MARK_TEXT_READ_SCHEMA = {
    "name": "inkbox_mark_text_read",
    "description": "Mark a single SMS/MMS message as read.",
    "parameters": {
        "type": "object",
        "properties": {
            "textId": {"type": "string", "description": "UUID of the text message."},
        },
        "required": ["textId"],
    },
}

MARK_TEXT_CONVERSATION_READ_SCHEMA = {
    "name": "inkbox_mark_text_conversation_read",
    "description": "Mark every message in a text conversation as read. Use conversationId for canonical rows and group chats; remotePhoneNumber is the legacy 1:1 fallback.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Inkbox text conversation UUID from inkbox_list_text_conversations."},
            "remotePhoneNumber": {"type": "string", "description": "Legacy 1:1 remote E.164 phone number identifying the conversation."},
        },
    },
}

IMESSAGE_TRIAGE_NUMBER_SCHEMA = {
    "name": "inkbox_imessage_triage_number",
    "description": "Return the Inkbox iMessage router number and the connect command a person texts to it (from an iPhone) to reach this agent over iMessage. Share these when someone asks how to iMessage the agent.",
    "parameters": {"type": "object", "properties": {}},
}

SEND_IMESSAGE_SCHEMA = {
    "name": "inkbox_send_imessage",
    "description": "Send an iMessage from the configured Inkbox identity. Recipient-first channel: a person must have connected via the iMessage router and messaged this agent before outbound sends work, so prefer conversationId from an inbound message or inkbox_list_imessage_conversations.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Existing Inkbox iMessage conversation UUID. Preferred for replies. Mutually exclusive with `to`."},
            "to": {"type": "string", "description": "Recipient phone number in E.164 format. Only works after that person has messaged this agent. Mutually exclusive with `conversationId`."},
            "text": {"type": "string", "maxLength": IMESSAGE_MAX_LENGTH, "description": "Message body, max 18995 chars."},
            "mediaUrls": {"type": "array", "items": {"type": "string"}, "maxItems": 1, "description": "Optional media URL (at most one per message)."},
            "sendStyle": {
                "type": "string",
                "enum": ["celebration", "shooting_star", "fireworks", "lasers", "love", "confetti", "balloons", "spotlight", "echo", "invisible", "gentle", "loud", "slam"],
                "description": "Optional expressive iMessage send style.",
            },
            "spinoff": _SPINOFF_PROPERTY,
        },
    },
}

LIST_IMESSAGE_ASSIGNMENTS_SCHEMA = {
    "name": "inkbox_list_imessage_assignments",
    "description": "List the people actively connected to this agent over iMessage (one row per recipient, newest first). Released connections are not returned. Use to answer who the agent can currently iMessage.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
    },
}

LIST_IMESSAGE_CONVERSATIONS_SCHEMA = {
    "name": "inkbox_list_imessage_conversations",
    "description": "List iMessage conversation summaries for the configured Inkbox identity. Returns conversation IDs for replies, latest-message previews, unread counts, and assignment_status (released = that person disconnected; replies fail until they reconnect).",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
    },
}

GET_IMESSAGE_CONVERSATION_SCHEMA = {
    "name": "inkbox_get_imessage_conversation",
    "description": "Fetch messages in one iMessage conversation, newest first. Messages include any live tapback reactions.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Inkbox iMessage conversation UUID from inkbox_list_imessage_conversations."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["conversationId"],
    },
}

SEND_IMESSAGE_REACTION_SCHEMA = {
    "name": "inkbox_send_imessage_reaction",
    "description": "Send a tapback reaction to an iMessage the agent received.",
    "parameters": {
        "type": "object",
        "properties": {
            "messageId": {"type": "string", "description": "UUID of the iMessage being reacted to."},
            "reaction": {
                "type": "string",
                "enum": ["love", "like", "dislike", "laugh", "emphasize", "question"],
                "description": "Tapback kind.",
            },
            "partIndex": {"type": "integer", "minimum": 0, "default": 0, "description": "Part of a multi-part message to react to."},
        },
        "required": ["messageId", "reaction"],
    },
}

MARK_IMESSAGE_CONVERSATION_READ_SCHEMA = {
    "name": "inkbox_mark_imessage_conversation_read",
    "description": "Send a read receipt and mark every inbound message in an iMessage conversation as read.",
    "parameters": {
        "type": "object",
        "properties": {
            "conversationId": {"type": "string", "description": "Inkbox iMessage conversation UUID."},
        },
        "required": ["conversationId"],
    },
}

PLACE_CALL_SCHEMA = {
    "name": "inkbox_place_call",
    "description": (
        "Place an outbound voice call. Calls can go out over two lines: your "
        "own dedicated phone number, or the shared Inkbox iMessage line you are "
        "already messaging the recipient on. Match the channel you're talking on "
        "— call SMS/phone contacts from your dedicated number, and call an "
        "iMessage contact over the shared iMessage line (set `origination` "
        "accordingly). Always include purpose."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to_number": {"type": "string", "description": "Recipient phone number in E.164 format."},
            "purpose": {"type": "string", "description": "Why the call is being placed; loaded into the live call before greeting."},
            "origination": {
                "type": "string",
                "enum": ["dedicated_number", "shared_imessage_number"],
                "description": (
                    "Which line to call from. Use \"dedicated_number\" to call from your own "
                    "phone number (the same line SMS/voice conversations use). Use "
                    "\"shared_imessage_number\" to call someone over the shared iMessage line you "
                    "are already messaging them on — this only works if they are connected to you "
                    "over iMessage (otherwise the call is rejected). If omitted, it is resolved "
                    "automatically when only one path is available."
                ),
            },
            "opening_message": {"type": "string", "description": "Optional first thing to say when the call connects."},
            "context": {"type": "string", "description": "Optional concise background for the voice agent."},
            "client_websocket_url": {"type": "string", "description": "Optional explicit call media WebSocket URL."},
            "spinoff": _SPINOFF_PROPERTY,
        },
        "required": ["to_number", "purpose"],
    },
}

RELAY_ANSWER_SCHEMA = {
    "name": "inkbox_relay_answer",
    "description": (
        "Relay the answer from a spin-off sub-conversation back to the "
        "conversation that started it. Call this only from the sub-conversation, "
        "once you have what the originator asked for. Pass a short distilled "
        "summary (never the recipient's raw transcript); set satisfied=false to "
        "keep waiting if this message wasn't the answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "edge_id": {"type": "string", "description": "The spin-off edge id you are answering (from inkbox_spinoff_origin)."},
            "summary": {"type": "string", "description": "The answer to relay back to the originator, distilled to the point but INCLUDING the exact content they need — any specific codes, numbers, names, dates, or wording quoted verbatim. Required when satisfied is true."},
            "satisfied": {"type": "boolean", "description": "True if this completes the delegated task; false keeps the spin-off open."},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["value"],
                },
                "description": "Optional structured key/value results to relay alongside the summary.",
            },
        },
        "required": ["edge_id", "satisfied"],
    },
}

SPINOFF_LIST_SCHEMA = {
    "name": "inkbox_spinoff_list",
    "description": "List the spin-off sub-conversations this conversation started, with masked recipient and status. Open spin-offs only unless includeTerminal is set.",
    "parameters": {
        "type": "object",
        "properties": {
            "includeTerminal": {"type": "boolean", "default": False, "description": "Include closed/abandoned/relayed spin-offs too."},
        },
    },
}

LINEAGE_STATUS_SCHEMA = {
    "name": "inkbox_lineage_status",
    "description": "Check a spin-off's status. Pass edgeId for one edge's detail (including its relayed answer, if any), or omit it (or pass \"open\") to list this conversation's open spin-offs.",
    "parameters": {
        "type": "object",
        "properties": {
            "edgeId": {"type": "string", "description": "A spin-off edge id, or \"open\" / omitted to list all open ones."},
        },
    },
}

SPINOFF_ORIGIN_SCHEMA = {
    "name": "inkbox_spinoff_origin",
    "description": "List the delegated tasks this conversation owes an answer on — why it was spun off and what a complete answer looks like. Use it to frame a reply and to get the edge id for inkbox_relay_answer.",
    "parameters": {"type": "object", "properties": {}},
}


def register_tools(ctx) -> None:
    ctx.register_tool("inkbox_whoami", "inkbox", WHOAMI_SCHEMA, inkbox_whoami, check_fn=_configured)
    ctx.register_tool("inkbox_lookup_contact", "inkbox", LOOKUP_CONTACT_SCHEMA, inkbox_lookup_contact, check_fn=_configured)
    ctx.register_tool("inkbox_list_contacts", "inkbox", LIST_CONTACTS_SCHEMA, inkbox_list_contacts, check_fn=_configured)
    ctx.register_tool("inkbox_get_contact", "inkbox", GET_CONTACT_SCHEMA, inkbox_get_contact, check_fn=_configured)
    ctx.register_tool("inkbox_create_contact", "inkbox", CREATE_CONTACT_SCHEMA, inkbox_create_contact, check_fn=_configured)
    ctx.register_tool("inkbox_update_contact", "inkbox", UPDATE_CONTACT_SCHEMA, inkbox_update_contact, check_fn=_configured)
    ctx.register_tool("inkbox_delete_contact", "inkbox", DELETE_CONTACT_SCHEMA, inkbox_delete_contact, check_fn=_configured)
    ctx.register_tool("inkbox_send_email", "inkbox", SEND_EMAIL_SCHEMA, inkbox_send_email, check_fn=_configured)
    ctx.register_tool("inkbox_send_sms", "inkbox", SEND_SMS_SCHEMA, inkbox_send_sms, check_fn=_configured)
    ctx.register_tool("inkbox_list_text_conversations", "inkbox", LIST_TEXT_CONVERSATIONS_SCHEMA, inkbox_list_text_conversations, check_fn=_configured)
    ctx.register_tool("inkbox_get_text_conversation", "inkbox", GET_TEXT_CONVERSATION_SCHEMA, inkbox_get_text_conversation, check_fn=_configured)
    ctx.register_tool("inkbox_list_texts", "inkbox", LIST_TEXTS_SCHEMA, inkbox_list_texts, check_fn=_configured)
    ctx.register_tool("inkbox_get_text", "inkbox", GET_TEXT_SCHEMA, inkbox_get_text, check_fn=_configured)
    ctx.register_tool("inkbox_mark_text_read", "inkbox", MARK_TEXT_READ_SCHEMA, inkbox_mark_text_read, check_fn=_configured)
    ctx.register_tool("inkbox_mark_text_conversation_read", "inkbox", MARK_TEXT_CONVERSATION_READ_SCHEMA, inkbox_mark_text_conversation_read, check_fn=_configured)
    ctx.register_tool("inkbox_imessage_triage_number", "inkbox", IMESSAGE_TRIAGE_NUMBER_SCHEMA, inkbox_imessage_triage_number, check_fn=_configured)
    ctx.register_tool("inkbox_send_imessage", "inkbox", SEND_IMESSAGE_SCHEMA, inkbox_send_imessage, check_fn=_configured)
    ctx.register_tool("inkbox_list_imessage_assignments", "inkbox", LIST_IMESSAGE_ASSIGNMENTS_SCHEMA, inkbox_list_imessage_assignments, check_fn=_configured)
    ctx.register_tool("inkbox_list_imessage_conversations", "inkbox", LIST_IMESSAGE_CONVERSATIONS_SCHEMA, inkbox_list_imessage_conversations, check_fn=_configured)
    ctx.register_tool("inkbox_get_imessage_conversation", "inkbox", GET_IMESSAGE_CONVERSATION_SCHEMA, inkbox_get_imessage_conversation, check_fn=_configured)
    ctx.register_tool("inkbox_send_imessage_reaction", "inkbox", SEND_IMESSAGE_REACTION_SCHEMA, inkbox_send_imessage_reaction, check_fn=_configured)
    ctx.register_tool("inkbox_mark_imessage_conversation_read", "inkbox", MARK_IMESSAGE_CONVERSATION_READ_SCHEMA, inkbox_mark_imessage_conversation_read, check_fn=_configured)
    ctx.register_tool("inkbox_place_call", "inkbox", PLACE_CALL_SCHEMA, inkbox_place_call, check_fn=_configured)
    ctx.register_tool("inkbox_relay_answer", "inkbox", RELAY_ANSWER_SCHEMA, inkbox_relay_answer, check_fn=_configured)
    ctx.register_tool("inkbox_spinoff_list", "inkbox", SPINOFF_LIST_SCHEMA, inkbox_spinoff_list, check_fn=_configured)
    ctx.register_tool("inkbox_lineage_status", "inkbox", LINEAGE_STATUS_SCHEMA, inkbox_lineage_status, check_fn=_configured)
    ctx.register_tool("inkbox_spinoff_origin", "inkbox", SPINOFF_ORIGIN_SCHEMA, inkbox_spinoff_origin, check_fn=_configured)
