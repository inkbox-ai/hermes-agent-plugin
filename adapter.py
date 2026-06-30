"""Inkbox platform adapter.

Inkbox (https://inkbox.ai) is API-first communication infrastructure that
gives an AI agent a stable email address, phone number, and persistent
contact list scoped to one *agent identity*. This adapter routes the
four Inkbox modalities — inbound email, inbound SMS, inbound iMessage,
and live voice calls — into a single contact-keyed Hermes session per
remote party.

Architecture
------------
On ``connect()`` the adapter:
  1. Brings up the Inkbox edge-mode tunnel attached to the configured
     identity (tunnels are provisioned atomically when the identity is
     created — there is no standalone ``POST /api/v1/tunnels``). The
     public URL is ``https://{agent_handle}.{env}.inkboxwire.com`` and
     the tunnel id is persisted in HERMES_HOME so subsequent runs reuse
     the same tunnel. Data-plane auth uses the SDK client's ``x-api-key``
     directly. Production deployments can bypass tunneling entirely by
     setting ``INKBOX_PUBLIC_URL``.
  2. Registers webhook subscriptions for the configured identity's
     mailbox (``message.*`` events), phone number (``text.*``
     events), and — when the identity is iMessage-enabled — the
     identity itself (``imessage.*`` events; iMessage rides shared
     Inkbox-managed numbers, so the subscription owner is the agent
     identity rather than a phone number) pointing at the tunnel, and
     patches the phone number's incoming-call webhook URL + WebSocket
     URL on the resource itself (the call channel is a synchronous
     control-plane callback and is not a fan-out subscription).
  3. Starts an aiohttp server with two routes:
        - ``POST /webhook`` — verifies the ``X-Inkbox-Signature`` HMAC
          via the SDK, parses the body into one of three event shapes
          (mail / SMS / call), resolves the remote party to a Contact
          via ``inkbox.contacts.lookup()``, and pushes a
          :class:`MessageEvent` onto the gateway runner.
        - ``WS /phone/media/ws`` — live-call media bridge. Receives
          ``transcript`` events from Inkbox, hands each finalized
          transcript turn to the gateway as a MessageEvent, and pushes
          the agent's streamed response back as ``text`` frames for
          Inkbox-managed TTS playback.

Session keys
------------
Every inbound event is mapped to ``chat_id = contact_id`` so that one
Hermes session spans email + SMS + voice for the same remote party::

    inbound mail     → chat_id=contact_id, thread_id=f"email:{tid}"
    inbound SMS      → chat_id=contact_id, thread_id=None
    inbound iMessage → chat_id=contact_id, thread_id=f"imessage:{cid}"
    inbound call     → chat_id=contact_id, thread_id=f"call:{call_id}"
    outbound call    → chat_id=contact_id, thread_id=None  (joins the
                       contact's main session so the agent inherits the
                       conversation that decided to place the call)

When ``inkbox.contacts.lookup()`` returns 0 or >1 contacts the adapter
falls back to the raw email address / phone number as ``chat_id``, so
unknown senders still get a session — just not a contact-merged one.

Outbound
--------
``send()`` is mode-aware via ``metadata['mode']``:
  - ``email``    → ``identity.send_email(to=..., subject=..., body_text=...)``
  - ``sms``      → ``identity.send_text(conversation_id=..., text=...)``
                   for replies, falling back to ``to=...`` for legacy/new sends
  - ``imessage`` → ``identity.send_imessage(conversation_id=..., text=...)``.
    iMessage is recipient-first: the remote party must have messaged the
    agent at least once before outbound sends work, so replies always
    target an existing conversation.
  - ``voice``    → push a ``text`` frame onto the contact's active call
    WebSocket so Inkbox-managed TTS speaks it to the caller.

When the agent streams (gateway calls ``edit_message()`` repeatedly),
voice deltas are forwarded to the WS as incremental ``text`` events;
email and SMS edits are no-ops (the platforms have no native edit
semantics).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import re
import socket as _socket
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

if TYPE_CHECKING:
    # Used only in forward-ref annotations (the module has `from __future__
    # import annotations`); both are imported locally where actually called.
    import threading
    from pathlib import Path

try:
    from aiohttp import WSMsgType, web

    AIOHTTP_AVAILABLE = True
except ImportError:
    web = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    from inkbox import Inkbox, verify_webhook
    from inkbox.exceptions import InkboxAPIError

    INKBOX_AVAILABLE = True
except ImportError:
    Inkbox = None  # type: ignore[assignment]
    verify_webhook = None  # type: ignore[assignment]
    InkboxAPIError = Exception  # type: ignore[assignment,misc]
    INKBOX_AVAILABLE = False

try:
    from inkbox.tunnels.client import (
        TunnelListener,
        connect as inkbox_tunnel_connect,
    )

    INKBOX_TUNNEL_AVAILABLE = True
except ImportError:
    TunnelListener = None  # type: ignore[assignment]
    inkbox_tunnel_connect = None  # type: ignore[assignment]
    INKBOX_TUNNEL_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.platforms.helpers import redact_phone
try:
    from .config import INKBOX_BASE_URL_DEFAULT, inkbox_client_kwargs
    from .realtime import (
        DEFAULT_MODEL as REALTIME_DEFAULT_MODEL,
        DEFAULT_VOICE as REALTIME_DEFAULT_VOICE,
        DEFAULT_CONSULT_TIMEOUT_S as REALTIME_DEFAULT_CONSULT_TIMEOUT_S,
        RealtimeCallMeta,
        RealtimeConfig,
        RealtimeBridgeConnectError,
        RealtimeConsultResult,
        open_inkbox_realtime_bridge,
    )
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import INKBOX_BASE_URL_DEFAULT, inkbox_client_kwargs
    from realtime import (
        DEFAULT_MODEL as REALTIME_DEFAULT_MODEL,
        DEFAULT_VOICE as REALTIME_DEFAULT_VOICE,
        DEFAULT_CONSULT_TIMEOUT_S as REALTIME_DEFAULT_CONSULT_TIMEOUT_S,
        RealtimeCallMeta,
        RealtimeConfig,
        RealtimeBridgeConnectError,
        RealtimeConsultResult,
        open_inkbox_realtime_bridge,
    )

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_WEBHOOK_PATH = "/webhook"
DEFAULT_WS_PATH = "/phone/media/ws"
CONTACT_CACHE_TTL_SECONDS = 300
WEBHOOK_DEDUP_TTL_SECONDS = 300
SMS_MAX_LENGTH = 1600  # Inkbox SMS hard cap
IMESSAGE_MAX_LENGTH = 18995  # Sendblue-compatible iMessage text cap
SMS_TEXT_BATCH_DELAY_SECONDS = 0.0
SMS_TEXT_BATCH_MAX_MESSAGES = 8
SMS_TEXT_BATCH_MAX_CHARS = 4000


def _inkbox_platform() -> Platform:
    """Resolve the dynamic Inkbox platform after plugin registration."""
    return Platform("inkbox")

# Mail: agent only acts on inbound; lifecycle events fire-and-forget at the
# wire layer, so subscribing to them would pay signature cost for no behaviour.
_DESIRED_MAIL_EVENTS: tuple[str, ...] = ("message.received",)

# Text: inbound plus the four outbound lifecycle transitions are all consumed
# by _on_text_received / _on_text_lifecycle.
_DESIRED_TEXT_EVENTS: tuple[str, ...] = (
    "text.received",
    "text.sent",
    "text.delivered",
    "text.delivery_failed",
    "text.delivery_unconfirmed",
)

# iMessage: inbound plus the outbound delivery lifecycle, consumed by
# _on_imessage_received / _on_imessage_lifecycle — same split as text.
# Tapback reactions (``imessage.reaction_received``) are subscribed and
# routed to _on_imessage_reaction, which enqueues a turn carrying the
# reaction + a response policy: the agent decides whether to reply or emit
# [SILENT] (a "?" tapback usually warrants a reply, a "love" usually does
# not). The agent can also send its own tapbacks via inkbox_send_imessage_reaction.
_DESIRED_IMESSAGE_EVENTS: tuple[str, ...] = (
    "imessage.received",
    "imessage.sent",
    "imessage.delivered",
    "imessage.delivery_failed",
    "imessage.reaction_received",
)


def _inkbox_state_path():
    """Return the on-disk path used for the Inkbox identity state file.

    Single source of truth shared by the reader and the writer so the
    path can never drift between them. Imports are local because the
    hermes_cli package may not be importable in every consumer (tests).
    """
    from hermes_cli.config import get_hermes_home
    return get_hermes_home() / "inkbox_identity_state.json"


def _read_previous_webhook_url() -> Optional[str]:
    """Read the prior webhook_url out of the identity state file.

    Returns ``None`` on missing file, bad JSON, permission error, or any
    other failure — callers treat ``None`` as "no prior URL recorded".
    """
    try:
        path = _inkbox_state_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text())
    except Exception as exc:
        logger.debug("[Inkbox] Could not read prior identity state: %s", exc)
        return None

    # State file might be from a future schema, hand-edited, or truncated
    # to a non-object root — treat anything other than a dict as "no prior".
    if not isinstance(data, dict):
        return None

    url = data.get("webhook_url")
    return url if isinstance(url, str) and url else None


def _sms_conversation_target(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    without_provider = re.sub(r"^(inkbox:)", "", text, flags=re.IGNORECASE)
    match = re.match(
        r"^(?:sms:conversation:|text:conversation:|phone:conversation:|conversation:|thread:)(.+)$",
        without_provider,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip() or None
    match = re.match(r"^(?:sms:|text:|phone:)(.+)$", without_provider, flags=re.IGNORECASE)
    if match:
        candidate = match.group(1).strip()
        return candidate if candidate and not candidate.startswith("+") else None
    return None


def _imessage_conversation_target(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    without_provider = re.sub(r"^(inkbox:)", "", text, flags=re.IGNORECASE)
    match = re.match(
        r"^(?:imessage:conversation:|imessage:)(.+)$",
        without_provider,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip()
        return candidate if candidate and not candidate.startswith("+") else None
    return None


def _sms_state_key(chat_id: Any, thread_id: Any = None) -> str:
    chat = str(chat_id or "").strip()
    thread = str(thread_id or "").strip()
    return f"{chat}|{thread}" if thread else chat


def _channel_thread_key(prefix: str, value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    return f"{prefix}:{raw}" if raw else None


def _chat_id_for_route(
    contact: Optional[Dict[str, Any]],
    thread_key: Optional[str],
    fallback: str,
) -> str:
    if contact and contact.get("id"):
        return str(contact["id"])
    if thread_key:
        return thread_key
    return fallback


def _field(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj.get(name)
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _list_field(obj: Any, *names: str) -> list[Any]:
    value = _field(obj, *names)
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _string_list_field(obj: Any, *names: str) -> list[str]:
    return [str(item).strip() for item in _list_field(obj, *names) if str(item).strip()]


def _webhook_list(data: Dict[str, Any], *names: str) -> list[Any]:
    for name in names:
        value = data.get(name)
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
    return []


def _conversation_summary_is_group(summary: Any) -> bool:
    return bool(_field(summary, "isGroup", "is_group", "is_group_conversation"))


def _realtime_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return default


def _resolve_realtime_config(extra: Dict[str, Any]) -> RealtimeConfig:
    rt_extra = extra.get("realtime") if isinstance(extra.get("realtime"), dict) else {}
    rt_api_key = (
        (rt_extra.get("api_key") or "").strip()
        or os.getenv("INKBOX_REALTIME_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    rt_has_cred = bool(rt_api_key)
    rt_enabled_raw = (
        rt_extra.get("enabled")
        if "enabled" in rt_extra
        else os.getenv("INKBOX_REALTIME_ENABLED")
    )
    rt_enabled_text = (
        str(rt_enabled_raw).strip().lower()
        if rt_enabled_raw is not None
        else "auto"
    )
    rt_enabled_requested = rt_enabled_text in ("auto", "true", "1", "yes", "on")
    config = RealtimeConfig(
        enabled=rt_enabled_requested and rt_has_cred,
        api_key=rt_api_key,
        model=str(rt_extra.get("model") or os.getenv("INKBOX_REALTIME_MODEL") or REALTIME_DEFAULT_MODEL),
        voice=str(rt_extra.get("voice") or os.getenv("INKBOX_REALTIME_VOICE") or REALTIME_DEFAULT_VOICE),
        additional_instructions=str(rt_extra.get("additional_instructions") or ""),
        consult_timeout_s=float(
            rt_extra.get("consult_timeout_s")
            or os.getenv("INKBOX_REALTIME_CONSULT_TIMEOUT_S")
            or REALTIME_DEFAULT_CONSULT_TIMEOUT_S
        ),
        connect_timeout_s=float(
            rt_extra.get("connect_timeout_s")
            or os.getenv("INKBOX_REALTIME_CONNECT_TIMEOUT_S")
            or 8.0
        ),
        fallback_to_inkbox_stt_tts=_realtime_bool(
            rt_extra.get("fallback_to_inkbox_stt_tts")
            if "fallback_to_inkbox_stt_tts" in rt_extra
            else os.getenv("INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS"),
            True,
        ),
    )
    if rt_enabled_text in ("true", "1", "yes", "on") and not rt_has_cred:
        logger.warning(
            "[Inkbox] realtime voice was enabled but no OpenAI credential was found "
            "(checked realtime.api_key, INKBOX_REALTIME_API_KEY, OPENAI_API_KEY); "
            "falling back to Inkbox-side STT/TTS for calls.",
        )
    return config


def _reconcile_subscription(
    client,
    *,
    owner_kwarg: str,
    owner_id,
    desired_url: str,
    previous_webhook_url: Optional[str],
    desired_events: tuple[str, ...],
):
    """Reconcile a single owner's webhook subscription against desired state.

    ``owner_kwarg`` is ``"mailbox_id"`` or ``"phone_number_id"``. Returns the
    active subscription's id for DEBUG logging at the call site.
    """
    desired_set = set(desired_events)
    list_kwargs = {owner_kwarg: owner_id}
    existing = client.webhooks.subscriptions.list(**list_kwargs)

    # Same URL + same event set: adopt verbatim, no writes.
    for row in existing:
        if row.url == desired_url and set(row.event_types) == desired_set:
            active_id = row.id
            break
    else:
        # Same URL but drifted event set: patch in place, do not delete-recreate.
        drifted = next(
            (r for r in existing if r.url == desired_url), None,
        )
        if drifted is not None:
            updated = client.webhooks.subscriptions.update(
                drifted.id, event_types=list(desired_events),
            )
            active_id = updated.id
        else:
            active_id = _create_with_409_repair(
                client,
                owner_kwarg=owner_kwarg,
                owner_id=owner_id,
                desired_url=desired_url,
                desired_events=desired_events,
            )

    # Previous-URL cleanup runs after the new row is in place so a failure
    # mid-reconcile can never leave the owner with zero receivers.
    if previous_webhook_url and previous_webhook_url != desired_url:
        # Re-list rather than reusing ``existing`` because a create/update
        # may have shifted the visible rows.
        for row in client.webhooks.subscriptions.list(**list_kwargs):
            if row.url == previous_webhook_url:
                try:
                    client.webhooks.subscriptions.delete(row.id)
                except InkboxAPIError as exc:
                    if exc.status_code == 404:
                        pass  # already gone; fine
                    else:
                        raise
                break

    return active_id


def _create_with_409_repair(
    client,
    *,
    owner_kwarg: str,
    owner_id,
    desired_url: str,
    desired_events: tuple[str, ...],
):
    """POST a new subscription; on a 409 race, adopt or repair the existing row.

    Server uniqueness is ``(owner, url)`` only — event set is not part of it.
    So a 409 may surface a row with a different event set; check and patch.
    """
    create_kwargs = {
        owner_kwarg: owner_id,
        "url": desired_url,
        "event_types": list(desired_events),
    }
    try:
        sub = client.webhooks.subscriptions.create(**create_kwargs)
        return sub.id
    except InkboxAPIError as exc:
        if exc.status_code != 409:
            raise

    desired_set = set(desired_events)
    list_kwargs = {owner_kwarg: owner_id}
    for row in client.webhooks.subscriptions.list(**list_kwargs):
        if row.url != desired_url:
            continue

        if set(row.event_types) == desired_set:
            return row.id

        repaired = client.webhooks.subscriptions.update(
            row.id, event_types=list(desired_events),
        )
        return repaired.id

    # Theoretically unreachable: 409 says the row exists, but the followup
    # list didn't surface it. Re-raise the original collision shape so
    # higher layers see a clear failure rather than a None.
    raise InkboxAPIError(
        status_code=409,
        detail=(
            f"Webhook subscription collision on {owner_kwarg}={owner_id} "
            f"url={desired_url}, but follow-up list did not return the row."
        ),
    )


def _reconcile_mail_subscription(
    client,
    mailbox_id,
    desired_url: str,
    previous_webhook_url: Optional[str],
    desired_events: tuple[str, ...] = _DESIRED_MAIL_EVENTS,
):
    """Reconcile a mailbox's webhook subscription against the desired state."""
    return _reconcile_subscription(
        client,
        owner_kwarg="mailbox_id",
        owner_id=mailbox_id,
        desired_url=desired_url,
        previous_webhook_url=previous_webhook_url,
        desired_events=desired_events,
    )


def _reconcile_text_subscription(
    client,
    phone_number_id,
    desired_url: str,
    previous_webhook_url: Optional[str],
    desired_events: tuple[str, ...] = _DESIRED_TEXT_EVENTS,
):
    """Reconcile a phone number's text webhook subscription."""
    return _reconcile_subscription(
        client,
        owner_kwarg="phone_number_id",
        owner_id=phone_number_id,
        desired_url=desired_url,
        previous_webhook_url=previous_webhook_url,
        desired_events=desired_events,
    )


def _reconcile_imessage_subscription(
    client,
    agent_identity_id,
    desired_url: str,
    previous_webhook_url: Optional[str],
    desired_events: tuple[str, ...] = _DESIRED_IMESSAGE_EVENTS,
):
    """Reconcile the identity-owned iMessage webhook subscription.

    iMessage traffic rides shared Inkbox-managed numbers, so the
    subscription owner is the agent identity, not a phone number.
    """
    return _reconcile_subscription(
        client,
        owner_kwarg="agent_identity_id",
        owner_id=agent_identity_id,
        desired_url=desired_url,
        previous_webhook_url=previous_webhook_url,
        desired_events=desired_events,
    )

SMS_CONTROL_WORDS = frozenset({
    "start",
    "stop",
    "unstop",
    "help",
    "cancel",
    "end",
    "quit",
    "yes",
    "subscribe",
    "info",
    "unsubscribe",
})

# Stable ``error`` codes returned by the Inkbox SMS send endpoint. Sourced
# from the live server (apps/api_server/subapps/phone/send_text_service.py).
SMS_SENDER_PROVISIONING_ERROR_CODES = frozenset({
    "sender_sms_pending",
    "sender_sms_assignment_failed",
    "sender_not_registered",
    "sender_registration_required",
    "messaging_profile_disabled",
    "toll_free_sms_unsupported",
})
SMS_CONSENT_ERROR_CODES = frozenset({
    "recipient_not_opted_in",
    "recipient_opted_out",
    "recipient_blocked",
})
SMS_RATE_LIMIT_ERROR_CODES = frozenset({
    "carrier_rate_limit",
    "sender_rate_limited",
})
SMS_CONTENT_LENGTH_ERROR_CODES = frozenset({
    # ``message_too_long`` is what the Inkbox server emits for upstream
    # length rejections. ``sms_too_long`` is set by the local pre-check in
    # ``_sms_too_long_fields`` and is bypassed by the classifier path (the
    # failure is constructed directly), so it does not need to live here.
    "message_too_long",
})
SMS_TRANSIENT_ERROR_CODES = frozenset({
    "carrier_unavailable",
})
SMS_PERMANENT_ERROR_CODES = frozenset({
    "invalid_phone_number",
    "carrier_rejected",
})

# Hermes emits a few classes of admin/system notice via adapter.send() —
# session-reset banners ("◐ ..."), runtime info blocks ("◆ Model: ..."),
# the home-channel prompt ("📬 No home channel..."), update/restart notes
# ("🔄 ..."), check/x-mark status pings ("✓ ..." / "✗ ..."), and the
# warning prefix ("⚠️ ...").  These are CLI/terminal-style chatter that
# leaks into the user's actual mailbox or SMS thread on Inkbox.  Drop
# them at adapter.send() so they never get delivered as real messages.
_ADMIN_NOTICE_PREFIXES: Tuple[str, ...] = (
    "◐", "◆", "📬", "🔄", "✓", "✗", "⚠️", "⚠", "⚡", "💡", "⏳",
    # Tool-call narration glyphs — Hermes emits these as interim
    # "I'm running X right now" updates while streaming. They have no
    # place in a real SMS or email thread.
    "💻",  # terminal / bash
    "🔎",  # grep / search_files
    "📖",  # read
    "📝",  # write / edit
    "📚",  # skill load
    "📋",  # todo / task planning
    "🐍",  # exec / python
    "🌐",  # web fetch
    "🧠",  # thinking / reasoning
    "⚙️",  # default Hermes tool-progress glyph
    "⚙",  # default Hermes tool-progress glyph without variation selector
    "🛠",  # tool generic
    "🔧",  # tool generic alt
    # Save/cache/persistence glyph — covers the background self-improvement
    # review banner ("💾 Self-improvement review: User profile updated · …"),
    # prompt-cache + cached-context status pings, trajectory-compressor
    # "Metrics saved to …" notices, etc.
    "💾",
)

# Substrings that mark CLI/TUI runtime chatter even when the leading glyph is
# absent (some Hermes notices fold across sentences, e.g. the busy/queue tip
# arrives mid-paragraph after the ⚡ banner).  Match any of these → suppress.
#
# Kept narrow on purpose: these patterns run on every outbound message body
# (including real user replies), so each entry needs to be specific enough
# that a legitimate agent reply can't reasonably contain it.  De-glyphed
# diagnostic catch-alls (compression dumps, token counts, "stack trace"…)
# live behind ``metadata['notice_type']`` instead — see
# ``_ADMIN_NOTICE_METADATA_TYPES`` — so producers tag themselves and final
# replies stay safe.
_ADMIN_NOTICE_SUBSTRINGS: Tuple[str, ...] = (
    "Interrupting current task",
    "First-time tip",
    "/busy queue",
    "/busy steer",
    "/busy status",
    "Session automatically reset",
    "No home channel is set",
    "Still working",
    "min elapsed — iteration",
    "Cronjob Response:",
    # Belt-and-suspenders: the self-improvement banner is sometimes
    # forwarded with the leading glyph stripped upstream of us.
    "Self-improvement review:",
)

# Hermes gateway tool-progress messages are shaped like:
#   "⏰ cronjob: \"create\""
#   "📨 send_message..."
#   "⚙️ mcp.tool(['arg'])\n{...}"
# The forked gateway can skip them before dispatch via supports_progress_updates,
# but older/fresh installs may still route them through edit_message().  Keep a
# final body-shape filter here so live calls never speak UI/tool chrome.
_TOOL_PROGRESS_GLYPHS: Tuple[str, ...] = (
    "⚙️", "⚙", "⏰", "📨", "✉️", "✉", "✍️", "✍",
    "💻", "🔎", "🔍", "📖", "📄", "📝", "📚", "📋",
    "🐍", "🌐", "🧠", "🛠", "🔧", "🔊", "👁️", "👁",
    "🎨", "🎬", "🏠", "🐦", "👥", "➕", "▶", "✔",
    "⏸", "💓", "💬", "🔗",
)
_TOOL_PROGRESS_TAIL_RE = re.compile(
    r"\s+[A-Za-z_][A-Za-z0-9_.-]*(?:\s*\(|\s*:|\.{3})(?:\s|$)",
)

# Internal producers (status_callback, interim assistant chatter, the
# "Still working" notifier, the trajectory compressor…) tag their outbound
# sends with ``metadata['notice_type']`` so the adapter can drop them
# without inspecting the body.  This is what lets a *real* agent reply
# containing the phrase "stack trace" or "5,000 tokens" survive — the body
# filter above is intentionally narrow, and the broader catch-all happens
# here via explicit producer opt-in.
#
# Only ``notice_type`` is honored.  Earlier revisions also accepted the
# aliases ``event_type`` / ``kind`` / ``source``, but those keys are already
# used elsewhere for unrelated purposes (e.g. ``event_type="state_changed"``
# on Home Assistant, ``source="inkbox"`` in session data).  Sharing the
# namespace risked silent suppression on future generic uses, so the alias
# list was dropped — producers must opt in with ``notice_type`` explicitly.
_ADMIN_NOTICE_METADATA_KEYS: Tuple[str, ...] = ("notice_type",)
_ADMIN_NOTICE_METADATA_TYPES: frozenset = frozenset({
    "admin",
    "admin_notice",
    "compression",
    "context_rollover",
    "interim_assistant",
    "notify_interval",
    "preflight",
    "preflight_compression",
    "provider_diagnostic",
    "runtime_diagnostic",
    "session_diagnostic",
    "status_callback",
    "system",
    "tool_progress",
})


def _is_hermes_admin_notice(
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when *content* is a Hermes-internal status/admin chatter line.

    Args:
        content: The outbound message body to inspect.
        metadata: Optional adapter metadata; when present, a recognized
            ``notice_type`` short-circuits the body inspection.

    Returns:
        bool: True if the message should be dropped before delivery.

    Triggered when (a) ``metadata`` carries a recognized notice-type tag,
    (b) the message starts with one of the well-known glyphs Hermes uses
    to flag system messages in the CLI/TUI, or (c) the body contains any
    of ``_ADMIN_NOTICE_SUBSTRINGS``.  These have no business landing in a
    real human's email inbox, SMS thread, or — worst of all — being read
    aloud as TTS over a live phone call.
    """
    # Metadata tag wins outright — producers that opt in to the channel
    # don't need their body inspected.
    if metadata:
        for key in _ADMIN_NOTICE_METADATA_KEYS:
            tag = str(metadata.get(key) or "").lower().strip()
            if tag and tag in _ADMIN_NOTICE_METADATA_TYPES:
                return True
    head = (content or "").lstrip().lstrip("﻿")
    if head.startswith(_ADMIN_NOTICE_PREFIXES):
        return True
    for glyph in _TOOL_PROGRESS_GLYPHS:
        if head.startswith(glyph) and _TOOL_PROGRESS_TAIL_RE.match(head[len(glyph):]):
            return True
    return any(s in head for s in _ADMIN_NOTICE_SUBSTRINGS)


def _float_setting(extra: Dict[str, Any], key: str, env_name: str, default: float) -> float:
    raw = extra[key] if key in extra else os.getenv(env_name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(0.0, value)


def _int_setting(extra: Dict[str, Any], key: str, env_name: str, default: int) -> int:
    raw = extra[key] if key in extra else os.getenv(env_name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def _parse_inkbox_timestamp(value: Any) -> datetime:
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _format_inkbox_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_sms_delta(first_at: datetime, current_at: datetime) -> str:
    seconds = max(0, int(round((current_at - first_at).total_seconds())))
    return f"+{seconds}s"


def _normalized_sms_control_word(text: str) -> Optional[str]:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized if normalized in SMS_CONTROL_WORDS else None


def _plain_value(value: Any) -> Optional[str]:
    """Return enum-like values as strings without leaking object repr noise."""
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return text or None


def _normalize_email_address(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"<([^<>]+)>", text)
    if match:
        text = match.group(1).strip()
    text = re.sub(r"^mailto:", "", text, flags=re.IGNORECASE).strip().lower()
    return text if "@" in text else ""


def _normalized_identity_handle(value: Any) -> str:
    return str(value or "").strip().lower()


def _identity_email_addresses(identity: Any) -> set[str]:
    mailbox = _field(identity, "mailbox")
    return {
        addr
        for addr in (
            _normalize_email_address(_field(mailbox, "email_address", "emailAddress")),
            _normalize_email_address(_field(identity, "email_address", "emailAddress")),
        )
        if addr
    }


def _mail_agent_identity_matches(
    envelope: Dict[str, Any],
    from_address: str,
    *,
    identity_handle: str = "",
    identity_id: str = "",
) -> bool:
    handle = _normalized_identity_handle(identity_handle)
    own_id = str(identity_id or "").strip()
    if not handle and not own_id:
        return False
    identities = _webhook_list(
        envelope.get("data") or {},
        "agent_identities",
        "agentIdentities",
    )
    for entry in identities:
        bucket = _field(entry, "bucket")
        address = _normalize_email_address(_field(entry, "address"))
        if bucket != "from" or address != from_address:
            continue
        entry_id = str(_field(entry, "id") or "").strip()
        entry_handle = _normalized_identity_handle(
            _field(entry, "agent_handle", "agentHandle")
        )
        if (own_id and entry_id == own_id) or (handle and entry_handle == handle):
            return True
    return False


def _json_safe_detail(value: Any) -> Any:
    """Keep structured provider error details if they are JSON-safe."""
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sms_error_body(detail: Any) -> Dict[str, Any]:
    """Normalize Inkbox API error payloads to the innermost detail dict."""
    if not isinstance(detail, dict):
        return {}
    nested = detail.get("detail")
    if isinstance(nested, dict):
        return nested
    return detail


def _classify_sms_error(
    status_code: Optional[int],
    error_code: Optional[str],
    message: str,
) -> Tuple[str, bool]:
    """Return ``(category, retryable)`` for an Inkbox SMS send failure."""
    code = (error_code or "").lower().strip()
    lower_msg = (message or "").lower()

    if code in SMS_TRANSIENT_ERROR_CODES:
        return ("transient", True)
    if code in SMS_SENDER_PROVISIONING_ERROR_CODES:
        return ("sender_provisioning", False)
    if code in SMS_CONSENT_ERROR_CODES:
        return ("recipient_consent", False)
    if code in SMS_RATE_LIMIT_ERROR_CODES:
        return ("rate_limit", False)
    if code in SMS_CONTENT_LENGTH_ERROR_CODES:
        return ("content_length", False)
    if code in SMS_PERMANENT_ERROR_CODES:
        return ("permanent", False)

    if status_code in {408, 500, 502, 503, 504}:
        return ("transient", True)
    if status_code == 429:
        return ("rate_limit", False)
    if status_code == 409:
        return ("conflict", False)
    if status_code is not None and 400 <= status_code < 500:
        return ("permanent", False)

    if any(marker in lower_msg for marker in ("timeout", "temporar", "connection")):
        return ("transient", True)
    return ("sdk_error", False)


def _sms_too_long_fields(content: str, *, max_chars: int = SMS_MAX_LENGTH) -> Dict[str, Any]:
    char_count = len(content or "")
    return {
        "status_code": None,
        "error_code": "sms_too_long",
        "message": f"SMS text is {char_count} characters; maximum is {max_chars}. Shorten it or split it into smaller SMS messages.",
        "detail": None,
        "category": "content_length",
        "retryable": False,
        "char_count": char_count,
        "max_chars": max_chars,
        "fallback_allowed": False,
    }


def _sms_too_long_failure(content: str, *, max_chars: int = SMS_MAX_LENGTH) -> SendResult:
    fields = _sms_too_long_fields(content, max_chars=max_chars)
    return SendResult(
        success=False,
        error=_format_inkbox_sms_error(fields),
        raw_response={"platform": "inkbox", "mode": "sms", **fields},
        retryable=False,
    )


def _sms_too_long_failure_dict(content: str, *, max_chars: int = SMS_MAX_LENGTH) -> Dict[str, Any]:
    fields = _sms_too_long_fields(content, max_chars=max_chars)
    return {
        "success": False,
        "platform": "inkbox",
        "mode": "sms",
        "error": _format_inkbox_sms_error(fields),
        **fields,
    }


def _imessage_too_long_fields(content: str, *, max_chars: int = IMESSAGE_MAX_LENGTH) -> Dict[str, Any]:
    char_count = len(content or "")
    return {
        "status_code": None,
        "error_code": "imessage_too_long",
        "message": f"iMessage text is {char_count} characters; maximum is {max_chars}. Shorten it or split it into smaller iMessages.",
        "detail": None,
        "category": "content_length",
        "retryable": False,
        "char_count": char_count,
        "max_chars": max_chars,
        "fallback_allowed": False,
    }


def _imessage_too_long_failure(content: str, *, max_chars: int = IMESSAGE_MAX_LENGTH) -> SendResult:
    fields = _imessage_too_long_fields(content, max_chars=max_chars)
    return SendResult(
        success=False,
        error=_format_inkbox_imessage_error(fields),
        raw_response={"platform": "inkbox", "mode": "imessage", **fields},
        retryable=False,
    )


def _imessage_too_long_failure_dict(content: str, *, max_chars: int = IMESSAGE_MAX_LENGTH) -> Dict[str, Any]:
    fields = _imessage_too_long_fields(content, max_chars=max_chars)
    return {
        "success": False,
        "platform": "inkbox",
        "mode": "imessage",
        "error": _format_inkbox_imessage_error(fields),
        **fields,
    }


def _extract_inkbox_sms_error(exc: Exception) -> Dict[str, Any]:
    """Extract structured fields from SDK exceptions without depending on SDK types."""
    status_code = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)

    body = _sms_error_body(detail)
    error_code = _plain_value(
        body.get("error")
        or body.get("code")
        or getattr(exc, "code", None)
    )
    message = _plain_value(body.get("message"))
    if not message:
        nested = body.get("detail")
        message = nested if isinstance(nested, str) else None
    if not message:
        message = str(exc) or exc.__class__.__name__

    category, retryable = _classify_sms_error(status_code, error_code, message)
    return {
        "status_code": status_code,
        "error_code": error_code,
        "message": message,
        "detail": _json_safe_detail(detail),
        "category": category,
        "retryable": retryable,
    }


def _format_inkbox_sms_error(fields: Dict[str, Any]) -> str:
    status = fields.get("status_code")
    code = fields.get("error_code")
    message = fields.get("message") or "send_text failed"
    prefix = "Inkbox SMS send failed"
    if status:
        prefix += f" (HTTP {status})"
    if code:
        prefix += f" [{code}]"
    return f"{prefix}: {message}"


def _sms_send_failure(exc: Exception, *, to_number: str) -> SendResult:
    fields = _extract_inkbox_sms_error(exc)
    logger.error(
        "[Inkbox] SMS send failed to %s: status=%s code=%s category=%s retryable=%s message=%s",
        redact_phone(to_number),
        fields.get("status_code"),
        fields.get("error_code"),
        fields.get("category"),
        fields.get("retryable"),
        fields.get("message"),
    )
    return SendResult(
        success=False,
        error=_format_inkbox_sms_error(fields),
        raw_response={"platform": "inkbox", "mode": "sms", **fields},
        retryable=bool(fields.get("retryable")),
    )


def _sms_send_failure_dict(exc: Exception) -> Dict[str, Any]:
    fields = _extract_inkbox_sms_error(exc)
    return {
        "success": False,
        "platform": "inkbox",
        "mode": "sms",
        "error": _format_inkbox_sms_error(fields),
        "fallback_allowed": False,
        **fields,
    }


def _format_inkbox_imessage_error(fields: Dict[str, Any]) -> str:
    status = fields.get("status_code")
    code = fields.get("error_code")
    message = fields.get("message") or "send_imessage failed"
    prefix = "Inkbox iMessage send failed"
    if status:
        prefix += f" (HTTP {status})"
    if code:
        prefix += f" [{code}]"
    return f"{prefix}: {message}"


def _imessage_send_failure(exc: Exception, *, target: str) -> SendResult:
    # Same structured extraction as SMS — Inkbox error envelopes share one
    # shape — but labelled as iMessage so logs and agent-visible errors
    # point at the right channel. 409s carry the recipient-first /
    # disconnected-assignment explanations from the server verbatim.
    fields = _extract_inkbox_sms_error(exc)
    logger.error(
        "[Inkbox] iMessage send failed to %s: status=%s code=%s message=%s",
        redact_phone(target),
        fields.get("status_code"),
        fields.get("error_code"),
        fields.get("message"),
    )
    return SendResult(
        success=False,
        error=_format_inkbox_imessage_error(fields),
        raw_response={"platform": "inkbox", "mode": "imessage", **fields},
        retryable=bool(fields.get("retryable")),
    )


def _extract_text_media(
    text_msg: Dict[str, Any],
    *,
    marker_label: str = "MMS",
) -> Tuple[list[str], list[str], list[str]]:
    media_items = (
        text_msg.get("media")
        or text_msg.get("attachments")
        or text_msg.get("media_items")
        or []
    )
    if isinstance(media_items, dict):
        media_items = [media_items]
    if not isinstance(media_items, list):
        media_items = [media_items]

    urls: list[str] = []
    types: list[str] = []
    markers: list[str] = []
    for item in media_items:
        if not isinstance(item, dict):
            url = _plain_value(getattr(item, "url", None) or getattr(item, "media_url", None))
            content_type = _plain_value(
                getattr(item, "content_type", None)
                or getattr(item, "mime_type", None)
                or getattr(item, "type", None)
            )
        else:
            url = _plain_value(
                item.get("url")
                or item.get("media_url")
                or item.get("download_url")
                or item.get("signed_url")
            )
            content_type = _plain_value(
                item.get("content_type")
                or item.get("mime_type")
                or item.get("type")
            )
        if url:
            urls.append(url)
        media_type = content_type or "unknown"
        types.append(media_type)
        markers.append(f"[{marker_label} attachment received: {media_type}]")
    return urls, types, markers


def _text_message_metadata(message: Any, *, mode: str) -> Dict[str, Any]:
    """Small, non-body metadata payload for SendResult.raw_response."""
    return {
        "platform": "inkbox",
        "mode": mode,
        "message_id": _plain_value(getattr(message, "id", None)),
        "delivery_status": _plain_value(getattr(message, "delivery_status", None)),
        "status": _plain_value(getattr(message, "status", None)),
        "direction": _plain_value(getattr(message, "direction", None)),
        "type": _plain_value(getattr(message, "type", None)),
    }


def _inkbox_tunnel_state_dir() -> "Path":
    """Dedicated subdir of HERMES_HOME for the SDK's tunnel state.

    The SDK writes generic-named files inside ``state_dir`` —
    ``state.json``, ``private_key.pem``, ``cert_chain.pem`` — so we keep
    them in their own subfolder to avoid colliding with other state files
    Hermes itself owns under HERMES_HOME (e.g. ``state.json`` for sessions).
    """
    from pathlib import Path  # local — keep top-of-module import surface tight
    from hermes_cli.config import get_hermes_home
    return Path(get_hermes_home()) / "inkbox_tunnel"


def _wipe_inkbox_tunnel_state(state_dir: "Path") -> None:
    """Remove the three SDK-owned files inside ``state_dir``.

    Called on every connect so a stale ``tunnel_id`` referencing a tunnel
    that's been removed server-side can never block the next start. The
    directory itself is left in place; the SDK recreates contents during
    ``connect()``.
    """
    for name in ("state.json", "private_key.pem", "cert_chain.pem"):
        path = state_dir / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.debug(
                "[Inkbox] Couldn't remove stale tunnel state file %s: %s",
                path, exc,
            )


def _slugify_for_tunnel(handle: str) -> str:
    """Convert an agent handle into a valid tunnel-name slug.

    Tunnel names must be 3-63 lowercase letters/digits/hyphens — same rules
    as a DNS label. Mirrors what the deleted ``inkbox_tunnel.derive_tunnel_name``
    helper did, kept inline now that it's a one-liner.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", (handle or "").lower()).strip("-")
    return slug[:63] if slug else "hermes-agent"


def check_inkbox_requirements() -> bool:
    """Return True iff the Python ``inkbox`` SDK and aiohttp are importable.

    The SDK ships its own tunnel runtime under ``inkbox.tunnels.client`` —
    no extra dependency beyond the inkbox extra is needed when running
    against inkboxwire.com. Operators behind their own reverse proxy /
    hosted tunnel can set ``INKBOX_PUBLIC_URL`` and skip the tunnel path
    entirely.
    """
    return INKBOX_AVAILABLE and AIOHTTP_AVAILABLE


class InkboxAdapter(BasePlatformAdapter):
    """Hermes platform adapter for Inkbox (email + SMS + voice)."""

    MAX_MESSAGE_LENGTH = 4096  # email/voice are unbounded; SMS chunked separately in send()

    def __init__(self, config: PlatformConfig):
        super().__init__(config, _inkbox_platform())
        extra = config.extra or {}

        self._api_key = (
            extra.get("api_key") or os.getenv("INKBOX_API_KEY") or ""
        ).strip()
        self._signing_key = (
            extra.get("signing_key") or os.getenv("INKBOX_SIGNING_KEY") or ""
        ).strip()
        self._identity_handle = (
            extra.get("identity") or os.getenv("INKBOX_IDENTITY") or ""
        ).strip()
        self._identity_id: Optional[str] = None
        self._identity_email_addresses: set[str] = set()
        self._identity_email_addresses_loaded = False
        # Background tasks that refresh the iMessage typing indicator while
        # the agent processes an inbound message, keyed by conversation_id.
        # Started in _start_imessage_typing, cancelled in _stop_imessage_typing
        # (on send or on failure).
        self._imessage_typing_tasks: Dict[str, "asyncio.Task"] = {}
        self._base_url = (
            extra.get("base_url") or os.getenv("INKBOX_BASE_URL") or INKBOX_BASE_URL_DEFAULT
        ).strip()
        self._host = str(extra.get("host") or os.getenv("INKBOX_HOST") or DEFAULT_HOST)
        self._port = int(
            extra.get("port") or os.getenv("INKBOX_LISTEN_PORT") or DEFAULT_PORT
        )
        self._webhook_path = str(extra.get("webhook_path") or DEFAULT_WEBHOOK_PATH)
        self._ws_path = str(extra.get("ws_path") or DEFAULT_WS_PATH)
        self._public_url_override = (
            extra.get("public_url") or os.getenv("INKBOX_PUBLIC_URL") or ""
        ).strip()
        self._tunnel_name_override = (
            extra.get("tunnel_name") or os.getenv("INKBOX_TUNNEL_NAME") or ""
        ).strip().lower()
        # Gate the start-time guard + per-webhook verify block. Defaults to
        # true so a missing INKBOX_SIGNING_KEY fails loudly instead of
        # silently accepting unsigned traffic from anyone who finds the
        # tunnel URL.
        #
        # Explicit `in extra` check (not `extra.get(...) or ...`) so that
        # a config-level boolean False isn't silently coalesced into the
        # env default — `False or "true"` evaluates to "true".
        if "require_signature" in extra:
            raw_require_signature = extra["require_signature"]
        else:
            raw_require_signature = os.getenv("INKBOX_REQUIRE_SIGNATURE", "true")
        self._require_signature = str(raw_require_signature).lower() not in ("false", "0", "no")

        # Realtime voice bridge. When an OpenAI API key is present, inbound
        # voice calls are bridged to OpenAI's Realtime API instead of relying
        # on Inkbox-side STT/TTS. See :mod:`realtime`.
        # Config shape under ``platforms.inkbox.realtime`` in config.yaml:
        #   enabled: bool (optional; defaults to auto-detect from credential)
        #   api_key: str (or read from OPENAI_API_KEY / INKBOX_REALTIME_API_KEY env)
        #   model: str (default "gpt-realtime-2")
        #   voice: str (default "cedar")
        #   additional_instructions: str
        #   consult_timeout_s: float
        self._realtime_config = _resolve_realtime_config(extra)
        self._sms_text_batch_delay_seconds = _float_setting(
            extra,
            "sms_text_batch_delay_seconds",
            "INKBOX_SMS_TEXT_BATCH_DELAY_SECONDS",
            SMS_TEXT_BATCH_DELAY_SECONDS,
        )
        self._sms_text_batch_max_messages = _int_setting(
            extra,
            "sms_text_batch_max_messages",
            "INKBOX_SMS_TEXT_BATCH_MAX_MESSAGES",
            SMS_TEXT_BATCH_MAX_MESSAGES,
        )
        self._sms_text_batch_max_chars = _int_setting(
            extra,
            "sms_text_batch_max_chars",
            "INKBOX_SMS_TEXT_BATCH_MAX_CHARS",
            SMS_TEXT_BATCH_MAX_CHARS,
        )

        # Live state.
        self._inkbox: Optional[Any] = None
        self._public_url: Optional[str] = None
        self._public_host: Optional[str] = None
        self._app: Optional[Any] = None
        self._runner: Optional[Any] = None
        self._site: Optional[Any] = None
        self._tunnel: Optional[Any] = None  # inkbox.tunnels.client.TunnelListener
        self._tunnel_runtime_thread: Optional["threading.Thread"] = None
        # contact_id → active call WebSocket. Used by send()/edit_message() to
        # push voice replies to the correct ongoing call.
        self._active_call_ws: Dict[str, Any] = {}
        # Per-WS metadata so the WS handler can rebuild the source on each turn.
        self._call_ws_meta: Dict[int, Dict[str, Any]] = {}
        # ((kind, value) → (contact_id, contact_name, expires_at)).  TTL cache
        # for inkbox.contacts.lookup() — every inbound event resolves the
        # remote party to a Contact, and the same number/email shows up
        # repeatedly within a single conversation.
        self._contact_cache: Dict[Tuple[str, str], Tuple[Optional[str], Optional[str], float]] = {}
        # Webhook dedup by ``X-Inkbox-Request-Id`` (Inkbox retries on timeout).
        self._seen_request_ids: Dict[str, float] = {}
        self._inflight_request_ids: Dict[str, float] = {}
        # session_key → pending inbound SMS fragments waiting for a quiet
        # window. Human SMS users often send fragments/corrections in bursts;
        # batching keeps one thought from becoming several self-interrupting
        # agent turns.
        self._pending_sms_text_batches: Dict[str, Dict[str, Any]] = {}
        self._pending_sms_text_batch_tasks: Dict[str, asyncio.Task] = {}
        # chat_id → metadata of the most-recent inbound email for that chat.
        # Used by send()'s email branch to populate Re: <subject> and the
        # In-Reply-To header so replies thread into the original conversation
        # in the recipient's mail client.
        self._last_inbound_email: Dict[str, Dict[str, str]] = {}
        # chat_id → metadata of the most-recent inbound SMS for that chat.
        # Used by send()'s SMS branch to reply by Inkbox conversation id
        # instead of reconstructing a phone-number send. This is required for
        # group MMS and keeps 1:1 replies on the canonical server thread.
        self._last_inbound_sms: Dict[str, Dict[str, str]] = {}
        # chat_id → metadata of the most-recent inbound iMessage for that
        # chat. iMessage rides shared Inkbox numbers, so replies MUST target
        # the conversation id (there is no agent-owned number to send from).
        self._last_inbound_imessage: Dict[str, Dict[str, str]] = {}
        # chat_id → modality of the most-recent inbound message ('email',
        # 'sms', or 'voice').  Critical when chat_id is a Contact UUID (no
        # `+` or `@` to disambiguate) — without this, send() defaults the
        # mode by chat_id shape and would email an SMS reply.
        self._last_inbound_modality: Dict[str, str] = {}
        # chat_id → unix timestamp at which the contact's call WS most-recently
        # closed.  send() consults this to drop replies generated during the
        # short window after a call ends — when the agent's last in-call turn
        # finishes generating after the WS is gone, the response would
        # otherwise fall through to the email/SMS default and leak the
        # voice-intended text into the user's inbox.
        self._voice_recently_closed: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, is_reconnect: bool = False, **kwargs) -> bool:
        if not check_inkbox_requirements():
            logger.warning(
                "[Inkbox] aiohttp or `inkbox` SDK not installed. "
                "Run: pip install 'hermes-agent[inkbox]' or `pip install inkbox aiohttp`",
            )
            return False
        if not self._api_key:
            logger.warning("[Inkbox] INKBOX_API_KEY not set")
            return False
        if not self._identity_handle:
            logger.warning("[Inkbox] INKBOX_IDENTITY not set")
            return False
        if self._require_signature and not self._signing_key:
            logger.warning(
                "[Inkbox] INKBOX_SIGNING_KEY not set and "
                "INKBOX_REQUIRE_SIGNATURE is enabled; refusing to start. "
                "Generate a signing key in https://inkbox.ai/console/signing-keys "
                "or set INKBOX_REQUIRE_SIGNATURE=false for local-only testing.",
            )
            return False

        if not self._acquire_platform_lock(
            scope="inkbox",
            identity=self._identity_handle,
            resource_desc=f"Inkbox identity '{self._identity_handle}'",
        ):
            return False

        # Check the listen port is free before trying to bind.
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", self._port))
            logger.error("[Inkbox] Port %d already in use", self._port)
            self._release_platform_lock()
            return False
        except (ConnectionRefusedError, OSError):
            pass

        try:
            self._inkbox = Inkbox(**inkbox_client_kwargs(self._api_key, self._base_url))
        except Exception as exc:
            logger.error("[Inkbox] Failed to construct SDK client: %s", exc)
            self._release_platform_lock()
            return False

        # Start the local aiohttp server FIRST. The SDK's tunnel runtime opens
        # its data-plane connection during ``tunnel_connect`` and starts
        # forwarding immediately, so the local server has to be accepting
        # before we hand inkboxwire.com a forward-to URL.
        try:
            self._app = web.Application()
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_post(self._webhook_path, self._handle_webhook)
            self._app.router.add_get(self._ws_path, self._handle_call_ws)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
        except Exception:
            logger.exception("[Inkbox] Failed to start HTTP server")
            await self._cleanup()
            self._release_platform_lock()
            return False

        # Resolve the public URL: explicit override wins, else open an
        # SDK-managed tunnel against inkboxwire.com.
        if self._public_url_override:
            self._public_url = self._public_url_override.rstrip("/")
            self._public_host = urlparse(self._public_url).netloc
        elif INKBOX_TUNNEL_AVAILABLE:
            if not await self._provision_inkbox_tunnel():
                await self._cleanup()
                self._release_platform_lock()
                return False
        else:
            logger.error(
                "[Inkbox] No public URL configured. Set INKBOX_PUBLIC_URL or "
                "install the inkbox extra: pip install 'hermes-agent[inkbox]'.",
            )
            await self._cleanup()
            self._release_platform_lock()
            return False

        # PATCH the identity's mailboxes + phone numbers to point at this server.
        try:
            await asyncio.to_thread(self._patch_identity_objects)
        except Exception:
            logger.exception("[Inkbox] Failed to register webhook receivers")
            await self._cleanup()
            self._release_platform_lock()
            return False

        self._mark_connected()
        logger.info(
            "[Inkbox] Connected: identity=%s public=%s listen=%s:%d",
            self._identity_handle, self._public_url, self._host, self._port,
        )

        return True

    async def disconnect(self) -> None:
        self._running = False
        await self._cleanup()
        self._release_platform_lock()
        self._mark_disconnected()
        logger.info("[Inkbox] Disconnected")

    async def _cleanup(self) -> None:
        for task in list(self._pending_sms_text_batch_tasks.values()):
            if not task.done():
                task.cancel()
        if self._pending_sms_text_batch_tasks:
            await asyncio.gather(
                *self._pending_sms_text_batch_tasks.values(),
                return_exceptions=True,
            )
        self._pending_sms_text_batch_tasks.clear()
        self._pending_sms_text_batches.clear()

        # Cancel any iMessage typing pulses still running.
        for task in list(self._imessage_typing_tasks.values()):
            if not task.done():
                task.cancel()
        if self._imessage_typing_tasks:
            await asyncio.gather(
                *self._imessage_typing_tasks.values(),
                return_exceptions=True,
            )
        self._imessage_typing_tasks.clear()

        # Close any live call WS so callers don't hang on a half-open socket.
        for ws in list(self._active_call_ws.values()):
            with suppress(Exception):
                await ws.close()
        self._active_call_ws.clear()
        self._call_ws_meta.clear()

        if self._site is not None:
            with suppress(Exception):
                await self._site.stop()
            self._site = None
        if self._runner is not None:
            with suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        self._app = None
        if self._tunnel is not None:
            # ``listener.close`` is sync; offload so it doesn't block the loop.
            with suppress(Exception):
                await asyncio.to_thread(self._tunnel.close)
            self._tunnel = None
        if self._tunnel_runtime_thread is not None:
            # close() unblocks wait(); join briefly so logs flush in order.
            with suppress(Exception):
                await asyncio.to_thread(
                    self._tunnel_runtime_thread.join, 5.0,
                )
            self._tunnel_runtime_thread = None
        if self._inkbox is not None:
            with suppress(Exception):
                self._inkbox.close()
            self._inkbox = None

    # ------------------------------------------------------------------
    # Bootstrap helpers
    # ------------------------------------------------------------------

    async def _provision_inkbox_tunnel(self) -> bool:
        """Open an SDK-managed tunnel to inkboxwire.com.

        ``inkbox.tunnels.client.connect`` only does the control-plane
        registration; the runtime thread that actually opens the h2 data
        plane is started lazily inside ``listener.wait()``. We don't want
        to block the gateway event loop on ``wait()``, so we spawn a small
        background thread to drive it — which gives us a live data plane
        plus a place to log any runtime error the listener captures.

        Data-plane auth uses the SDK client's ``x-api-key`` (admin-scoped
        or scoped to this tunnel's owning identity), so there is no
        connect_secret to mint, rotate, or persist on this side.

        SDK-managed state (state.json, private_key.pem, cert_chain.pem)
        lives in a dedicated subdir of HERMES_HOME so its generic filenames
        don't collide with Hermes' own state files. We wipe that subdir on
        every connect so a stale ``tunnel_id`` referencing a tunnel that's
        been removed server-side can never put us in a TunnelRemoved loop.
        """
        import threading
        from inkbox.tunnels.exceptions import TunnelNotProvisioned

        tunnel_name = self._tunnel_name_override or _slugify_for_tunnel(
            self._identity_handle,
        )
        forward_to = f"http://127.0.0.1:{self._port}"
        state_dir = _inkbox_tunnel_state_dir()

        _wipe_inkbox_tunnel_state(state_dir)

        try:
            # ``connect`` is sync (does an HTTPS round-trip + opens the data
            # plane); offload to a thread so the gateway event loop isn't
            # blocked. The returned listener owns its own supervisor threads.
            self._tunnel = await asyncio.to_thread(
                inkbox_tunnel_connect,
                self._inkbox,
                name=tunnel_name,
                forward_to=forward_to,
                state_dir=state_dir,
            )
        except TunnelNotProvisioned:
            # The identity exists but its tunnel does not (1:1 invariant
            # broken upstream, or running against an identity created
            # before the data-model migration landed). Surface a clear
            # message — recovery is to recreate the identity, which
            # atomically provisions the tunnel again.
            logger.error(
                "[Inkbox] No tunnel provisioned for handle %r — recreate "
                "the identity via the Inkbox console or `inkbox identity "
                "create`, then restart the gateway.",
                tunnel_name,
            )
            self._tunnel = None
            return False
        except Exception:
            logger.exception("[Inkbox] Failed to open SDK tunnel")
            self._tunnel = None
            return False

        # Drive the listener's runtime in a daemon thread. ``wait()`` calls
        # ``_start_thread_if_needed()`` internally — that's what actually
        # spawns the data-plane runtime thread. Without this, ``connect()``
        # returns a listener whose runtime never starts and inkboxwire.com
        # gets a "no agent connected" 503 for every inbound webhook.
        def _drive_listener(listener):
            try:
                listener.wait()
            except KeyboardInterrupt:
                pass
            except Exception:
                logger.exception(
                    "[Inkbox] Tunnel runtime exited with error",
                )

        self._tunnel_runtime_thread = threading.Thread(
            target=_drive_listener,
            args=(self._tunnel,),
            name="inkbox-tunnel-wait",
            daemon=True,
        )
        self._tunnel_runtime_thread.start()

        self._public_url = self._tunnel.public_url.rstrip("/")
        self._public_host = self._tunnel.tunnel.public_host
        logger.info(
            "[Inkbox] Tunnel ready: %s → 127.0.0.1:%d",
            self._public_url, self._port,
        )
        return True

    def _patch_identity_objects(self) -> None:
        """Point every mailbox + phone number on the identity at this server."""
        webhook_url = f"{self._public_url}{self._webhook_path}"
        ws_url = f"wss://{self._public_host}{self._ws_path}"

        # Snapshot the prior webhook URL before we overwrite state so the
        # reconcile helpers can delete exactly the row we installed last time.
        previous_webhook_url = _read_previous_webhook_url()

        identity = self._inkbox.get_identity(self._identity_handle)
        self._identity_id = str(getattr(identity, "id", "") or "") or None
        self._identity_email_addresses = _identity_email_addresses(identity)
        self._identity_email_addresses_loaded = True

        # Mailbox: register the inbound-mail subscription.
        if identity.mailbox is not None:
            _reconcile_mail_subscription(
                self._inkbox,
                identity.mailbox.id,
                desired_url=webhook_url,
                previous_webhook_url=previous_webhook_url,
                desired_events=_DESIRED_MAIL_EVENTS,
            )
            logger.info(
                "[Inkbox] Patched mailbox %s → %s",
                identity.mailbox.email_address, webhook_url,
            )

        # Phone number: text subscription + call channel on the resource.
        # ``incoming_call_action="auto_accept"`` tells Inkbox to pick up the
        # call itself and immediately open a WS to ``client_websocket_url``,
        # without round-tripping a webhook first.  Lower setup latency than
        # ``webhook`` mode, and the call context arrives on the WS itself
        # via the ``x-call-context`` header (parsed in ``_handle_call_ws``).
        if identity.phone_number is not None:
            _reconcile_text_subscription(
                self._inkbox,
                identity.phone_number.id,
                desired_url=webhook_url,
                previous_webhook_url=previous_webhook_url,
                desired_events=_DESIRED_TEXT_EVENTS,
            )
            self._inkbox.phone_numbers.update(
                identity.phone_number.id,
                incoming_call_webhook_url=webhook_url,
                incoming_call_action="auto_accept",
                client_websocket_url=ws_url,
            )
            logger.info(
                "[Inkbox] Patched phone %s → %s + %s",
                identity.phone_number.number, webhook_url, ws_url,
            )

        # iMessage: identity-owned subscription, only while the identity is
        # enabled (the server rejects imessage.* subscriptions otherwise).
        if getattr(identity, "imessage_enabled", False) and self._identity_id:
            _reconcile_imessage_subscription(
                self._inkbox,
                self._identity_id,
                desired_url=webhook_url,
                previous_webhook_url=previous_webhook_url,
                desired_events=_DESIRED_IMESSAGE_EVENTS,
            )
            logger.info(
                "[Inkbox] Patched iMessage for identity %s → %s",
                self._identity_handle, webhook_url,
            )

        # Persist the resolved identity so non-Inkbox sessions (CLI, etc.) can
        # tell the agent which email + phone it can be reached on.  Read by
        # ``prompt_builder.build_inkbox_identity_hint``.
        self._write_identity_state(identity, webhook_url, ws_url)

    def _write_identity_state(self, identity, webhook_url: str, ws_url: str) -> None:
        # Atomic write (tmp + os.replace) so a concurrent reader — e.g.
        # ``prompt_builder.build_inkbox_identity_hint`` running in another
        # process at agent start — can never observe a half-written file.
        # Matches the pattern used by feishu_comment_rules._save_pairing and
        # the google_chat adapter's thread-count store.
        try:
            state_path = _inkbox_state_path()
            state = {
                "handle": self._identity_handle,
                "email_address": (
                    getattr(identity.mailbox, "email_address", None)
                    if identity.mailbox else None
                ),
                "phone_number": (
                    getattr(identity.phone_number, "number", None)
                    if identity.phone_number else None
                ),
                "phone_number_id": (
                    str(getattr(identity.phone_number, "id", ""))
                    if identity.phone_number else None
                ),
                "imessage_enabled": bool(getattr(identity, "imessage_enabled", False)),
                "public_url": self._public_url,
                "webhook_url": webhook_url,
                "ws_url": ws_url,
            }
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(state, indent=2) + "\n")
            os.replace(tmp_path, state_path)
        except Exception as exc:
            logger.debug("[Inkbox] Failed to write identity state file: %s", exc)

    # ------------------------------------------------------------------
    # Outbound: send / edit / get_chat_info
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Dispatch a message via the right Inkbox modality.

        ``metadata['mode']`` selects the channel: ``email`` (default for
        contacts that have an email on file), ``sms``, ``imessage``, or
        ``voice``. For voice mode the message is pushed onto the contact's
        active call WebSocket — the caller hears it through Inkbox-managed
        TTS.

        Hermes admin / status banners (session-reset notices, runtime info,
        home-channel prompt, update notifications) are silently dropped —
        these are CLI chatter that never belongs in a real user's email or
        SMS thread.  See ``_is_hermes_admin_notice`` for the prefix list.
        """
        if _is_hermes_admin_notice(content, metadata):
            logger.debug(
                "[Inkbox] Suppressed admin notice for chat %s: %s…",
                chat_id, (content or "")[:60].replace("\n", " "),
            )
            self._stop_imessage_typing_for_chat(chat_id)
            return SendResult(success=True, message_id="suppressed-admin-notice")

        # The [SILENT] marker is the cron scheduler's "I have nothing to
        # say" sentinel and is also instructed to the agent in the
        # post-call synthetic [call_ended] turn (see _handle_call_ws).
        # If the agent emits it through any send() path — including a
        # send_message tool call that picks email/SMS as the channel —
        # drop the send entirely. A bare [SILENT] email is never a
        # message a human wants to receive.
        if (content or "").strip().upper() == "[SILENT]":
            logger.info(
                "[Inkbox] Suppressed [SILENT] sentinel for chat %s",
                chat_id,
            )
            self._stop_imessage_typing_for_chat(chat_id)
            return SendResult(success=True, message_id="suppressed-silent-marker")

        meta = metadata or {}
        mode = (meta.get("mode") or "").lower().strip()

        # End-of-call grace window: when a voice call ends, the agent's last
        # in-flight turn often finishes generating *after* the WS has closed.
        # Without this guard the response falls through to the email/SMS
        # default and leaks the voice-intended text into the user's inbox.
        #
        # Drop when ALL of:
        #   - call WS just closed for this chat within VOICE_GRACE_SECONDS
        #   - no active call WS now (so we cannot ride the WS)
        #   - no fresh non-voice inbound has arrived since the close (which
        #     would have repopulated ``_last_inbound_modality`` and made this
        #     a legitimate SMS/email reply, not stale voice content).
        #
        # Note: we intentionally suppress regardless of whether the caller
        # passed an explicit ``mode``. An explicit ``mode='email'`` from a
        # post-call send_message tool call is exactly the case that bit us
        # (the agent's "reflect on the call" reply leaked out as an email).
        VOICE_GRACE_SECONDS = 60
        closed_at = self._voice_recently_closed.get(str(chat_id))
        if (
            closed_at is not None
            and (time.time() - closed_at) < VOICE_GRACE_SECONDS
            and chat_id not in self._active_call_ws
            and not self._last_inbound_modality.get(str(chat_id))
        ):
            logger.info(
                "[Inkbox] Suppressed post-call voice-leakage for chat %s: %s…",
                chat_id, (content or "")[:60].replace("\n", " "),
            )
            return SendResult(success=True, message_id="suppressed-post-call-leak")
        # Garbage-collect stale entries so the dict doesn't grow unbounded.
        if closed_at is not None and (time.time() - closed_at) > VOICE_GRACE_SECONDS:
            self._voice_recently_closed.pop(str(chat_id), None)

        # Resolve mode if the gateway didn't pass one explicitly.  Order of
        # preference:
        #   1. An open live-call WebSocket on this chat — voice trumps
        #      everything because dropping it would leave the caller hearing
        #      silence while we send an email.
        #   2. The modality of the most-recent inbound from this chat —
        #      SMS-conversations on contact-UUID chat_ids land here (the
        #      chat_id shape doesn't reveal which channel inbound came in
        #      on).
        #   3. SMS if the chat target itself looks like an E.164 number.
        #   4. Email otherwise (contact UUIDs, raw email addresses).
        if not mode and chat_id in self._active_call_ws:
            mode = "voice"
        if not mode:
            mode = self._last_inbound_modality.get(str(chat_id), "")
        if not mode:
            mode = "sms" if str(chat_id).startswith("+") else "email"

        if mode == "sms" and len(content or "") > SMS_MAX_LENGTH:
            return _sms_too_long_failure(content)
        if mode == "imessage" and len(content or "") > IMESSAGE_MAX_LENGTH:
            return _imessage_too_long_failure(content)

        # Voice replies ride the per-call WebSocket the WS handler keeps
        # open for the duration of the call.  No SDK round-trip.
        if mode == "voice":
            ws = self._active_call_ws.get(chat_id)
            if ws is None:
                return SendResult(
                    success=False,
                    error=(
                        f"No active call WebSocket for chat_id={chat_id}. "
                        "Voice replies require an open call."
                    ),
                )
            turn_id = str(meta.get("turn_id") or "")
            try:
                # Two-frame protocol matching the legacy phone-bridge: a
                # delta carrying the text, then a final ``done: true`` frame
                # that flushes Inkbox's TTS and ends the turn.
                await ws.send_str(json.dumps(
                    {"event": "text", "delta": content, "turn_id": turn_id}
                ))
                await ws.send_str(json.dumps(
                    {"event": "text", "done": True, "turn_id": turn_id}
                ))
                return SendResult(success=True)
            except Exception as exc:
                return SendResult(success=False, error=str(exc), retryable=True)

        if self._inkbox is None:
            return SendResult(success=False, error="Inkbox SDK client not initialized")

        try:
            identity = await asyncio.to_thread(
                self._inkbox.get_identity, self._identity_handle,
            )
        except Exception as exc:
            return SendResult(success=False, error=f"get_identity failed: {exc}")

        if mode == "imessage":
            thread_id = str(meta.get("thread_id") or "").strip()
            imessage_meta = (
                self._last_inbound_imessage.get(_sms_state_key(chat_id, thread_id))
                or self._last_inbound_imessage.get(str(chat_id), {})
            )
            conversation_id = str(
                meta.get("conversation_id")
                or meta.get("conversationId")
                or _imessage_conversation_target(thread_id)
                or imessage_meta.get("conversation_id")
                or ""
            ).strip()
            to_number = str(imessage_meta.get("remote_number") or "").strip()
            if not conversation_id and not to_number:
                if str(chat_id).startswith("+"):
                    to_number = str(chat_id).strip()
                else:
                    to_number = await asyncio.to_thread(
                        self._lookup_contact_phone, chat_id,
                    ) or ""
            if not conversation_id and not to_number:
                return SendResult(
                    success=False,
                    error=f"No iMessage conversation or phone number for chat {chat_id}",
                )
            # The reply is going out now — stop the typing pulse for this
            # conversation regardless of how the send below resolves.
            self._stop_imessage_typing(conversation_id)
            send_imessage = getattr(identity, "send_imessage", None)
            if not callable(send_imessage):
                return SendResult(
                    success=False,
                    error=(
                        "Installed Inkbox SDK has no send_imessage; "
                        "upgrade with: pip install -U inkbox"
                    ),
                )
            try:
                if conversation_id:
                    msg = await asyncio.to_thread(
                        send_imessage,
                        conversation_id=conversation_id,
                        text=content,
                    )
                    target_label = f"conversation:{conversation_id}"
                else:
                    msg = await asyncio.to_thread(
                        send_imessage, to=to_number, text=content,
                    )
                    target_label = redact_phone(to_number)
                raw_response = _text_message_metadata(msg, mode="imessage")
                logger.info(
                    "[Inkbox] iMessage queued to %s: id=%s status=%s",
                    target_label,
                    raw_response.get("message_id") or "",
                    raw_response.get("status") or "",
                )
                return SendResult(
                    success=True,
                    message_id=str(getattr(msg, "id", "")),
                    raw_response=raw_response,
                )
            except Exception as exc:
                return _imessage_send_failure(
                    exc, target=conversation_id or to_number,
                )

        if mode == "sms":
            thread_id = str(meta.get("thread_id") or "").strip()
            sms_meta = (
                self._last_inbound_sms.get(_sms_state_key(chat_id, thread_id))
                or self._last_inbound_sms.get(str(chat_id), {})
            )
            conversation_id = str(
                meta.get("conversation_id")
                or meta.get("conversationId")
                or _sms_conversation_target(thread_id)
                or sms_meta.get("conversation_id")
                or ""
            ).strip()
            to_number = str(
                meta.get("to_phone")
                or meta.get("toPhone")
                or sms_meta.get("remote_phone_number")
                or chat_id
            ).strip()
            if not conversation_id and not to_number.startswith("+"):
                # chat_id is a contact UUID (or unknown shape) — look up the
                # primary phone number on the contact record.
                to_number = await asyncio.to_thread(self._lookup_contact_phone, chat_id)
                if not to_number:
                    return SendResult(
                        success=False,
                        error=f"No phone number on contact {chat_id}",
                    )
            try:
                if conversation_id:
                    try:
                        msg = await asyncio.to_thread(
                            identity.send_text,
                            conversation_id=conversation_id,
                            text=content,
                        )
                    except TypeError:
                        msg = await asyncio.to_thread(
                            identity.send_text,
                            {"conversationId": conversation_id, "text": content},
                        )
                    target_label = f"conversation:{conversation_id}"
                else:
                    msg = await asyncio.to_thread(identity.send_text, to=to_number, text=content)
                    target_label = to_number
                raw_response = _text_message_metadata(msg, mode="sms")
                logger.info(
                    "[Inkbox] SMS queued to %s: id=%s delivery_status=%s",
                    target_label if conversation_id else redact_phone(to_number),
                    raw_response.get("message_id") or "",
                    raw_response.get("delivery_status") or raw_response.get("status") or "",
                )
                return SendResult(
                    success=True,
                    message_id=str(getattr(msg, "id", "")),
                    raw_response=raw_response,
                )
            except Exception as exc:
                return _sms_send_failure(exc, to_number=conversation_id or to_number)

        if mode == "email":
            stash = self._last_inbound_email.get(str(chat_id), {})
            to_addr = (meta.get("to_email") or stash.get("from_address") or "").strip()
            if not to_addr:
                # If the chat_id already looks like an email address, use it
                # directly — this is the unknown-sender path where the
                # contact lookup at ingest returned 0 matches and the raw
                # email became the chat_id.  Only try contacts.get() when the
                # chat_id is a contact UUID we can actually fetch.
                if "@" in str(chat_id):
                    to_addr = str(chat_id).strip()
                else:
                    to_addr = await asyncio.to_thread(self._lookup_contact_email, chat_id)
            if not to_addr:
                return SendResult(
                    success=False,
                    error=f"No email address on contact {chat_id}",
                )

            # Threading: prefer the inbound RFC 5322 Message-ID we stashed in
            # _on_mail_received, fall back to whatever the gateway passed in
            # as ``reply_to``.  Subject defaults to ``Re: <inbound subject>``
            # when replying to a known thread, ``(no subject)`` when sending
            # cold.  Mail clients use both signals (header + subject) to group
            # the message into the original conversation.
            in_reply_to = (
                meta.get("in_reply_to_message_id")
                or reply_to
                or stash.get("rfc_message_id")
                or None
            )
            inbound_subject = stash.get("subject", "")
            if meta.get("subject"):
                subject = str(meta["subject"])
            elif inbound_subject:
                # Don't double-prefix if the agent's reply target already had Re:.
                if inbound_subject.lower().startswith("re:"):
                    subject = inbound_subject
                else:
                    subject = f"Re: {inbound_subject}"
            else:
                subject = "(no subject)"

            try:
                msg = await asyncio.to_thread(
                    identity.send_email,
                    to=[to_addr],
                    subject=subject,
                    body_text=content,
                    in_reply_to_message_id=in_reply_to or None,
                )
                return SendResult(success=True, message_id=str(getattr(msg, "id", "")))
            except Exception as exc:
                return SendResult(success=False, error=f"send_email failed: {exc}")

        return SendResult(success=False, error=f"Unknown Inkbox send mode: {mode!r}")

    def supports_progress_updates(self, chat_id: str) -> bool:
        """Return True when tool-progress bubbles are useful on this chat.

        Args:
            chat_id: The chat the gateway is about to dispatch progress to —
                an active-call contact UUID, an inbound SMS E.164 number, or
                an email-tied contact UUID / address.

        Returns:
            True when the gateway should attempt to render tool-progress
            bubbles for this chat, False to skip them entirely.

        Voice calls opt out: tool names and argument previews are UI
        chrome, not speech.  SMS gets a single batched bubble per turn
        (the gateway's edit-failure handler drops the rest).  Email chats
        opt out entirely — sending a separate email per tool call
        ("🖥️ browser_console...") is a UX disaster, and the agent's final
        reply still lands as one email at turn end.
        """
        # Active voice call -> keep tool-progress UI out of TTS.
        if chat_id in self._active_call_ws:
            return False
        modality = self._last_inbound_modality.get(str(chat_id), "")
        if modality == "email":
            return False
        if modality in ("sms", "imessage"):
            return True
        # Unknown modality (agent-initiated outbound, contact-keyed chat
        # we haven't seen inbound yet) — mirror send()'s default heuristic:
        # E.164 → SMS, otherwise email.  Treat email as opt-out.
        return str(chat_id).startswith("+")

    def supports_interim_messages(self, chat_id: str) -> bool:
        """Return True when interim assistant messages should fire on this chat.

        Args:
            chat_id: The chat the gateway is about to dispatch an interim
                assistant message to.

        Returns:
            True when the gateway should attempt to render mid-turn
            assistant status (e.g. "Let me check on that…"), False to
            suppress these updates entirely for this chat.

        Voice calls stream interim status as TTS deltas — essentially
        free.  SMS users benefit from periodic "I'm still working on
        it" pings (it's a slow channel and silence is worse than an
        extra short text).  Email is the opt-out: one mid-turn email
        per ``status_callback`` is unsendable UX, and the user gets
        the final answer in their inbox at turn end anyway.
        """
        # Active voice call → interim status is streamed as TTS deltas.
        if chat_id in self._active_call_ws:
            return True
        modality = self._last_inbound_modality.get(str(chat_id), "")
        if modality == "email":
            return False
        if modality in ("sms", "imessage"):
            return True
        # Unknown modality — mirror send()'s E.164-or-email heuristic
        # (matches ``supports_progress_updates``): E.164-shaped chat_id
        # → treat as SMS and allow, anything else → email and suppress.
        return str(chat_id).startswith("+")

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Stream incremental deltas to an open call. No-op for mail/SMS."""
        ws = self._active_call_ws.get(chat_id)
        if ws is None:
            return SendResult(success=False, error="Not supported")
        # Same admin-notice guard as send() — runtime banners are even more
        # offensive when read aloud over a live call than when delivered as
        # text to email/SMS.
        if _is_hermes_admin_notice(content, metadata):
            return SendResult(success=True, message_id="suppressed-admin-notice")
        try:
            # Match the bridge's two-frame protocol — Inkbox's TTS pipeline
            # mixes ``delta`` and ``done`` into separate frames rather than
            # one combined message.
            if content:
                await ws.send_str(json.dumps(
                    {"event": "text", "delta": content, "turn_id": message_id}
                ))
            if finalize:
                await ws.send_str(json.dumps(
                    {"event": "text", "done": True, "turn_id": message_id}
                ))
            return SendResult(success=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return ``{name, type, chat_id}`` for a contact-keyed chat."""
        info = {"name": chat_id, "type": "dm", "chat_id": chat_id}
        if self._inkbox is None:
            return info
        try:
            contact = await asyncio.to_thread(self._inkbox.contacts.get, chat_id)
        except Exception:
            return info
        info["name"] = (
            getattr(contact, "preferred_name", None)
            or getattr(contact, "given_name", None)
            or chat_id
        )
        return info

    # ------------------------------------------------------------------
    # Inbound: webhook handler
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({
            "status": "ok",
            "platform": "inkbox",
            "identity": self._identity_handle,
            "public_url": self._public_url,
        })

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        body = await request.read()
        # Verify HMAC over the raw body before doing anything with it — both
        # public-URL and tunnel-delivered traffic hits this handler, so this
        # is the one chokepoint where we can refuse spoofed requests.
        if self._require_signature:
            ok = verify_webhook(
                payload=body,
                headers=dict(request.headers),
                secret=self._signing_key,
            )
            if not ok:
                return web.Response(status=401, text="invalid signature")

        request_id = request.headers.get("X-Inkbox-Request-Id", "")
        if request_id and self._dedup_begin(request_id):
            return web.Response(status=200, text="duplicate")

        try:
            envelope = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._dedup_rollback(request_id)
            return web.Response(status=400, text="invalid json")

        try:
            # Mail / SMS webhooks come wrapped in {event_type, data:{...}}; the
            # incoming-call webhook is delivered as a flat object with a
            # ``phone_number_id`` field at the top level (no envelope).
            event_type = envelope.get("event_type")
            if event_type == "message.received":
                response = await self._on_mail_received(envelope)
            elif event_type and event_type.startswith("message."):
                # Outbound mail lifecycle (sent/delivered/bounced/failed) — log only.
                response = web.Response(status=200, text="ok")
            elif event_type == "text.received":
                response = await self._on_text_received(envelope)
            elif event_type and event_type.startswith("text."):
                response = await self._on_text_lifecycle(envelope)
            elif event_type == "imessage.received":
                response = await self._on_imessage_received(envelope)
            elif event_type == "imessage.reaction_received":
                response = await self._on_imessage_reaction(envelope)
            elif event_type and event_type.startswith("imessage."):
                response = await self._on_imessage_lifecycle(envelope)
            elif "phone_number_id" in envelope and "remote_phone_number" in envelope:
                response = await self._on_incoming_call(envelope)
            else:
                # Catch-all: anything that is NOT a known Inkbox event
                # (mail/text/imessage/call above) is treated as an
                # externally-injected event (e.g. a GitHub Actions failure
                # forwarded through the tunnel) with no Inkbox contact behind
                # it — wake the agent on a fresh thread to decide what to do.
                response = await self._on_external_event(envelope, request_id)
        except Exception:
            self._dedup_rollback(request_id)
            raise
        self._dedup_commit(request_id)
        return response

    def _prune_dedup_ids(self) -> None:
        if not hasattr(self, "_seen_request_ids"):
            self._seen_request_ids = {}
        if not hasattr(self, "_inflight_request_ids"):
            self._inflight_request_ids = {}
        now = time.time()
        # Prune expired entries opportunistically.
        for store in (self._seen_request_ids, self._inflight_request_ids):
            for rid, ts in list(store.items()):
                if now - ts >= WEBHOOK_DEDUP_TTL_SECONDS:
                    store.pop(rid, None)
        if len(self._seen_request_ids) > 2000:
            oldest = sorted(self._seen_request_ids.items(), key=lambda item: item[1])
            for rid, _ts in oldest[: len(self._seen_request_ids) - 2000]:
                self._seen_request_ids.pop(rid, None)

    def _dedup_begin(self, request_id: str) -> bool:
        if not request_id:
            return False
        self._prune_dedup_ids()
        prior = self._seen_request_ids.get(request_id)
        if prior is not None:
            return True
        if request_id in self._inflight_request_ids:
            return True
        self._inflight_request_ids[request_id] = time.time()
        return False

    def _dedup_commit(self, request_id: str) -> None:
        if not request_id:
            return
        self._prune_dedup_ids()
        self._inflight_request_ids.pop(request_id, None)
        self._seen_request_ids[request_id] = time.time()

    def _dedup_rollback(self, request_id: str) -> None:
        if request_id:
            self._inflight_request_ids.pop(request_id, None)

    def _is_duplicate(self, request_id: str) -> bool:
        if self._dedup_begin(request_id):
            return True
        self._dedup_commit(request_id)
        return False

    async def _is_self_mail_received(
        self,
        envelope: Dict[str, Any],
        from_address: str,
    ) -> bool:
        if _mail_agent_identity_matches(
            envelope,
            from_address,
            identity_handle=self._identity_handle,
            identity_id=self._identity_id or "",
        ):
            return True

        if getattr(self, "_identity_email_addresses_loaded", False):
            return from_address in self._identity_email_addresses

        if self._inkbox is None or not self._identity_handle:
            return False

        try:
            identity = await asyncio.to_thread(
                self._inkbox.get_identity,
                self._identity_handle,
            )
        except Exception as exc:
            logger.debug("[Inkbox] Could not resolve identity for self-mail check: %s", exc)
            return False

        self._identity_id = str(getattr(identity, "id", "") or "") or None
        self._identity_email_addresses = _identity_email_addresses(identity)
        self._identity_email_addresses_loaded = True
        if _mail_agent_identity_matches(
            envelope,
            from_address,
            identity_handle=self._identity_handle,
            identity_id=self._identity_id or "",
        ):
            return True
        return from_address in self._identity_email_addresses

    async def _on_mail_received(self, envelope: Dict[str, Any]) -> "web.Response":
        message = (envelope.get("data") or {}).get("message") or {}
        from_address = _normalize_email_address(message.get("from_address"))
        if not from_address:
            return web.Response(status=200, text="ok")

        if await self._is_self_mail_received(envelope, from_address):
            logger.info(
                "[Inkbox] Ignored self-originated inbound email from %s; not waking agent",
                from_address,
            )
            return web.Response(status=200, text="ok")

        thread_id = message.get("thread_id")
        contact = await self._resolve_contact_full(kind="email", value=from_address)
        chat_id = _chat_id_for_route(
            contact,
            _channel_thread_key("email", thread_id),
            from_address,
        )
        contact_name = contact["name"] if contact and contact.get("name") else None
        rfc_message_id = message.get("message_id")  # RFC 5322 Message-ID for threading
        subject = message.get("subject") or ""

        # Stash the subject + RFC 5322 Message-ID so send() can populate
        # Re: <subject> and the In-Reply-To header on replies.  Keyed by
        # chat_id so unsolicited cron sends to the same chat fall back to
        # the most-recent inbound for threading context.
        self._last_inbound_email[str(chat_id)] = {
            "subject": subject,
            "rfc_message_id": rfc_message_id or "",
            "from_address": from_address,
        }
        self._last_inbound_modality[str(chat_id)] = "email"

        source = self.build_source(
            chat_id=str(chat_id),
            chat_name=contact_name or from_address,
            chat_type="dm",
            user_id=str(chat_id),
            user_name=contact_name or from_address,
            user_id_alt=from_address,
            thread_id=f"email:{thread_id}" if thread_id else None,
            chat_topic=subject or None,
            # MessageEvent.message_id is what the gateway passes back as
            # ``reply_to`` on send().  Use the RFC 5322 Message-ID (not the
            # Inkbox UUID) so SDK send_email(in_reply_to_message_id=...)
            # actually threads the reply.
            message_id=rfc_message_id or message.get("id"),
        )
        body_text = message.get("snippet") or subject or ""
        # Modality marker — every inbound is prefixed with one line that
        # tells the agent which modality + which Inkbox Contact (if any)
        # this message belongs to.  PLATFORM_HINTS["inkbox"] explains how
        # the agent should use this and tells it never to echo the line.
        contact_block = self._contact_marker(contact)
        tagged = (
            f"[inkbox:email from={from_address}"
            f"{f' subject={subject!r}' if subject else ''}"
            f" | {contact_block}]\n{body_text}"
        )
        # Built-in default plus any operator-configured email overrides
        # (system prompt and/or extra skills) for this contact.
        channel_prompt, auto_skill = self._resolve_channel_overrides(
            "email", chat_id, "inkbox:inkbox-troubleshooting"
        )
        event = MessageEvent(
            text=tagged,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=envelope,
            message_id=rfc_message_id or str(message.get("id") or ""),
            channel_prompt=channel_prompt,
            auto_skill=auto_skill,
        )
        await self._enqueue(event)
        return web.Response(status=200, text="ok")

    async def _on_external_event(
        self,
        envelope: Dict[str, Any],
        request_id: str = "",
    ) -> "web.Response":
        """Wake the agent on a fresh thread for an externally-injected event.

        This is the catch-all path: any inbound webhook whose type is not a
        known Inkbox event (mail/text/imessage/call) lands here.  External
        systems (e.g. a GitHub Actions workflow) have no Inkbox contact behind
        them and use their own ad-hoc JSON schema, so we read whatever common
        fields are present, surface the whole payload, and enqueue an
        ``internal`` MessageEvent on a unique ``thread_id`` — a new Hermes
        session per event — for the agent to act on.

        Args:
            envelope (Dict[str, Any]): Parsed webhook body.  No fixed schema;
                fields are read from the top level and from a ``data`` wrapper
                if present (``event``/``event_type``, ``title``, ``summary``/
                ``body``, ``severity``, ``environment``, ``requested_action``,
                ``url``/``run_url``, ``source``, optional ``id``, and a
                ``github`` context block).
            request_id (str): The ``X-Inkbox-Request-Id``, used as the
                thread/event key when the payload carries no id of its own.

        Returns:
            web.Response: 200 once the event is enqueued for the agent.
        """
        # Some senders wrap fields under "data"; the GitHub demo sends a flat
        # object. Read the top level first, then fall back to the data wrapper.
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        github = envelope.get("github") if isinstance(envelope.get("github"), dict) else {}

        def _field(*names: str) -> str:
            """First non-empty value for any of ``names`` across envelope/data."""
            for name in names:
                for scope in (envelope, data):
                    value = scope.get(name)
                    if value not in (None, ""):
                        return str(value).strip()
            return ""

        # Event name + where it came from (repo for GitHub, else any "source").
        event_name = _field("event_type", "event") or "external"
        source_name = (
            _field("source") or str(github.get("repository") or "").strip() or "external"
        )
        title = _field("title")
        body = _field("summary", "body", "message", "description")
        severity = _field("severity")
        # Free-form deployment environment (prod/beta/dev) the agent uses to
        # decide how loudly to react; passed through verbatim.
        environment = _field("environment", "env")
        requested_action = _field("requested_action", "action")
        url = _field("url", "run_url", "link") or str(github.get("run_url") or "").strip()

        # A stable per-event key keeps each event on its own thread: prefer an
        # explicit id (payload id or GitHub run id), fall back to the webhook
        # request id, and finally hash the payload so events never collide.
        event_key = _field("id") or str(github.get("run_id") or "").strip() or request_id
        if not event_key:
            event_key = hashlib.sha256(
                json.dumps(envelope, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]

        # New thread per event so the agent wakes into a clean session, grouped
        # under one chat_id per source for continuity across that source.
        chat_id = f"external:{source_name}"
        thread_id = f"external:{source_name}:{event_key}"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=f"{source_name} events",
            chat_type="dm",
            user_id=chat_id,
            user_name=source_name,
            thread_id=thread_id,
            chat_topic=title or event_name or None,
            message_id=event_key,
        )

        # Routing marker mirrors the inbound-modality convention so the agent
        # knows this is an external event (and its source/env/severity).
        marker_bits = [f"source={source_name}", f"event={event_name}"]
        if environment:
            marker_bits.append(f"environment={environment}")
        if severity:
            marker_bits.append(f"severity={severity}")
        marker = f"[inkbox:external {' '.join(marker_bits)}]"
        # Body the agent reads: recognized fields first, then the raw payload so
        # the agent has every detail regardless of the sender's schema.
        parts = [marker]
        if title:
            parts.append(title)
        if body:
            parts.append(body)
        if requested_action:
            parts.append(f"Requested action: {requested_action}")
        if url:
            parts.append(f"Link: {url}")
        parts.append("")
        parts.append("Raw event payload:")
        parts.append(json.dumps(envelope, indent=2, default=str)[:4000])
        text = "\n".join(parts)

        # Per-source operator overrides (system prompt and/or skills) — this is
        # the seam where the "what to do on this event" playbook is attached.
        channel_prompt, auto_skill = self._resolve_channel_overrides(
            "external", chat_id, None
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=envelope,
            message_id=event_key,
            channel_prompt=channel_prompt,
            auto_skill=auto_skill,
            internal=True,  # no Inkbox contact behind this — bypass user auth
        )
        await self._enqueue(event)
        return web.Response(status=200, text="ok")

    def _resolve_channel_overrides(
        self,
        modality: str,
        chat_id: Any,
        default_skills: "str | list[str] | None",
    ) -> "tuple[Optional[str], str | list[str] | None]":
        """Resolve the per-channel system prompt and auto-loaded skills for an event.

        Operators tailor how this agent behaves on each Inkbox channel with two
        optional ``inkbox:`` config blocks — ``channel_prompts`` (an ephemeral
        system prompt) and ``channel_skill_bindings`` (extra skills to load on a
        new session). Both are keyed by either a modality or a specific Inkbox
        contact id, contact id winning. Configured skills are merged on top of
        the channel's built-in defaults so a binding never drops them.

        Args:
            modality (str): Inkbox channel for this event — ``email``, ``sms``,
                ``imessage``, or ``voice``. The broad lookup key.
            chat_id (Any): Inkbox contact id (or raw address) for this event.
                The fine-grained lookup key, preferred over the modality.
            default_skills (str | list[str] | None): Skills the channel always
                auto-loads, before operator overrides are merged in.

        Returns:
            tuple[Optional[str], str | list[str] | None]: The resolved channel
                prompt (or ``None``) and the merged ``auto_skill`` value.
        """
        config = getattr(self, "config", None)
        extra = getattr(config, "extra", None) or {}
        contact_key = str(chat_id or "")
        prompt = self._lookup_channel_prompt(extra, contact_key, modality)
        configured = self._lookup_channel_skills(extra, contact_key, modality)
        return prompt, self._merge_auto_skills(default_skills, configured)

    @staticmethod
    def _lookup_channel_prompt(
        extra: Dict[str, Any], contact_key: str, modality: str
    ) -> Optional[str]:
        """Return the operator channel prompt for this contact/modality, else None."""
        prompts = extra.get("channel_prompts")
        if not isinstance(prompts, dict):
            return None
        # Contact-specific prompt wins over the modality-wide default.
        for key in (contact_key, modality):
            if not key:
                continue
            value = prompts.get(key)
            if value is None:
                continue
            value = str(value).strip()
            if value:
                return value
        return None

    @staticmethod
    def _lookup_channel_skills(
        extra: Dict[str, Any], contact_key: str, modality: str
    ) -> "str | list[str] | None":
        """Return operator-bound skills for this contact/modality, else None."""
        bindings = extra.get("channel_skill_bindings")
        if not isinstance(bindings, list):
            return None
        # Contact-specific binding wins over the modality-wide one.
        for key in (contact_key, modality):
            if not key:
                continue
            for entry in bindings:
                if not isinstance(entry, dict) or str(entry.get("id") or "") != key:
                    continue
                skills = entry.get("skills")
                if skills is None:
                    skills = entry.get("skill")  # single-name shorthand
                if skills:
                    return skills
        return None

    @staticmethod
    def _merge_auto_skills(default, configured):
        """Union built-in defaults with configured skills, defaults first, deduped."""
        merged: list = []
        for group in (default, configured):
            if not group:
                continue
            for name in [group] if isinstance(group, str) else group:
                if name and name not in merged:
                    merged.append(name)
        return merged or None

    def _sms_text_batch_key(self, event: MessageEvent) -> str:
        from gateway.session import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    @staticmethod
    def _sms_text_batch_chars(batch: Dict[str, Any]) -> int:
        return sum(len(str(fragment.get("text") or "")) for fragment in batch["fragments"])

    async def _lookup_text_conversation_summary(self, conversation_id: str) -> Any:
        if not conversation_id or self._inkbox is None:
            return None

        def _lookup():
            identity = self._inkbox.get_identity(self._identity_handle)
            method = getattr(identity, "list_text_conversations", None)
            if callable(method):
                try:
                    convos = method(limit=200, offset=0, include_groups=True)
                except TypeError:
                    convos = method({"limit": 200, "offset": 0, "includeGroups": True})
            else:
                method = getattr(identity, "listTextConversations", None)
                if not callable(method):
                    return None
                convos = method({"limit": 200, "offset": 0, "includeGroups": True})
            for entry in convos or []:
                if str(_field(entry, "id", "conversation_id", "conversationId") or "") == conversation_id:
                    return entry
            return None

        try:
            return await asyncio.to_thread(_lookup)
        except Exception as exc:
            logger.debug(
                "[Inkbox] text conversation summary lookup failed for %s: %s",
                conversation_id,
                exc,
            )
            return None

    def _build_sms_text_event(
        self,
        *,
        envelope: Dict[str, Any],
        text_id: str,
        remote: str,
        contact: Optional[Dict[str, Any]],
        chat_id: Any,
        contact_name: Optional[str],
        body: str,
        timestamp: datetime,
        text: Optional[str] = None,
        message_type: MessageType = MessageType.TEXT,
        media_urls: Optional[list[str]] = None,
        media_types: Optional[list[str]] = None,
        conversation_id: Optional[str] = None,
        is_group: bool = False,
        local_phone: Optional[str] = None,
        participants: Optional[list[str]] = None,
    ) -> MessageEvent:
        thread_id = f"sms:{conversation_id}" if conversation_id else None
        chat_name = (
            f"Inkbox SMS group {conversation_id or remote}"
            if is_group
            else contact_name or remote
        )
        source = self.build_source(
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type="dm",
            user_id=str(chat_id),
            user_name=contact_name or remote,
            user_id_alt=remote,
            thread_id=thread_id,
            message_id=text_id,
        )
        if text is None:
            contact_block = self._contact_marker(contact)
            if is_group:
                marker_parts = [
                    f"[inkbox:group_sms conversation_id={conversation_id or 'unknown'}",
                    f"from={remote}",
                    f"local={local_phone}" if local_phone else None,
                    f"participants={','.join(participants or [])}" if participants else None,
                    "reply_mode=conversation_id",
                    f"| {contact_block}]",
                ]
                marker = " ".join(part for part in marker_parts if part)
                group_policy = "\n".join([
                    "Group SMS response policy: you receive every message in this group so you can track context.",
                    "Reply only when the latest message clearly addresses this Inkbox agent, asks it to act, or a visible answer would be expected from the agent.",
                    "Treat ordinary group chatter as context only.",
                    "If no visible reply is warranted, return exactly [SILENT].",
                ])
                text = "\n".join(part for part in [marker, group_policy, body] if part)
            else:
                conversation_part = f" conversation_id={conversation_id}" if conversation_id else ""
                text = f"[inkbox:sms from={remote}{conversation_part} | {contact_block}]\n{body}"
        default_skills = (
            "inkbox:inkbox-troubleshooting"
            if message_type == MessageType.TEXT else None
        )
        channel_prompt, auto_skill = self._resolve_channel_overrides(
            "sms", chat_id, default_skills
        )
        return MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=envelope,
            message_id=text_id,
            channel_prompt=channel_prompt,
            auto_skill=auto_skill,
            timestamp=timestamp,
            media_urls=list(media_urls or []),
            media_types=list(media_types or []),
        )

    def busy_followup_policy(self, event: MessageEvent) -> Optional[Dict[str, Any]]:
        if event.message_type != MessageType.TEXT:
            return None
        text = (event.text or "").lstrip()
        if (
            text.startswith("[inkbox:sms ")
            or text.startswith("[inkbox:sms_burst ")
            or text.startswith("[inkbox:group_sms ")
            or text.startswith("[inkbox:group_sms_burst ")
            or text.startswith("[inkbox:imessage ")
            or text.startswith("[inkbox:imessage_burst ")
        ):
            return {"mode": "queue", "merge_text": True}
        return None

    async def _enqueue_sms_text_event(self, event: MessageEvent) -> None:
        key = self._sms_text_batch_key(event)
        text = event.text or ""
        marker, body = text.split("\n", 1) if "\n" in text else ("[inkbox:sms]", text)
        batch = self._pending_sms_text_batches.get(key)
        if batch is not None:
            next_count = len(batch["fragments"]) + 1
            next_chars = self._sms_text_batch_chars(batch) + len(body)
            if (
                next_count > self._sms_text_batch_max_messages
                or next_chars > self._sms_text_batch_max_chars
            ):
                await self._flush_sms_text_batch_now(key)
                batch = self._pending_sms_text_batches.get(key)

        if batch is None:
            batch = {
                "marker": marker,
                "fragments": [],
                "raw_messages": [],
            }
            self._pending_sms_text_batches[key] = batch

        batch["fragments"].append({
            "text": body,
            "timestamp": event.timestamp,
            "message_id": event.message_id,
            "source": event.source,
            "media_urls": list(event.media_urls or []),
            "media_types": list(event.media_types or []),
        })
        batch["raw_messages"].append(event.raw_message)
        batch["last_event"] = event

        if self._sms_text_batch_delay_seconds <= 0:
            await self._flush_sms_text_batch_now(key)
            return

        prior_task = self._pending_sms_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_sms_text_batch_tasks[key] = asyncio.create_task(
            self._flush_sms_text_batch_after_delay(key)
        )

    async def _flush_sms_text_batch_after_delay(self, key: str) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._sms_text_batch_delay_seconds)
            await self._flush_sms_text_batch_now(key)
        finally:
            if self._pending_sms_text_batch_tasks.get(key) is current_task:
                self._pending_sms_text_batch_tasks.pop(key, None)

    async def _flush_sms_text_batch_now(self, key: str) -> None:
        current_task = asyncio.current_task()
        task = self._pending_sms_text_batch_tasks.get(key)
        if task is not None and task is not current_task and not task.done():
            task.cancel()
        if task is not None and task is not current_task:
            self._pending_sms_text_batch_tasks.pop(key, None)

        batch = self._pending_sms_text_batches.pop(key, None)
        if not batch:
            return

        fragments = batch["fragments"]
        event = batch["last_event"]
        event.media_urls = [
            url
            for fragment in fragments
            for url in (fragment.get("media_urls") or [])
        ]
        event.media_types = [
            media_type
            for fragment in fragments
            for media_type in (fragment.get("media_types") or [])
        ]
        if len(fragments) == 1:
            body = fragments[0]["text"]
            event.text = f"{batch['marker']}\n{body}"
        else:
            first_at = fragments[0]["timestamp"]
            last_at = fragments[-1]["timestamp"]
            if str(batch["marker"]).startswith("[inkbox:group_sms "):
                burst_marker = batch["marker"].replace(
                    "[inkbox:group_sms ",
                    (
                        f"[inkbox:group_sms_burst messages={len(fragments)} "
                        f"first_at={_format_inkbox_timestamp(first_at)} "
                        f"last_at={_format_inkbox_timestamp(last_at)} "
                    ),
                    1,
                )
            elif str(batch["marker"]).startswith("[inkbox:imessage "):
                burst_marker = batch["marker"].replace(
                    "[inkbox:imessage ",
                    (
                        f"[inkbox:imessage_burst messages={len(fragments)} "
                        f"first_at={_format_inkbox_timestamp(first_at)} "
                        f"last_at={_format_inkbox_timestamp(last_at)} "
                    ),
                    1,
                )
            else:
                burst_marker = batch["marker"].replace(
                    "[inkbox:sms ",
                    (
                        f"[inkbox:sms_burst messages={len(fragments)} "
                        f"first_at={_format_inkbox_timestamp(first_at)} "
                        f"last_at={_format_inkbox_timestamp(last_at)} "
                    ),
                    1,
                )
            lines = [burst_marker]
            for fragment in fragments:
                delta = _format_sms_delta(first_at, fragment["timestamp"])
                lines.append(f"[{delta}] {fragment['text']}")
            event.text = "\n".join(lines)
            event.raw_message = {
                "event_type": "text.received.batch",
                "items": batch["raw_messages"],
            }
        event.message_id = fragments[-1].get("message_id") or event.message_id
        event.source = fragments[-1].get("source") or event.source
        event.timestamp = fragments[-1].get("timestamp") or event.timestamp
        await self._enqueue(event)

    async def _on_text_received(self, envelope: Dict[str, Any]) -> "web.Response":
        text_msg = (envelope.get("data") or {}).get("text_message") or {}
        text_id = str(text_msg.get("id") or "").strip()
        event_key = f"text:{text_id}" if text_id else ""
        if self._dedup_begin(event_key):
            return web.Response(status=200, text="duplicate")
        try:
            response = await self._on_text_received_once(envelope)
        except Exception:
            self._dedup_rollback(event_key)
            raise
        self._dedup_commit(event_key)
        return response

    async def _on_text_received_once(self, envelope: Dict[str, Any]) -> "web.Response":
        text_msg = (envelope.get("data") or {}).get("text_message") or {}
        data = envelope.get("data") or {}
        text_id = str(text_msg.get("id") or "").strip()
        direction = str(text_msg.get("direction") or "").strip().lower()
        if direction and direction != "inbound":
            return web.Response(status=200, text="ok")
        remote = (text_msg.get("remote_phone_number") or "").strip()
        if not remote:
            return web.Response(status=200, text="ok")
        conversation_id = str(
            text_msg.get("conversation_id") or text_msg.get("conversationId") or ""
        ).strip()
        local_phone = str(
            text_msg.get("local_phone_number") or text_msg.get("localPhoneNumber") or ""
        ).strip()
        conversation_summary = await self._lookup_text_conversation_summary(conversation_id)
        participants = []
        for entry in (
            _string_list_field(conversation_summary, "participants")
            + _string_list_field(text_msg, "participants")
        ):
            if entry not in participants:
                participants.append(entry)
        webhook_contacts = _webhook_list(data, "contacts", "contact_list")
        webhook_agent_identities = _webhook_list(
            data,
            "agent_identities",
            "agentIdentities",
            "identity_agents",
            "agentIdentities",
        )
        is_group = (
            _conversation_summary_is_group(conversation_summary)
            or bool(_field(text_msg, "isGroup", "is_group"))
            or len(participants) > 1
            or len(webhook_contacts) > 1
            or len(webhook_agent_identities) > 1
        )

        contact = await self._resolve_contact_full(kind="phone", value=remote)
        chat_id = _chat_id_for_route(
            contact,
            _channel_thread_key("sms", conversation_id),
            remote,
        )
        contact_name = contact["name"] if contact and contact.get("name") else None
        raw_body = text_msg.get("text") or ""
        body = raw_body
        media_urls, media_types, media_markers = _extract_text_media(text_msg)
        if media_markers:
            body = "\n".join(part for part in [body, *media_markers] if part)
        timestamp = _parse_inkbox_timestamp(text_msg.get("created_at"))

        control_word = _normalized_sms_control_word(raw_body)
        if control_word:
            logger.info(
                "[Inkbox] SMS control '%s' from %s handled as protocol text",
                control_word.upper(),
                redact_phone(remote),
            )
            return web.Response(status=200, text="ok")

        self._last_inbound_modality[str(chat_id)] = "sms"
        sms_state = {
            "conversation_id": conversation_id,
            "remote_phone_number": remote,
            "text_id": text_id,
            "conversation_kind": "group" if is_group else "direct",
        }
        self._last_inbound_sms[str(chat_id)] = sms_state
        if conversation_id:
            self._last_inbound_sms[_sms_state_key(chat_id, f"sms:{conversation_id}")] = sms_state

        if raw_body.lstrip().startswith("/"):
            event = self._build_sms_text_event(
                envelope=envelope,
                text_id=text_id,
                remote=remote,
                contact=contact,
                chat_id=chat_id,
                contact_name=contact_name,
                body=body,
                timestamp=timestamp,
                text=raw_body.strip(),
                message_type=MessageType.COMMAND,
                media_urls=media_urls,
                media_types=media_types,
                conversation_id=conversation_id,
                is_group=is_group,
                local_phone=local_phone,
                participants=participants,
            )
            await self._enqueue(event)
            return web.Response(status=200, text="ok")

        event = self._build_sms_text_event(
            envelope=envelope,
            text_id=text_id,
            remote=remote,
            contact=contact,
            chat_id=chat_id,
            contact_name=contact_name,
            body=body,
            timestamp=timestamp,
            media_urls=media_urls,
            media_types=media_types,
            conversation_id=conversation_id,
            is_group=is_group,
            local_phone=local_phone,
            participants=participants,
        )
        await self._enqueue_sms_text_event(event)
        return web.Response(status=200, text="ok")

    async def _on_text_lifecycle(self, envelope: Dict[str, Any]) -> "web.Response":
        """Log SMS delivery/status callbacks without enqueueing an agent turn."""
        event_type = str(envelope.get("event_type") or "")
        text_msg = (envelope.get("data") or {}).get("text_message") or {}
        text_id = str(text_msg.get("id") or "").strip()
        direction = str(text_msg.get("direction") or "").strip()
        remote = str(text_msg.get("remote_phone_number") or "").strip()
        status = (
            _plain_value(text_msg.get("delivery_status"))
            or _plain_value(text_msg.get("status"))
            or ""
        )
        error_code = _plain_value(text_msg.get("error") or text_msg.get("error_code"))
        logger.info(
            "[Inkbox] Text lifecycle event=%s id=%s direction=%s status=%s remote=%s error=%s",
            event_type,
            text_id,
            direction,
            status,
            redact_phone(remote),
            error_code or "",
        )
        return web.Response(status=200, text="ok")

    def _build_imessage_event(
        self,
        *,
        envelope: Dict[str, Any],
        message_id: str,
        remote: str,
        contact: Optional[Dict[str, Any]],
        chat_id: Any,
        contact_name: Optional[str],
        body: str,
        timestamp: datetime,
        text: Optional[str] = None,
        message_type: MessageType = MessageType.TEXT,
        media_urls: Optional[list[str]] = None,
        media_types: Optional[list[str]] = None,
        conversation_id: Optional[str] = None,
    ) -> MessageEvent:
        thread_id = f"imessage:{conversation_id}" if conversation_id else None
        source = self.build_source(
            chat_id=str(chat_id),
            chat_name=contact_name or remote,
            chat_type="dm",
            user_id=str(chat_id),
            user_name=contact_name or remote,
            user_id_alt=remote,
            thread_id=thread_id,
            message_id=message_id,
        )
        if text is None:
            contact_block = self._contact_marker(contact)
            conversation_part = (
                f" conversation_id={conversation_id}" if conversation_id else ""
            )
            text = f"[inkbox:imessage from={remote}{conversation_part} | {contact_block}]\n{body}"
        # iMessage always carries the responder playbook alongside the general
        # guide; operator overrides for this channel layer on top.
        default_skills = (
            ["inkbox:inkbox-troubleshooting", "inkbox:inkbox-imessage-responder"]
            if message_type == MessageType.TEXT else None
        )
        channel_prompt, auto_skill = self._resolve_channel_overrides(
            "imessage", chat_id, default_skills
        )
        return MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=envelope,
            message_id=message_id,
            channel_prompt=channel_prompt,
            auto_skill=auto_skill,
            timestamp=timestamp,
            media_urls=list(media_urls or []),
            media_types=list(media_types or []),
        )

    async def _on_imessage_received(self, envelope: Dict[str, Any]) -> "web.Response":
        """Route an inbound iMessage into the contact's Hermes session.

        Mirrors ``_on_text_received`` minus the SMS-only concerns: there is
        no group support, no opt-in control words, and no local number —
        iMessage rides a shared Inkbox-managed line, so the conversation id
        is the only stable reply target and is stashed for ``send()``.
        """
        message = (envelope.get("data") or {}).get("message") or {}
        message_id = str(message.get("id") or "").strip()
        event_key = f"imessage:{message_id}" if message_id else ""
        if self._dedup_begin(event_key):
            return web.Response(status=200, text="duplicate")
        try:
            response = await self._on_imessage_received_once(envelope)
        except Exception:
            self._dedup_rollback(event_key)
            raise
        self._dedup_commit(event_key)
        return response

    async def _on_imessage_received_once(self, envelope: Dict[str, Any]) -> "web.Response":
        message = (envelope.get("data") or {}).get("message") or {}
        message_id = str(message.get("id") or "").strip()
        direction = str(message.get("direction") or "").strip().lower()
        if direction and direction != "inbound":
            return web.Response(status=200, text="ok")
        remote = str(
            message.get("remote_number") or message.get("remoteNumber") or ""
        ).strip()
        if not remote:
            return web.Response(status=200, text="ok")
        conversation_id = str(
            message.get("conversation_id") or message.get("conversationId") or ""
        ).strip()

        contact = await self._resolve_contact_full(kind="phone", value=remote)
        chat_id = _chat_id_for_route(
            contact,
            _channel_thread_key("imessage", conversation_id),
            remote,
        )
        contact_name = contact["name"] if contact and contact.get("name") else None
        raw_body = message.get("content") or ""
        body = raw_body
        media_urls, media_types, media_markers = _extract_text_media(
            message, marker_label="iMessage",
        )
        if media_markers:
            body = "\n".join(part for part in [body, *media_markers] if part)
        timestamp = _parse_inkbox_timestamp(message.get("created_at"))

        self._last_inbound_modality[str(chat_id)] = "imessage"
        imessage_state = {
            "conversation_id": conversation_id,
            "remote_number": remote,
            "message_id": message_id,
        }
        self._last_inbound_imessage[str(chat_id)] = imessage_state
        if conversation_id:
            self._last_inbound_imessage[
                _sms_state_key(chat_id, f"imessage:{conversation_id}")
            ] = imessage_state

        if raw_body.lstrip().startswith("/"):
            event = self._build_imessage_event(
                envelope=envelope,
                message_id=message_id,
                remote=remote,
                contact=contact,
                chat_id=chat_id,
                contact_name=contact_name,
                body=body,
                timestamp=timestamp,
                text=raw_body.strip(),
                message_type=MessageType.COMMAND,
                media_urls=media_urls,
                media_types=media_types,
                conversation_id=conversation_id,
            )
            await self._enqueue(event)
            return web.Response(status=200, text="ok")

        event = self._build_imessage_event(
            envelope=envelope,
            message_id=message_id,
            remote=remote,
            contact=contact,
            chat_id=chat_id,
            contact_name=contact_name,
            body=body,
            timestamp=timestamp,
            media_urls=media_urls,
            media_types=media_types,
            conversation_id=conversation_id,
        )
        # Show the recipient a typing indicator while the agent works on the
        # reply. The pulse is cancelled in send() once the response goes out.
        self._start_imessage_typing(conversation_id)
        # iMessage users send fragment bursts just like SMS users — reuse
        # the quiet-window batcher (the burst marker rewrite understands
        # the [inkbox:imessage ...] prefix).
        await self._enqueue_sms_text_event(event)
        return web.Response(status=200, text="ok")

    async def _on_imessage_lifecycle(self, envelope: Dict[str, Any]) -> "web.Response":
        """Log iMessage delivery/status callbacks without enqueueing an agent turn.

        Also the landing spot for any other ``imessage.*`` fan-out we don't
        subscribe to (e.g. reactions, if a subscription drifts) — logged and
        acknowledged, never a turn.
        """
        event_type = str(envelope.get("event_type") or "")
        message = (envelope.get("data") or {}).get("message") or {}
        message_id = str(message.get("id") or "").strip()
        remote = str(message.get("remote_number") or "").strip()
        status = _plain_value(message.get("status")) or ""
        error_code = _plain_value(message.get("error_code"))
        logger.info(
            "[Inkbox] iMessage lifecycle event=%s id=%s status=%s remote=%s error=%s",
            event_type,
            message_id,
            status,
            redact_phone(remote),
            error_code or "",
        )
        return web.Response(status=200, text="ok")

    # ── iMessage typing indicator ──────────────────────────────────────────
    #
    IMESSAGE_TYPING_REFRESH_SECONDS = 40.0
    # Safety cap so a turn that errors out before send() (and thus never
    # cancels the pulse) can't leave the indicator pulsing indefinitely.
    IMESSAGE_TYPING_MAX_SECONDS = 600.0

    def _typing_tasks(self) -> Dict[str, "asyncio.Task"]:
        """Lazily-initialized typing-task registry.

        Tolerates adapter instances built via ``object.__new__`` (used in
        unit tests and any path that skips ``__init__``).
        """
        tasks = getattr(self, "_imessage_typing_tasks", None)
        if tasks is None:
            tasks = {}
            self._imessage_typing_tasks = tasks
        return tasks

    def _start_imessage_typing(self, conversation_id: str) -> None:
        """Begin (or keep) showing a typing indicator for a conversation."""
        if not conversation_id:
            return
        existing = self._typing_tasks().get(conversation_id)
        if existing is not None and not existing.done():
            return  # already pulsing for this conversation
        self._typing_tasks()[conversation_id] = asyncio.create_task(
            self._imessage_typing_loop(conversation_id)
        )

    def _stop_imessage_typing(self, conversation_id: str) -> None:
        """Cancel the typing pulse for a conversation, if any."""
        if not conversation_id:
            return
        task = self._typing_tasks().pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _stop_imessage_typing_for_chat(self, chat_id: Any) -> None:
        """Stop the typing pulse tied to a chat's last inbound iMessage.

        Used on send paths that return before resolving a conversation_id
        (e.g. the [SILENT] sentinel and admin-notice suppression), so a
        no-reply turn doesn't leave the indicator pulsing forever.
        """
        state = self._last_inbound_imessage.get(str(chat_id)) or {}
        self._stop_imessage_typing(str(state.get("conversation_id") or ""))

    async def _imessage_typing_loop(self, conversation_id: str) -> None:
        """Pulse the iMessage typing indicator until cancelled."""
        if self._inkbox is None or not self._identity_handle:
            return
        elapsed = 0.0
        try:
            while elapsed < self.IMESSAGE_TYPING_MAX_SECONDS:
                try:
                    identity = await asyncio.to_thread(
                        self._inkbox.get_identity, self._identity_handle,
                    )
                    send_typing = getattr(identity, "send_imessage_typing", None)
                    if not callable(send_typing):
                        return  # SDK too old — nothing to pulse
                    await asyncio.to_thread(send_typing, conversation_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A transient typing failure should never derail the turn;
                    # log at debug and keep trying on the next tick.
                    logger.debug(
                        "[Inkbox] iMessage typing pulse failed for %s: %s",
                        conversation_id, exc,
                    )
                await asyncio.sleep(self.IMESSAGE_TYPING_REFRESH_SECONDS)
                elapsed += self.IMESSAGE_TYPING_REFRESH_SECONDS
        except asyncio.CancelledError:
            pass
        finally:
            # If we exited on the safety cap, our entry is stale — drop it.
            # (When cancelled via _stop_imessage_typing the entry was already
            # popped, and a *newer* pulse may now own the slot; only remove
            # the mapping when it still points at this very task.)
            current = asyncio.current_task()
            tasks = self._typing_tasks()
            if tasks.get(conversation_id) is current:
                tasks.pop(conversation_id, None)

    async def _on_imessage_reaction(self, envelope: Dict[str, Any]) -> "web.Response":
        """Route an inbound tapback into the contact's Hermes session.

        Unlike SMS/email there is no body — the signal is the reaction itself
        plus which message it targets. We enqueue a turn that hands the agent
        the reaction and a response policy: a "question" tapback usually wants
        a reply, the rest usually don't, so the agent is told it may return
        [SILENT] when no visible reply is warranted (the same sentinel the
        group-SMS policy and send() suppression already understand).
        """
        reaction = (envelope.get("data") or {}).get("reaction") or {}
        reaction_id = str(reaction.get("id") or "").strip()
        event_key = f"imessage_reaction:{reaction_id}" if reaction_id else ""
        if self._dedup_begin(event_key):
            return web.Response(status=200, text="duplicate")
        try:
            response = await self._on_imessage_reaction_once(envelope)
        except Exception:
            self._dedup_rollback(event_key)
            raise
        self._dedup_commit(event_key)
        return response

    async def _on_imessage_reaction_once(self, envelope: Dict[str, Any]) -> "web.Response":
        reaction = (envelope.get("data") or {}).get("reaction") or {}
        reaction_id = str(reaction.get("id") or "").strip()
        direction = str(reaction.get("direction") or "").strip().lower()
        if direction and direction != "inbound":
            # The agent's own outbound tapbacks echo back as a webhook too.
            return web.Response(status=200, text="ok")
        remote = str(reaction.get("remote_number") or "").strip()
        if not remote:
            return web.Response(status=200, text="ok")
        conversation_id = str(reaction.get("conversation_id") or "").strip()
        target_message_id = str(reaction.get("target_message_id") or "").strip()
        reaction_type = str(reaction.get("reaction") or "").strip().lower()
        custom_emoji = str(reaction.get("custom_emoji") or "").strip()
        reaction_label = (
            f"{reaction_type}:{custom_emoji}"
            if reaction_type == "custom" and custom_emoji
            else reaction_type
        ) or "unknown"
        timestamp = _parse_inkbox_timestamp(reaction.get("created_at"))

        contact = await self._resolve_contact_full(kind="phone", value=remote)
        chat_id = _chat_id_for_route(
            contact,
            _channel_thread_key("imessage", conversation_id),
            remote,
        )
        contact_name = contact["name"] if contact and contact.get("name") else None
        contact_block = self._contact_marker(contact)

        # Keep the reply target fresh so a follow-up send() lands in the right
        # iMessage conversation, exactly like an inbound message would.
        self._last_inbound_modality[str(chat_id)] = "imessage"
        imessage_state = {
            "conversation_id": conversation_id,
            "remote_number": remote,
            "message_id": target_message_id,
        }
        self._last_inbound_imessage[str(chat_id)] = imessage_state
        if conversation_id:
            self._last_inbound_imessage[
                _sms_state_key(chat_id, f"imessage:{conversation_id}")
            ] = imessage_state

        conversation_part = (
            f" conversation_id={conversation_id}" if conversation_id else ""
        )
        target_part = (
            f" target_message_id={target_message_id}" if target_message_id else ""
        )
        marker = (
            f"[inkbox:imessage_reaction from={remote} reaction={reaction_label}"
            f"{conversation_part}{target_part} | {contact_block}]"
        )
        policy = "\n".join([
            f"{contact_name or remote} reacted with a '{reaction_label}' tapback to your message.",
            "A reaction is a lightweight signal, not always a request for a reply.",
            "Reply only when the reaction plausibly warrants one — e.g. a 'question' "
            "tapback usually asks for clarification or a follow-up, 'emphasize' may "
            "invite one, while 'love'/'like'/'laugh'/'dislike' are usually just "
            "acknowledgements that need no response.",
            "If no visible reply is warranted, return exactly [SILENT].",
        ])
        text = f"{marker}\n{policy}"

        source = self.build_source(
            chat_id=str(chat_id),
            chat_name=contact_name or remote,
            chat_type="dm",
            user_id=str(chat_id),
            user_name=contact_name or remote,
            user_id_alt=remote,
            thread_id=f"imessage:{conversation_id}" if conversation_id else None,
            message_id=target_message_id or reaction_id,
        )
        channel_prompt, auto_skill = self._resolve_channel_overrides(
            "imessage",
            chat_id,
            ["inkbox:inkbox-troubleshooting", "inkbox:inkbox-imessage-responder"],
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=envelope,
            message_id=reaction_id or target_message_id,
            channel_prompt=channel_prompt,
            auto_skill=auto_skill,
            timestamp=timestamp,
        )
        # A "question" tapback usually expects a reply, so show the typing
        # indicator while the agent works on it (cancelled on send, or on the
        # [SILENT] path if the agent decides no reply is warranted after all).
        # Other reaction types most often resolve to [SILENT], so we don't
        # promise a reply that isn't coming.
        if reaction_type == "question":
            self._start_imessage_typing(conversation_id)
        await self._enqueue(event)
        return web.Response(status=200, text="ok")

    async def _on_incoming_call(self, envelope: Dict[str, Any]) -> "web.Response":
        """Answer the call and return the WS URL Inkbox should connect to.

        The remote-party → contact lookup is done eagerly so the WS handler
        already has the contact_id mapped when Inkbox opens the WebSocket
        moments later (avoiding a race where the first transcript fires
        before the contact is resolved).
        """
        remote = (envelope.get("remote_phone_number") or "").strip()
        contact = await self._resolve_contact_full(kind="phone", value=remote)
        contact_id = contact["id"] if contact else None
        contact_name = contact["name"] if contact and contact.get("name") else None
        # Stash the resolved identity under the call_id so the WS handler
        # can pick it up via the ``client_websocket_url`` query string.
        call_id = envelope.get("id")
        if call_id:
            self._call_ws_meta[hash(str(call_id))] = {
                "call_id": str(call_id),
                "contact_id": str(contact_id or remote),
                "contact_name": contact_name or remote,
                "contact": contact,
                "remote_phone_number": remote,
            }

        ws_url = f"wss://{self._public_host}{self._ws_path}?call_id={call_id}"
        return web.json_response({
            "action": "answer",
            "client_websocket_url": ws_url,
        })

    # ------------------------------------------------------------------
    # Inbound: WebSocket (live calls)
    # ------------------------------------------------------------------

    async def _handle_call_ws(self, request: "web.Request") -> "web.WebSocketResponse":
        # Verify the HMAC on the upgrade BEFORE prepare(). The public tunnel
        # URL is reachable by anyone on the internet — the tunnel's TLS
        # only auths the SDK<->edge channel, not the requests flowing
        # through it. Inkbox-server signs the WS upgrade with the same
        # scheme as webhooks (sign_webhook_payload over the
        # X-Call-Context body), so the same verify_webhook works here.
        signature_payload = request.headers.get("X-Call-Context", "") or ""
        if self._require_signature:
            ok = verify_webhook(
                payload=signature_payload.encode(),
                headers=dict(request.headers),
                secret=self._signing_key,
            )
            if not ok:
                return web.Response(status=401, text="invalid signature")

        # ``WebSocketResponse`` doesn't take ``headers=`` as a constructor kwarg.
        # We mutate ``ws.headers`` immediately before ``prepare()`` once we know
        # whether this call can really use OpenAI Realtime. Preparing too early
        # commits Inkbox to raw-media mode before we know Realtime is reachable.
        ws = web.WebSocketResponse()

        async def _prepare_call_ws(*, use_realtime: bool) -> None:
            if use_realtime:
                ws.headers["x-use-inkbox-text-to-speech"] = "false"
                ws.headers["x-use-inkbox-speech-to-text"] = "false"
            else:
                ws.headers["x-use-inkbox-text-to-speech"] = "true"
                ws.headers["x-use-inkbox-speech-to-text"] = "true"
            await ws.prepare(request)

        # Resolve call context.  Three sources, tried in order:
        #   1. webhook-mode pre-stash from ``_on_incoming_call`` (legacy)
        #   2. ``x-call-context`` header (some Inkbox versions ship it)
        #   3. ``ink.phone.calls.get(...)`` round-trip — the only reliable
        #      source when Inkbox accepts the call itself and connects the
        #      WS without forwarding caller metadata.  Without this, every
        #      call lands as ``contact=unknown`` and the agent can't tell
        #      who's on the line until it manually queries the SDK.
        call_id = request.query.get("call_id", "")
        meta = self._call_ws_meta.pop(hash(call_id), None) or {}

        if not meta:
            ctx_raw = request.headers.get("x-call-context", "") or ""
            try:
                ctx = json.loads(ctx_raw) if ctx_raw else {}
            except json.JSONDecodeError:
                ctx = {}
            call_id = call_id or str(ctx.get("call_id") or ctx.get("id") or "")
            remote = (ctx.get("remote_phone_number") or "").strip()
            # NOTE: ``ctx`` may carry a ``direction`` field but it's reported
            # from Inkbox-server perspective (always "inbound to them"), so
            # we cannot trust it here.  The SDK call record is the only
            # authoritative source — fetched below.
            direction = ""

            # Always round-trip through the SDK to learn ``direction`` (and
            # backfill ``remote_phone_number`` if the header didn't carry it).
            # Direction drives session keying below — outbound calls join the
            # contact's main session for context continuity, inbound calls
            # stay isolated under their own thread.
            if call_id and self._inkbox is not None:
                try:
                    identity = await asyncio.to_thread(
                        self._inkbox.get_identity, self._identity_handle,
                    )
                    pn_id = getattr(identity.phone_number, "id", None)
                    if pn_id:
                        # ``_calls`` is the SDK's private call resource — same
                        # accessor the legacy phone-bridge used.  Public
                        # attribute is not yet exposed on Inkbox().
                        call = await asyncio.to_thread(
                            self._inkbox._calls.get, pn_id, call_id,
                        )
                        direction = (getattr(call, "direction", "") or "").strip().lower()
                        if not remote:
                            remote = (getattr(call, "remote_phone_number", "") or "").strip()
                except Exception as exc:
                    logger.warning(
                        "[Inkbox] Call lookup failed for call_id=%s: %s", call_id, exc,
                    )

            # Header value is only a fallback if the SDK round-trip failed.
            if not direction:
                direction = (ctx.get("direction") or "").strip().lower()

            contact = (
                await self._resolve_contact_full(kind="phone", value=remote)
                if remote else None
            )
            meta = {
                "call_id": call_id,
                "contact_id": (contact["id"] if contact else (remote or call_id or "unknown")),
                "contact_name": (
                    contact["name"] if contact and contact.get("name") else (remote or "unknown")
                ),
                "contact": contact,
                "remote_phone_number": remote,
                "direction": direction or "inbound",
            }

        contact_id = meta.get("contact_id") or call_id or "unknown"
        contact_name = meta.get("contact_name") or contact_id
        remote_phone_number = (meta.get("remote_phone_number") or "").strip() or None
        direction = (meta.get("direction") or "inbound").strip().lower()

        # Direction-aware session keying:
        #   - Outbound calls (the agent placed them) collapse into the
        #     contact's main session — same session SMS/email use — so the
        #     agent inherits the conversation that decided to call.  This
        #     is what lets it answer "why are you calling me?" without any
        #     external context-token plumbing.
        #   - Inbound calls (someone dialled us) stay isolated under their
        #     own ``call:<call_id>`` thread so the caller's fresh intent
        #     isn't drowned in old SMS/email history.
        call_thread_id = None if direction == "outbound" else f"call:{call_id}"

        # Bind this WS as the active sink for the contact, and tag the
        # contact's most-recent inbound modality as ``voice`` so the gateway's
        # outbound ``send()`` path routes the agent's reply onto this WS
        # rather than falling through to the SMS/email default heuristic.
        self._active_call_ws[contact_id] = ws
        self._last_inbound_modality[str(contact_id)] = "voice"

        # Outbound-call purpose: the agent that placed the call writes a
        # context file under ``$HERMES_HOME/inkbox_call_contexts/<token>.json``
        # and includes ``?context_token=<token>`` on the WS URL. Load it
        # before choosing realtime vs Inkbox STT/TTS so both paths get the
        # same call-start context.
        call_context: Dict[str, Any] = {}
        ctx_token = (request.query.get("context_token") or "").strip()
        if ctx_token:
            try:
                from hermes_cli.config import get_hermes_home
                ctx_path = get_hermes_home() / "inkbox_call_contexts" / f"{ctx_token}.json"
                if ctx_path.exists():
                    call_context = json.loads(ctx_path.read_text())
                    # Single-use: drop the file so abandoned tokens don't pile up.
                    with suppress(Exception):
                        ctx_path.unlink()
                else:
                    logger.warning(
                        "[Inkbox] Outbound-call context_token %s not found at %s",
                        ctx_token, ctx_path,
                    )
            except Exception as exc:
                logger.warning(
                    "[Inkbox] Failed to load context_token %s: %s", ctx_token, exc,
                )

        # Realtime voice bridge — when configured, pre-open OpenAI Realtime
        # before accepting the Inkbox websocket in raw-media mode. If preflight
        # fails and fallback is allowed, we still accept the same phone call
        # with Inkbox STT/TTS and continue into the text-event flow below.
        if self._realtime_config.enabled:
            realtime_bridge = None
            try:
                identity_for_meta = None
                if self._inkbox is not None:
                    try:
                        identity_for_meta = await asyncio.to_thread(
                            self._inkbox.get_identity, self._identity_handle,
                        )
                    except Exception:
                        identity_for_meta = None
                rt_contact = meta.get("contact") or {}
                rt_meta = RealtimeCallMeta(
                    call_id=call_id or "unknown",
                    contact_id=str(contact_id),
                    contact_name=str(contact_name),
                    remote_phone_number=remote_phone_number,
                    direction=direction or "inbound",
                    agent_identity_handle=self._identity_handle,
                    agent_identity_email=getattr(
                        getattr(identity_for_meta, "mailbox", None),
                        "email_address",
                        None,
                    ) if identity_for_meta is not None else None,
                    agent_identity_phone=getattr(
                        getattr(identity_for_meta, "phone_number", None),
                        "number",
                        None,
                    ) if identity_for_meta is not None else None,
                    contact_known=bool(meta.get("contact")),
                    contact_emails=list(rt_contact.get("emails") or []),
                    contact_phones=list(rt_contact.get("phones") or []),
                    contact_company=rt_contact.get("company") or None,
                    contact_notes=rt_contact.get("notes") or None,
                    outbound_purpose=str(call_context.get("purpose") or "").strip() or None,
                    outbound_opening=str(
                        call_context.get("opening_message")
                        or call_context.get("opening_line")
                        or call_context.get("openingMessage")
                        or ""
                    ).strip() or None,
                    outbound_reason=str(call_context.get("reason") or "").strip() or None,
                    outbound_scheduled_by=str(
                        call_context.get("scheduled_by") or ""
                    ).strip() or None,
                    outbound_conversation_summary=str(
                        call_context.get("conversation_summary") or ""
                    ).strip() or None,
                )
                realtime_bridge = await open_inkbox_realtime_bridge(
                    config=self._realtime_config,
                    meta=rt_meta,
                )
            except RealtimeBridgeConnectError as exc:
                if self._realtime_config.fallback_to_inkbox_stt_tts:
                    logger.warning(
                        "[Inkbox] realtime bridge connect failed for call_id=%s; "
                        "falling back to Inkbox STT/TTS: %s",
                        call_id,
                        exc.cause,
                    )
                else:
                    logger.warning(
                        "[Inkbox] realtime bridge connect failed for call_id=%s and "
                        "fallback is disabled: %s",
                        call_id,
                        exc.cause,
                    )
                    self._active_call_ws.pop(contact_id, None)
                    if self._last_inbound_modality.get(str(contact_id)) == "voice":
                        self._last_inbound_modality.pop(str(contact_id), None)
                    return web.Response(status=503, text="realtime bridge unavailable")
            except Exception as exc:
                if self._realtime_config.fallback_to_inkbox_stt_tts:
                    logger.warning(
                        "[Inkbox] realtime bridge preflight crashed for call_id=%s; "
                        "falling back to Inkbox STT/TTS: %s",
                        call_id,
                        exc,
                    )
                else:
                    logger.warning(
                        "[Inkbox] realtime bridge preflight crashed for call_id=%s "
                        "and fallback is disabled: %s",
                        call_id,
                        exc,
                    )
                    self._active_call_ws.pop(contact_id, None)
                    if self._last_inbound_modality.get(str(contact_id)) == "voice":
                        self._last_inbound_modality.pop(str(contact_id), None)
                    return web.Response(status=503, text="realtime bridge unavailable")

            if realtime_bridge is not None:
                try:
                    await _prepare_call_ws(use_realtime=True)
                    await realtime_bridge.run(
                        inkbox_ws=ws,
                        on_agent_consult=self._realtime_agent_consult,
                        on_post_call_actions=self._realtime_post_call_actions,
                        on_call_ended=self._realtime_call_ended,
                    )
                except Exception as exc:
                    logger.warning(
                        "[Inkbox] realtime bridge crashed for call_id=%s: %s",
                        call_id, exc,
                    )
                finally:
                    await realtime_bridge.close()
                    self._active_call_ws.pop(contact_id, None)
                    if self._last_inbound_modality.get(str(contact_id)) == "voice":
                        self._last_inbound_modality.pop(str(contact_id), None)
                    self._voice_recently_closed[str(contact_id)] = time.time()
                    try:
                        if not ws.closed:
                            await ws.close()
                    except Exception:
                        pass
                    logger.info("[Inkbox] Call WS closed: call_id=%s", call_id)
                return ws

        await _prepare_call_ws(use_realtime=False)

        logger.info(
            "[Inkbox] Call WS open: call_id=%s contact_id=%s remote=%s "
            "direction=%s thread=%s context=%s",
            call_id, contact_id, meta.get("remote_phone_number"),
            direction, call_thread_id,
            (call_context.get("reason") or "")[:80] if call_context else "(none)",
        )

        async def _send_text_delta(text: str, *, turn_id: str) -> None:
            await ws.send_str(json.dumps(
                {"event": "text", "delta": text, "turn_id": turn_id}
            ))

        async def _send_text_done(*, turn_id: str) -> None:
            await ws.send_str(json.dumps(
                {"event": "text", "done": True, "turn_id": turn_id}
            ))

        greeting_sent = False

        async def _send_static_greeting() -> None:
            """Static opener for INBOUND calls — caller is unknown intent.

            Sent direct from the adapter without going through the agent so
            the caller hears something within ~1s of pickup.  Inbound calls
            don't have prior context worth opening on, so a generic greeting
            is fine.
            """
            contact = meta.get("contact") or {}
            first_name = ""
            if contact.get("name"):
                first_name = str(contact["name"]).split()[0]
            who = f"{first_name}" if first_name else "there"
            text = f"Hi {who}, how can I help?"
            try:
                await _send_text_delta(text, turn_id="greeting")
                await _send_text_done(turn_id="greeting")
                logger.info("[Inkbox] Sent static greeting to call_id=%s", call_id)
            except Exception as exc:
                logger.warning("[Inkbox] Failed to send greeting: %s", exc)

        async def _trigger_outbound_opening() -> None:
            """Opener for OUTBOUND calls — let the agent speak first.

            We placed this call.  The session this call lands on is the same
            one that decided to call (SMS thread / email thread for the
            contact), so the agent already has full context for *why* it's
            calling.  Enqueue a synthetic event that asks the agent to greet
            with that context in mind — its reply rides the call WS as the
            first audio the callee hears.

            Trade-off: a 1-2s pause at pickup while the agent generates the
            opener.  Worth it: the caller gets "Hey Dima, calling about the
            cats thing as you asked" instead of a generic "How can I help?"
            from a system that just dialed them.
            """
            contact_block = self._contact_marker(meta.get("contact"))
            tagged = (
                f"[inkbox:voice_call call_id={call_id} | {contact_block}]\n"
                "[outbound_call_connected] You just placed this call. The "
                "callee picked up. Greet them by name and open with the "
                "reason for the call, drawing from the conversation that "
                "decided to place it (above in this thread). Keep it to one "
                "short sentence; the rest of the conversation will follow."
            )
            source = self.build_source(
                chat_id=str(contact_id),
                chat_name=contact_name,
                chat_type="dm",
                user_id=str(contact_id),
                user_name=contact_name,
                user_id_alt=remote_phone_number,
                thread_id=call_thread_id,
                chat_topic="voice_call",
                message_id=f"call:{call_id}:opening",
            )
            channel_prompt, auto_skill = self._resolve_channel_overrides(
                "voice", contact_id, "inkbox:inkbox-troubleshooting"
            )
            event = MessageEvent(
                text=tagged,
                message_type=MessageType.TEXT,
                source=source,
                raw_message={"synthetic": "outbound_call_opening"},
                message_id=f"call:{call_id}:opening",
                channel_prompt=channel_prompt,
                auto_skill=auto_skill,
            )
            try:
                await self._enqueue(event)
                logger.info(
                    "[Inkbox] Triggered outbound opener for call_id=%s", call_id,
                )
            except Exception as exc:
                logger.warning(
                    "[Inkbox] Failed to enqueue outbound opener: %s", exc,
                )

        first_transcript_seen = False

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                ev = payload.get("event")
                if ev == "start" and not greeting_sent:
                    greeting_sent = True
                    if direction == "outbound":
                        asyncio.create_task(_trigger_outbound_opening())
                    else:
                        asyncio.create_task(_send_static_greeting())
                    continue
                if ev == "transcript" and payload.get("is_final"):
                    text = (payload.get("text") or "").strip()
                    if not text:
                        continue
                    source = self.build_source(
                        chat_id=str(contact_id),
                        chat_name=contact_name,
                        chat_type="dm",
                        user_id=str(contact_id),
                        user_name=contact_name,
                        user_id_alt=remote_phone_number,
                        thread_id=call_thread_id,
                        chat_topic="voice_call",
                        message_id=payload.get("turn_id"),
                    )
                    contact_block = self._contact_marker(meta.get("contact"))

                    # On the FIRST transcript only, prepend the call-purpose
                    # block (if any) so the in-call agent — which has no
                    # memory of why it's calling — has authoritative context.
                    purpose_block = ""
                    if call_context and not first_transcript_seen:
                        reason = (call_context.get("reason") or "").strip()
                        scheduled_by = (call_context.get("scheduled_by") or "").strip()
                        prior = (call_context.get("conversation_summary") or "").strip()
                        lines = ["[outbound_call_context]"]
                        if reason:
                            lines.append(f"reason: {reason}")
                        if scheduled_by:
                            lines.append(f"scheduled_by: {scheduled_by}")
                        if prior:
                            lines.append(f"prior_conversation: {prior}")
                        lines.append("[/outbound_call_context]")
                        purpose_block = "\n".join(lines) + "\n"
                    first_transcript_seen = True

                    tagged = (
                        f"[inkbox:voice_call call_id={call_id} | {contact_block}]\n"
                        f"{purpose_block}{text}"
                    )
                    channel_prompt, auto_skill = self._resolve_channel_overrides(
                        "voice", contact_id, "inkbox:inkbox-troubleshooting"
                    )
                    event = MessageEvent(
                        text=tagged,
                        message_type=MessageType.TEXT,
                        source=source,
                        raw_message=payload,
                        message_id=f"call:{call_id}:{payload.get('turn_id') or ''}",
                        channel_prompt=channel_prompt,
                        auto_skill=auto_skill,
                    )
                    await self._enqueue(event)
                elif ev == "stop":
                    break
                # 'barge_in' is informational here — proper interruption
                # would require cancelling the in-flight gateway turn, which
                # crosses module boundaries we don't yet expose.
        finally:
            self._active_call_ws.pop(contact_id, None)
            # Clear the voice tag so a follow-up SMS/email from this contact
            # doesn't get mis-routed to a closed call socket.
            if self._last_inbound_modality.get(str(contact_id)) == "voice":
                self._last_inbound_modality.pop(str(contact_id), None)
            # Stamp the close time so send() can drop in-flight voice replies
            # that finish generating after the WS is gone, instead of letting
            # them leak to email/SMS via the default mode heuristic.
            self._voice_recently_closed[str(contact_id)] = time.time()
            with suppress(Exception):
                await ws.close()
            logger.info("[Inkbox] Call WS closed: call_id=%s", call_id)

            # Post-call reflection: enqueue a synthetic [call_ended] turn so
            # the agent has a chance to do follow-up work (send promised
            # emails, schedule callbacks, save notes, update memory).  The
            # agent's text reply will be suppressed by the voice-grace guard
            # in send() — only its TOOL CALLS produce side effects.  If the
            # agent has nothing to do it can answer "[SILENT]" and the
            # cron-style suppression delivers nothing.
            try:
                contact_block = self._contact_marker(meta.get("contact"))
                tagged = (
                    f"[inkbox:voice_call call_id={call_id} | {contact_block}]\n"
                    "[call_ended] The call has ended. Reflect on what just "
                    "happened and decide if any follow-up actions are "
                    "needed:\n"
                    "  - if you committed to anything during the call (send "
                    "an email, schedule a callback, text a contact, save a "
                    "note, update a contact record), perform that now via "
                    "tool calls — execute_code/terminal for SDK actions, "
                    "cronjob create deliver=local for delayed work, memory/"
                    "send_message for the obvious cases.\n"
                    "  - if there's nothing to do, reply with exactly "
                    "[SILENT] and no other text.\n"
                    "Note: any plain-text reply you produce here will be "
                    "suppressed (the caller hung up — they don't want a "
                    "trailing TTS or email containing your thoughts). "
                    "Side effects must come from tool calls."
                )
                source = self.build_source(
                    chat_id=str(contact_id),
                    chat_name=contact_name,
                    chat_type="dm",
                    user_id=str(contact_id),
                    user_name=contact_name,
                    user_id_alt=remote_phone_number,
                    thread_id=call_thread_id,
                    chat_topic="voice_call",
                    message_id=f"call:{call_id}:ended",
                )
                channel_prompt, auto_skill = self._resolve_channel_overrides(
                    "voice", contact_id, "inkbox:inkbox-troubleshooting"
                )
                event = MessageEvent(
                    text=tagged,
                    message_type=MessageType.TEXT,
                    source=source,
                    raw_message={"synthetic": "call_ended"},
                    message_id=f"call:{call_id}:ended",
                    channel_prompt=channel_prompt,
                    auto_skill=auto_skill,
                )
                await self._enqueue(event)
                logger.info(
                    "[Inkbox] Enqueued [call_ended] reflection for call_id=%s",
                    call_id,
                )
            except Exception as exc:
                logger.warning(
                    "[Inkbox] Failed to enqueue call_ended event: %s", exc,
                )
        return ws

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    # Realtime voice bridge callbacks
    # ------------------------------------------------------------------

    @staticmethod
    def _format_realtime_post_call_actions(actions: List[Dict[str, str]]) -> str:
        lines = []
        for i, action in enumerate(actions, start=1):
            text = f"{i}. {action.get('action', '')}".strip()
            details = (action.get("details") or "").strip()
            if details:
                text += f"\n   Details: {details}"
            lines.append(text)
        return "\n".join(lines)

    @staticmethod
    def _format_realtime_consult_results(results: List[RealtimeConsultResult]) -> str:
        lines = []
        for i, result in enumerate(results, start=1):
            request = getattr(result, "request", "")
            answer = getattr(result, "result", "")
            lines.append(f"{i}. Request: {request}\nResult: {answer}")
        return "\n\n".join(lines)

    async def _realtime_agent_consult(
        self,
        meta: RealtimeCallMeta,
        query: str,
        transcript: List[Tuple[str, str]],
        post_call_actions: Optional[List[Dict[str, str]]] = None,
        consult_results: Optional[List[RealtimeConsultResult]] = None,
    ) -> str:
        """Run the agent_consult tool: spawn a one-shot Hermes agent invocation.

        Uses ``hermes -z PROMPT`` (the CLI's --oneshot flag) so the main agent
        runs in its own session with full tooling. The spawned agent's stdout
        is captured and returned to the realtime model, which speaks it back
        to the caller.

        Why subprocess rather than in-process dispatch: we need a clean
        capture of the agent's text reply without mutating ``self.send()``
        for the duration of the consult (which would race with other
        concurrent calls). Subprocess overhead (~2s) is acceptable for a
        "let me look that up" interjection — the realtime model says "one
        moment" while it runs (see ``inkbox_realtime._dispatch_tool_call``).
        """
        prompt_lines = [
            "You are answering a question on behalf of an in-progress phone call.",
            f"Caller: {meta.contact_name}"
            + (f" ({meta.remote_phone_number})" if meta.remote_phone_number else ""),
            f"Call direction: {meta.direction}",
            "",
            "Recent transcript:",
        ]
        for role, text in transcript[-10:]:
            prompt_lines.append(f"  {role}: {text}")
        post_call_actions = post_call_actions or []
        consult_results = consult_results or []
        if post_call_actions:
            prompt_lines.extend([
                "",
                "Pending after-call actions already queued by the realtime call agent:",
                self._format_realtime_post_call_actions(post_call_actions),
                (
                    "If this consult completes, queues, cancels, or supersedes one "
                    "of those pending actions, say so explicitly in your result so "
                    "the call agent can delete that after-call action before hangup."
                ),
            ])
        if consult_results:
            prompt_lines.extend([
                "",
                "Previous Hermes consult results during this same live call:",
                self._format_realtime_consult_results(consult_results),
                (
                    "Do not repeat work that was already completed or queued unless "
                    "the caller explicitly asked for another, repeat, or different action."
                ),
            ])
        prompt_lines.extend([
            "",
            f"The realtime voice agent asked: {query}",
            "",
            "Answer concisely and naturally; your reply will be read aloud to "
            "the caller. Skip preamble; deliver the answer directly.",
        ])
        prompt = "\n".join(prompt_lines)

        hermes_bin = shutil.which("hermes") or "/home/ec2-user/.local/bin/hermes"
        try:
            proc = await asyncio.create_subprocess_exec(
                hermes_bin, "-z", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "HERMES_NO_TUI": "1"},
            )
            stdout_bytes, _stderr_bytes = await proc.communicate()
        except FileNotFoundError:
            return (
                "I couldn't reach the main Hermes agent to look that up. "
                "Tell the caller I'll follow up after the call."
            )
        text = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not text:
            return (
                "The lookup didn't return anything useful. Apologize "
                "briefly and ask if you can help another way."
            )
        return text

    async def _realtime_call_ended(
        self,
        meta: RealtimeCallMeta,
        transcript: List[Tuple[str, str]],
    ) -> None:
        """Enqueue the legacy [call_ended] reflection for realtime calls."""
        transcript_block = "\n".join(
            f"  - {role}: {text}" for role, text in transcript[-30:]
        )
        body_parts = [
            f"[inkbox:voice_call call_id={meta.call_id}]",
            "[call_ended] The realtime voice call has ended. Reflect on what just "
            "happened and decide if any follow-up actions are needed:",
            "  - if you committed to anything during the call (send an email, "
            "schedule a callback, text a contact, save a note, update a contact "
            "record), perform that now via tool calls.",
            "  - if there's nothing to do, reply with exactly [SILENT] and no other text.",
            "Note: any plain-text reply you produce here will be suppressed. "
            "Side effects must come from tool calls.",
        ]
        if transcript_block:
            body_parts.extend(["", "Recent realtime-call transcript:", transcript_block])
        body = "\n".join(body_parts)
        source = self.build_source(
            chat_id=meta.contact_id,
            chat_name=meta.contact_name,
            chat_type="dm",
            user_id=meta.contact_id,
            user_name=meta.contact_name,
            user_id_alt=meta.remote_phone_number,
            thread_id=None if meta.direction == "outbound" else f"call:{meta.call_id}",
            chat_topic="voice_call",
            message_id=f"call:{meta.call_id}:ended",
        )
        channel_prompt, auto_skill = self._resolve_channel_overrides(
            "voice", meta.contact_id, "inkbox:inkbox-call-review"
        )
        event = MessageEvent(
            text=body,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={"event": "realtime_call_ended", "transcript": transcript},
            message_id=f"call:{meta.call_id}:ended",
            reply_to_message_id=meta.call_id,
            channel_prompt=channel_prompt,
            auto_skill=auto_skill,
        )
        try:
            await self._enqueue(event)
            logger.info(
                "[Inkbox] Enqueued realtime [call_ended] reflection for call_id=%s",
                meta.call_id,
            )
        except Exception as exc:
            logger.warning(
                "[Inkbox] realtime call_ended enqueue failed for call_id=%s: %s",
                meta.call_id, exc,
            )

    async def _realtime_post_call_actions(
        self,
        meta: RealtimeCallMeta,
        actions: List[Dict[str, str]],
        transcript: List[Tuple[str, str]],
        consult_results: Optional[List[RealtimeConsultResult]] = None,
    ) -> None:
        """Dispatch queued post-call actions as a synthetic SMS-mode turn.

        Mirrors the Inkbox channel plugin's post-call action flow: build a single
        synthetic inbound message containing all queued actions + recent
        transcript, push it through the normal inbound queue so the main
        agent executes them with its full toolset.
        """
        action_lines = self._format_realtime_post_call_actions(actions).splitlines()
        consult_results = consult_results or []
        consult_block = self._format_realtime_consult_results(consult_results)
        transcript_block = "\n".join(
            f"{role}: {text}" for role, text in transcript
        )
        body = "\n".join([
            f"[inkbox:voice_post_call_actions call_id={meta.call_id}]",
            "The realtime voice call ended. Review these queued post-call actions "
            "and execute only the actions that are still needed.",
            "These actions were registered during the live call and may be stale. "
            "Before doing anything, reconcile them against the full live-call "
            "transcript, in-call Hermes consult results, and prior messages in "
            "this session.",
            "If an action was already completed or queued during the call, canceled, "
            "superseded, or the caller said it already happened, do not perform it "
            "again. A same-channel in-call consult result that says an SMS/email "
            "was sent or queued counts as already handled.",
            "Do not merely say still-needed actions are impossible. If an email, "
            "SMS, note, or contact update is still needed and enough recipient/"
            "content info is present, perform it.",
            "Do NOT send a confirmation follow-up after successful work unless the "
            "caller explicitly requested one. Only if required information is "
            "missing, ask the caller for the missing information. Try SMS first; "
            "if SMS is unavailable or not opted in, try email; if email is "
            "unavailable, place a follow-up call with the question.",
            "",
            "Queued actions:",
            *action_lines,
            "",
            "In-call Hermes consult results:" if consult_block else "",
            consult_block,
            "",
            "Full live-call transcript:" if transcript_block else "",
            transcript_block,
        ])
        source = self.build_source(
            chat_id=meta.contact_id,
            chat_name=meta.contact_name,
            chat_type="dm",
            user_id=meta.contact_id,
            user_name=meta.contact_name,
            user_id_alt=meta.remote_phone_number,
            thread_id=None if meta.direction == "outbound" else f"call:{meta.call_id}",
            chat_topic="voice_call",
            message_id=f"call:{meta.call_id}:post-call-actions",
        )
        event = MessageEvent(
            text=body,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={
                "event": "realtime_post_call_actions",
                "actions": actions,
                "consult_results": [
                    {
                        "id": result.id,
                        "request": result.request,
                        "result": result.result,
                        "created_at": result.created_at,
                        "dedupe_key": result.dedupe_key,
                    }
                    for result in consult_results
                ],
            },
            message_id=f"call:{meta.call_id}:post-call-actions",
            reply_to_message_id=meta.call_id,
        )
        try:
            await self._enqueue(event)
        except Exception as exc:
            logger.warning(
                "[Inkbox] post-call action enqueue failed for call_id=%s: %s",
                meta.call_id, exc,
            )

    # ------------------------------------------------------------------

    async def _resolve_contact(
        self, *, kind: str, value: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Thin wrapper that returns just ``(contact_id, display_name)``.

        Kept for the call-sites that only need the chat-routing pair.  New
        code that wants emails / phones / company / notes should use
        :meth:`_resolve_contact_full` instead.
        """
        details = await self._resolve_contact_full(kind=kind, value=value)
        if details is None:
            return (None, None)
        return (details.get("id"), details.get("name"))

    async def _resolve_contact_full(
        self, *, kind: str, value: str,
    ) -> Optional[Dict[str, Any]]:
        """Return a serialisable summary of the Inkbox Contact matched by *value*.

        Shape::

            {
                "id":       "<uuid>",
                "name":     "Dima Vremenko",
                "emails":   ["dima@vectorly.app", ...],   # primary first
                "phones":   ["+15167251294", ...],         # primary first
                "company":  "Inkbox",
                "job_title": "Cofounder",
                "notes":    "...",
            }

        ``None`` when the lookup returns 0 or >1 matches.  Cached for
        ``CONTACT_CACHE_TTL_SECONDS`` (positive *and* negative results).
        """
        if not value:
            return None
        cache_key = (kind, value.lower())
        now = time.time()
        cached = self._contact_cache.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

        if self._inkbox is None:
            return None

        kwargs = {kind: value}
        try:
            contacts = await asyncio.to_thread(self._inkbox.contacts.lookup, **kwargs)
        except Exception as exc:
            logger.debug("[Inkbox] contacts.lookup(%s=%s) failed: %s", kind, value, exc)
            self._contact_cache[cache_key] = (None, now + CONTACT_CACHE_TTL_SECONDS)
            return None

        if len(contacts) != 1:
            self._contact_cache[cache_key] = (None, now + CONTACT_CACHE_TTL_SECONDS)
            return None

        contact = contacts[0]
        emails_raw = list(getattr(contact, "emails", None) or [])
        phones_raw = list(getattr(contact, "phones", None) or [])
        emails_raw.sort(key=lambda e: not getattr(e, "is_primary", False))
        phones_raw.sort(key=lambda p: not getattr(p, "is_primary", False))
        details: Dict[str, Any] = {
            "id": str(getattr(contact, "id", "")),
            "name": (
                getattr(contact, "preferred_name", None)
                or getattr(contact, "given_name", None)
                or None
            ),
            "emails": [getattr(e, "value", "") for e in emails_raw if getattr(e, "value", "")],
            "phones": [getattr(p, "value", "") for p in phones_raw if getattr(p, "value", "")],
            "company": getattr(contact, "company_name", None) or None,
            "job_title": getattr(contact, "job_title", None) or None,
            "notes": ((getattr(contact, "notes", None) or "")[:200].strip() or None),
        }
        self._contact_cache[cache_key] = (details, now + CONTACT_CACHE_TTL_SECONDS)
        return details

    @staticmethod
    def _contact_marker(details: Optional[Dict[str, Any]]) -> str:
        """Render a one-line contact summary for embedding in MessageEvent text."""
        if not details:
            return "contact=unknown_in_inkbox"
        parts = [f"contact_id={details['id']}"]
        if details.get("name"):
            parts.append(f"contact_name={details['name']!r}")
        if details.get("company"):
            parts.append(f"contact_company={details['company']!r}")
        if details.get("emails"):
            parts.append(f"contact_emails={details['emails']}")
        if details.get("phones"):
            parts.append(f"contact_phones={details['phones']}")
        return " ".join(parts)

    def _lookup_contact_email(self, contact_id: str) -> Optional[str]:
        """Fetch the primary email address for a contact (sync helper)."""
        if self._inkbox is None:
            return None
        try:
            contact = self._inkbox.contacts.get(contact_id)
        except Exception:
            return None
        emails = getattr(contact, "emails", None) or []
        primary = next((e for e in emails if getattr(e, "is_primary", False)), None)
        chosen = primary or (emails[0] if emails else None)
        return getattr(chosen, "value", None) if chosen else None

    def _lookup_contact_phone(self, contact_id: str) -> Optional[str]:
        """Fetch the primary phone number (E.164) for a contact (sync helper)."""
        if self._inkbox is None:
            return None
        try:
            contact = self._inkbox.contacts.get(contact_id)
        except Exception:
            return None
        phones = getattr(contact, "phones", None) or []
        primary = next((p for p in phones if getattr(p, "is_primary", False)), None)
        chosen = primary or (phones[0] if phones else None)
        return getattr(chosen, "value", None) if chosen else None

    async def _enqueue(self, event: MessageEvent) -> None:
        """Dispatch an inbound event to the gateway runner as a background task."""
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)


# ---------------------------------------------------------------------------
# Standalone send helper (for cron + send_message tool outside the gateway)
# ---------------------------------------------------------------------------


async def send_inkbox_direct(
    extra: Dict[str, Any],
    chat_id: str,
    message: str,
    *,
    mode: Optional[str] = None,
    subject: Optional[str] = None,
    thread_id: Optional[str] = None,  # noqa: ARG001 — reserved for future email-thread replies
) -> Dict[str, Any]:
    """One-shot send via the Inkbox SDK — no aiohttp server, no WS.

    Mirrors the ``_send_*_direct`` helpers used by the other platforms for
    cron delivery and ``send_message`` calls outside an active gateway.
    """
    if not INKBOX_AVAILABLE:
        return {
            "error": "Inkbox SDK not installed. Run: pip install inkbox",
        }

    api_key = (extra.get("api_key") or os.getenv("INKBOX_API_KEY") or "").strip()
    if not api_key:
        return {"error": "INKBOX_API_KEY not set"}
    handle = (extra.get("identity") or os.getenv("INKBOX_IDENTITY") or "").strip()
    if not handle:
        return {"error": "INKBOX_IDENTITY not set"}
    base_url = (
        extra.get("base_url") or os.getenv("INKBOX_BASE_URL") or INKBOX_BASE_URL_DEFAULT
    )
    chosen_mode = (mode or "").lower().strip()
    if not chosen_mode:
        if _imessage_conversation_target(chat_id):
            chosen_mode = "imessage"
        else:
            chosen_mode = "sms" if str(chat_id).startswith("+") or _sms_conversation_target(chat_id) else "email"
    if chosen_mode == "sms" and len(message or "") > SMS_MAX_LENGTH:
        return _sms_too_long_failure_dict(message)
    if chosen_mode == "imessage" and len(message or "") > IMESSAGE_MAX_LENGTH:
        return _imessage_too_long_failure_dict(message)

    def _do_send() -> Dict[str, Any]:
        with Inkbox(**inkbox_client_kwargs(api_key, base_url)) as client:
            identity = client.get_identity(handle)

            if chosen_mode == "sms":
                conversation_id = _sms_conversation_target(chat_id)
                if conversation_id:
                    try:
                        msg = identity.send_text(conversation_id=conversation_id, text=message)
                    except TypeError:
                        msg = identity.send_text({"conversationId": conversation_id, "text": message})
                    except Exception as exc:
                        logger.error(
                            "[Inkbox] Direct SMS send failed to conversation %s",
                            conversation_id,
                        )
                        return _sms_send_failure_dict(exc)
                    return {
                        "success": True,
                        "platform": "inkbox",
                        "chat_id": chat_id,
                        "message_id": str(getattr(msg, "id", "")),
                        "mode": "sms",
                        "conversation_id": conversation_id,
                        "delivery_status": _plain_value(
                            getattr(msg, "delivery_status", None),
                        ),
                    }
                target = chat_id
                if not str(target).startswith("+"):
                    contact = client.contacts.get(chat_id)
                    phones = getattr(contact, "phones", None) or []
                    primary = next((p for p in phones if getattr(p, "is_primary", False)), None)
                    chosen = primary or (phones[0] if phones else None)
                    target = getattr(chosen, "value", None) if chosen else None
                if not target:
                    return {"error": f"No phone for contact {chat_id}"}
                try:
                    msg = identity.send_text(to=target, text=message)
                except Exception as exc:
                    logger.error(
                        "[Inkbox] Direct SMS send failed to %s",
                        redact_phone(str(target)),
                    )
                    return _sms_send_failure_dict(exc)
                return {
                    "success": True,
                    "platform": "inkbox",
                    "chat_id": chat_id,
                    "message_id": str(getattr(msg, "id", "")),
                    "mode": "sms",
                    "delivery_status": _plain_value(
                        getattr(msg, "delivery_status", None),
                    ),
                }

            if chosen_mode == "imessage":
                send_imessage = getattr(identity, "send_imessage", None)
                if not callable(send_imessage):
                    return {
                        "error": (
                            "Installed Inkbox SDK has no send_imessage; "
                            "upgrade with: pip install -U inkbox"
                        ),
                    }
                conversation_id = _imessage_conversation_target(chat_id)
                try:
                    if conversation_id:
                        msg = send_imessage(conversation_id=conversation_id, text=message)
                    elif str(chat_id).startswith("+"):
                        msg = send_imessage(to=str(chat_id).strip(), text=message)
                    else:
                        return {"error": f"No iMessage conversation target in {chat_id!r}"}
                except Exception as exc:
                    logger.error(
                        "[Inkbox] Direct iMessage send failed to %s",
                        conversation_id or redact_phone(str(chat_id)),
                    )
                    fields = _extract_inkbox_sms_error(exc)
                    return {
                        "success": False,
                        "platform": "inkbox",
                        "mode": "imessage",
                        "error": _format_inkbox_imessage_error(fields),
                        **fields,
                    }
                return {
                    "success": True,
                    "platform": "inkbox",
                    "chat_id": chat_id,
                    "message_id": str(getattr(msg, "id", "")),
                    "mode": "imessage",
                    "conversation_id": conversation_id or _plain_value(
                        getattr(msg, "conversation_id", None),
                    ),
                    "status": _plain_value(getattr(msg, "status", None)),
                }

            if chosen_mode == "email":
                target = str(chat_id).strip()
                if "@" not in target:
                    # chat_id is a contact UUID — look up the primary email.
                    contact = client.contacts.get(chat_id)
                    emails = getattr(contact, "emails", None) or []
                    primary = next((e for e in emails if getattr(e, "is_primary", False)), None)
                    chosen = primary or (emails[0] if emails else None)
                    target = getattr(chosen, "value", None) if chosen else None
                if not target:
                    return {"error": f"No email for contact {chat_id}"}
                msg = identity.send_email(
                    to=[target],
                    subject=subject or "(no subject)",
                    body_text=message,
                )
                return {
                    "success": True,
                    "platform": "inkbox",
                    "chat_id": chat_id,
                    "message_id": str(getattr(msg, "id", "")),
                    "mode": "email",
                }

            return {"error": f"Unknown Inkbox send mode: {chosen_mode!r}"}

    try:
        return await asyncio.to_thread(_do_send)
    except Exception as exc:
        return {"error": f"Inkbox send failed: {exc}"}
