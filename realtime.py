"""Inkbox ↔ OpenAI Realtime API voice bridge.

When ``inkbox.realtime.enabled`` is true and an OpenAI API key is configured,
the call WebSocket handler in :mod:`gateway.platforms.inkbox`
delegates inbound calls to :func:`run_inkbox_realtime_bridge` instead of using
Inkbox-side STT/TTS.

The bridge:

1. Preflights an OpenAI Realtime API WebSocket
   (``wss://api.openai.com/v1/realtime?model=<model>``) and sends
   ``session.update`` configuring tools, instructions, and the
   ``g711_ulaw`` input/output audio format.
2. Lets the adapter accept the Inkbox call WS with
   ``x-use-inkbox-text-to-speech: false`` and
   ``x-use-inkbox-speech-to-text: false`` headers only after OpenAI is ready,
   so fallback to Inkbox STT/TTS is still possible before accept.
3. Bridges audio bidirectionally: Inkbox → OpenAI as
   ``input_audio_buffer.append`` events, OpenAI → Inkbox as
   ``media`` frames carrying ``response.audio.delta`` payloads.
4. Exposes realtime-only tools to the model:

   - ``hermes_agent_consult`` — dispatches a synthetic SMS-mode turn through
     Hermes' main agent loop *in the background* and submits the agent's reply
     as the tool result so the realtime model can speak it. It runs off the
     audio pump so the model can keep talking (filler, small talk, barge-in)
     while the main agent thinks, rather than the call going silent.
   - ``register_post_call_action`` — queues a follow-up task. When the call
     ends, all queued actions are dispatched as a single synthetic SMS-mode
     turn so the main agent can execute them (send email, create note, etc.).
   - ``edit_post_call_action`` / ``delete_post_call_action`` — modify queued
     after-call work while the call is still live.
   - ``hang_up_call`` — a two-step hangup that lets the model say goodbye
     before closing the phone leg.

The shape mirrors Inkbox's channel-plugin ``RealtimeCallWebSocket``.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:  # pragma: no cover — aiohttp is a core dep on this fork
    aiohttp = None  # type: ignore

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime-2"
DEFAULT_VOICE = "cedar"
AUDIO_FORMAT_TELEPHONY = {"type": "audio/pcmu"}
INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

AGENT_CONSULT_TOOL_NAME = "hermes_agent_consult"
POST_CALL_ACTION_TOOL_NAME = "register_post_call_action"
EDIT_POST_CALL_ACTION_TOOL_NAME = "edit_post_call_action"
DELETE_POST_CALL_ACTION_TOOL_NAME = "delete_post_call_action"
HANG_UP_CALL_TOOL_NAME = "hang_up_call"

# How long to wait for the agent_consult tool to complete before giving up and
# returning an error tool result. The realtime model is sitting idle while this
# runs; longer values risk dead air, shorter values cut off legitimate work.
DEFAULT_CONSULT_TIMEOUT_S = 60.0

# A hang_up_call is a two-step confirm: the first call arms the hangup and asks
# the model to say goodbye; a second call within this window actually ends the
# call. Past the window, a lone call re-arms (treated as a fresh first attempt).
HANGUP_CONFIRM_WINDOW_S = 60.0

# After the confirmed hang_up_call, keep the phone leg open briefly before
# sending Inkbox the actual hangup frame. This gives already-forwarded goodbye
# audio time to play out instead of being clipped by immediate teardown.
HANGUP_CLOSE_DELAY_S = 2.0

# OpenAI Realtime must be reachable before the Inkbox websocket is accepted in
# raw-media mode. If this preflight fails, the adapter can still accept the
# same phone call with Inkbox STT/TTS enabled.
DEFAULT_CONNECT_TIMEOUT_S = 8.0


def _openai_realtime_ws_headers(bearer: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema definitions
# ─────────────────────────────────────────────────────────────────────────────


def _agent_consult_tool_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": AGENT_CONSULT_TOOL_NAME,
        "description": (
            "Pause the live voice conversation and ask the main Hermes agent to "
            "do tool work that requires the full agent loop (look up an email, "
            "search session history, check the calendar, hit an API, run a "
            "computation, draft a long-form reply, etc.). The result you "
            "receive is the agent's spoken-friendly answer; read it back to "
            "the caller. Use this whenever the caller asks for something that "
            "needs current external data, persistent memory, or a tool call. "
            "Do NOT use it for greetings, small talk, or generic answers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to ask the main agent in plain English. Include "
                        "enough context that the agent can act standalone."
                    ),
                },
            },
            "required": ["query"],
        },
    }


def _post_call_action_tool_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": POST_CALL_ACTION_TOOL_NAME,
        "description": (
            "Register work the main Hermes agent must do after this phone "
            "call ends — send an email/SMS follow-up, create a note, update "
            "a contact, etc. Tell the caller the action is queued for after "
            "the call; do NOT claim it's already done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Plain-English task for the main agent. Include the "
                        "channel, recipient, and outcome."
                    ),
                },
                "details": {
                    "type": "string",
                    "description": "Optional draft text, hints, or constraints.",
                },
            },
            "required": ["action"],
        },
    }


def _edit_post_call_action_tool_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": EDIT_POST_CALL_ACTION_TOOL_NAME,
        "description": (
            "Edit work previously registered for after this phone call ends. "
            "Use the one-based action_index returned by register_post_call_action "
            "when the caller changes the recipient, channel, wording, or scope."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_index": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "One-based index of the queued post-call action to edit.",
                },
                "action": {
                    "type": "string",
                    "description": "Replacement plain-English task. Omit to keep the current task.",
                },
                "details": {
                    "type": "string",
                    "description": (
                        "Replacement optional draft text, hints, or constraints. "
                        "Pass an empty string to clear details."
                    ),
                },
            },
            "required": ["action_index"],
        },
    }


def _delete_post_call_action_tool_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": DELETE_POST_CALL_ACTION_TOOL_NAME,
        "description": (
            "Delete work previously registered for after this phone call ends. "
            "Use this when the caller cancels a queued follow-up."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_index": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "One-based index of the queued post-call action to delete.",
                },
            },
            "required": ["action_index"],
        },
    }


def _hang_up_call_tool_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": HANG_UP_CALL_TOOL_NAME,
        "description": (
            "End the live phone call. This is a TWO-STEP tool: the first call "
            "does NOT hang up — it prompts you to say a short goodbye. After "
            "you have said goodbye, call hang_up_call a second time to actually "
            "end the call. Use it only when the caller asks to hang up, says "
            "goodbye, or the conversation is clearly complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Optional short reason for ending the call.",
                },
            },
            "required": [],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RealtimeCallMeta:
    """Per-call metadata threaded through to tool handlers."""

    call_id: str
    contact_id: str
    contact_name: str
    remote_phone_number: Optional[str]
    direction: str  # "inbound" or "outbound"
    agent_identity_email: Optional[str] = None
    agent_identity_phone: Optional[str] = None
    # True only when a real Inkbox contact resolved. When false, contact_name
    # may be a raw phone number / "unknown" and must NOT be treated as known.
    contact_known: bool = False
    # Full resolved contact record so the model knows who it's talking to
    # without a mid-call lookup.
    contact_emails: List[str] = field(default_factory=list)
    contact_phones: List[str] = field(default_factory=list)
    contact_company: Optional[str] = None
    contact_notes: Optional[str] = None
    outbound_purpose: Optional[str] = None
    outbound_opening: Optional[str] = None
    # Richer outbound-call context loaded from the call-context file. These
    # are the keys the legacy text-mode call handler reads, so realtime calls
    # keep the same "why we called" continuity.
    outbound_reason: Optional[str] = None
    outbound_scheduled_by: Optional[str] = None
    outbound_conversation_summary: Optional[str] = None


@dataclass
class RealtimeConfig:
    """Per-account realtime voice configuration.

    Populated from ``platforms.inkbox.realtime`` in config.yaml, with env
    overrides on a few common fields.
    """

    enabled: bool = False
    api_key: str = ""
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    additional_instructions: str = ""
    consult_timeout_s: float = DEFAULT_CONSULT_TIMEOUT_S
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    fallback_to_inkbox_stt_tts: bool = True
    # ``api.openai.com`` by default; override for Azure / proxies.
    base_url: str = REALTIME_URL

    @property
    def has_credential(self) -> bool:
        return bool(self.api_key)


@dataclass
class _ToolCallEvent:
    name: str
    call_id: str
    arguments_json: str


@dataclass
class RealtimeConsultResult:
    id: str
    request: str
    result: str
    created_at: float
    dedupe_key: Optional[str] = None


@dataclass
class _BridgeState:
    transcript: List[Tuple[str, str]] = field(default_factory=list)
    post_call_actions: List[Dict[str, str]] = field(default_factory=list)
    consult_results: List[RealtimeConsultResult] = field(default_factory=list)
    pending_consult_keys: Dict[str, str] = field(default_factory=dict)
    last_response_id: Optional[str] = None
    closed: bool = False
    greeting_triggered: bool = False
    # Inkbox-assigned stream id from the `start` event; echoed on outbound
    # media / audio_done frames.
    stream_id: Optional[str] = None
    # Monotonic timestamp of the first hang_up_call ("armed"). A second call
    # within HANGUP_CONFIRM_WINDOW_S fires the real hangup. None = not yet armed.
    hangup_armed_at: Optional[float] = None
    # In-flight hermes_agent_consult dispatches. The consult tool runs the full
    # main agent loop (seconds), so it is dispatched as a background task to keep
    # the OpenAI→Inkbox audio pump flowing; we track the tasks here so they can
    # be cancelled when the call tears down.
    consult_tasks: Set["asyncio.Task[None]"] = field(default_factory=set)


# ─────────────────────────────────────────────────────────────────────────────
# Instruction builder
# ─────────────────────────────────────────────────────────────────────────────


def build_realtime_instructions(
    meta: RealtimeCallMeta,
    additional_instructions: str = "",
) -> str:
    """Compose the system prompt sent to the realtime model.

    Gives the model a clear
    identity, caller context, when to call the two tools, and a directive to
    keep replies short and spoken-friendly.
    """
    lines: List[str] = [
        "You are the configured Hermes agent speaking on a live Inkbox phone call.",
        "Use natural, concise spoken replies. Keep most answers to one or two short sentences.",
        "Do not mention implementation details unless the caller asks.",
    ]
    if meta.agent_identity_email:
        lines.append(f"Your email identity: {meta.agent_identity_email}.")
    if meta.agent_identity_phone:
        lines.append(f"Your phone number: {meta.agent_identity_phone}.")
    if meta.remote_phone_number:
        lines.append(f"Caller is calling from: {meta.remote_phone_number}.")
    if meta.contact_known and meta.contact_name and meta.contact_name not in ("unknown", ""):
        lines.append(
            "You already know who this is — do NOT look them up or ask for "
            "details you already have below.",
        )
        lines.append(f"Caller name: {meta.contact_name}.")
        if meta.contact_emails:
            lines.append(f"Caller email(s): {', '.join(meta.contact_emails)}.")
        if meta.contact_phones:
            lines.append(f"Caller phone(s) on file: {', '.join(meta.contact_phones)}.")
        if meta.contact_company:
            lines.append(f"Caller company: {meta.contact_company}.")
        if meta.contact_notes:
            lines.append(f"Notes about the caller: {meta.contact_notes}")
    else:
        lines.append(
            "No matching contact record is loaded — you do NOT know who this is. "
            "Greet them neutrally; you may look them up by phone number if needed.",
        )
    if meta.direction == "outbound":
        if meta.outbound_purpose:
            lines.append(f"This is an outbound call you placed. Purpose: {meta.outbound_purpose}")
        if meta.outbound_reason:
            lines.append(f"Reason for the call: {meta.outbound_reason}")
        if meta.outbound_scheduled_by:
            lines.append(f"This call was scheduled by: {meta.outbound_scheduled_by}")
        if meta.outbound_conversation_summary:
            lines.append(
                f"Summary of the prior conversation that led to this call:\n"
                f"{meta.outbound_conversation_summary}",
            )
        if meta.outbound_opening:
            lines.append(
                f"Preferred opening message (say this naturally as your first turn): "
                f"{meta.outbound_opening}",
            )
        lines.append(
            "For outbound calls, do not open with a generic offer to help. "
            "Start by explaining why you are calling, then ask the next specific question.",
        )
    lines.extend([
        "Do not perform a context lookup before greeting the caller. Do not say you "
        "are waiting on a lookup or checking context.",
        f"If the caller asks for work to happen now during the live call and it needs "
        f"Hermes/Inkbox tools, call {AGENT_CONSULT_TOOL_NAME}. This includes sending "
        f"SMS/email, reading SMS/email/call history, creating notes, updating contacts, "
        f"or checking current workspace/session data.",
        f"If the caller explicitly asks for work to happen after the call, or accepts "
        f"an after-call deferral, call {POST_CALL_ACTION_TOOL_NAME}. Tell the caller "
        f"the action is queued for after the call; do not claim it has already been "
        f"completed.",
        f"If the caller changes or cancels previously queued after-call work, call "
        f"{EDIT_POST_CALL_ACTION_TOOL_NAME} or {DELETE_POST_CALL_ACTION_TOOL_NAME} "
        f"using the action index returned when the work was queued.",
        f"If {AGENT_CONSULT_TOOL_NAME} completes or queues work that matches a "
        f"previously registered after-call action, call {DELETE_POST_CALL_ACTION_TOOL_NAME} "
        f"for that action so it is not executed twice after hangup.",
        f"If the caller asks to hang up, says goodbye, or the conversation is "
        f"clearly complete, call {HANG_UP_CALL_TOOL_NAME}. The first call arms "
        f"hangup and asks you to say goodbye; after the goodbye, call it once "
        f"more to end the phone call.",
        f"Do not call {AGENT_CONSULT_TOOL_NAME} for greetings, caller identity at "
        f"call start, or generic chat.",
    ])
    if additional_instructions.strip():
        lines.append(additional_instructions.strip())
    return "\n".join(lines)


def build_realtime_greeting(meta: RealtimeCallMeta) -> str:
    """Build the instruction for the proactive opening line.

    Realtime calls must not start with silence. Inbound calls get a short
    friendly greeting. Outbound calls lead with the configured opening/purpose
    so the callee immediately knows why we called.
    """
    first_name = ""
    if meta.contact_known and meta.contact_name and meta.contact_name not in ("unknown", ""):
        first_name = meta.contact_name.split()[0]

    if meta.direction == "outbound":
        if meta.outbound_opening:
            return (
                "Open the call by saying this naturally as the very first thing, "
                "with no greeting before it:\n" + meta.outbound_opening
            )
        if meta.outbound_purpose:
            return (
                "Open the call by greeting the person and immediately explaining "
                f"why you are calling: {meta.outbound_purpose}"
            )
        return (
            "Open the call by greeting the person and explaining why you are "
            "calling. Be specific and concise."
        )

    who = first_name if first_name else "there"
    return (
        f"Greet the caller now as the very first thing you say. Say something "
        f"like 'Hi {who}, this is your Hermes agent — how can I help?' Keep it to "
        f"one short sentence and then wait for them to respond."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bridge
# ─────────────────────────────────────────────────────────────────────────────


# Type alias for the agent-consult callback. The bridge calls this when the
# realtime model invokes hermes_agent_consult; the platform supplies an
# implementation that runs a synthetic SMS-mode turn through the main agent
# and returns the agent's reply as plain text.
AgentConsultCallback = Callable[
    [
        RealtimeCallMeta,
        str,
        List[Tuple[str, str]],
        List[Dict[str, str]],
        List[RealtimeConsultResult],
    ],
    Awaitable[str],
]

# Called when the call ends, with the accumulated post-call actions list.
# Platform dispatches them as a synthetic SMS-mode turn to the main agent.
PostCallActionsCallback = Callable[
    [
        RealtimeCallMeta,
        List[Dict[str, str]],
        List[Tuple[str, str]],
        List[RealtimeConsultResult],
    ],
    Awaitable[None],
]

# Called when the realtime call WebSocket has ended, regardless of whether the
# model explicitly registered post-call actions. This mirrors the legacy
# Inkbox STT/TTS path's [call_ended] reflection so commitments made during a
# realtime call can still be followed up safely.
CallEndedCallback = Callable[
    [RealtimeCallMeta, List[Tuple[str, str]]],
    Awaitable[None],
]


class RealtimeBridgeConnectError(Exception):
    """Raised when OpenAI Realtime cannot be opened before Inkbox accept."""

    def __init__(self, cause: Any):
        self.cause = cause
        message = str(cause) if cause is not None else "unknown error"
        super().__init__(f"OpenAI Realtime connect failed: {message}")


@dataclass
class OpenedRealtimeBridge:
    session: Any
    openai_ws: Any
    state: _BridgeState
    config: RealtimeConfig
    meta: RealtimeCallMeta
    _closed: bool = False

    async def run(
        self,
        *,
        inkbox_ws: Any,
        on_agent_consult: AgentConsultCallback,
        on_post_call_actions: PostCallActionsCallback,
        on_call_ended: CallEndedCallback,
    ) -> None:
        """Bridge one already-opened OpenAI Realtime connection to Inkbox."""
        state = self.state
        openai_ws = self.openai_ws
        try:
            inkbox_task = asyncio.create_task(
                _inkbox_to_openai_pump(inkbox_ws, openai_ws, state, self.meta),
                name=f"realtime-inkbox-pump-{self.meta.call_id}",
            )
            openai_task = asyncio.create_task(
                _openai_to_inkbox_pump(
                    openai_ws=openai_ws,
                    inkbox_ws=inkbox_ws,
                    state=state,
                    config=self.config,
                    meta=self.meta,
                    on_agent_consult=on_agent_consult,
                ),
                name=f"realtime-openai-pump-{self.meta.call_id}",
            )

            done, pending = await asyncio.wait(
                {inkbox_task, openai_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                try:
                    exc = task.exception()
                except asyncio.CancelledError:
                    exc = None
                if exc:
                    logger.warning(
                        "[Inkbox realtime] Pump %s raised: %s",
                        task.get_name(),
                        exc,
                    )
        finally:
            state.closed = True
            # The call is over, so any consult still mid-flight can no longer be
            # spoken to the caller — cancel the background tasks and let them
            # settle before we close the sockets.
            await _cancel_consult_tasks(state)
            await self.close()

        await _dispatch_post_call(state, self.meta, on_post_call_actions, on_call_ended)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            await self.openai_ws.close()
        with suppress(Exception):
            await self.session.close()


async def open_inkbox_realtime_bridge(
    *,
    config: RealtimeConfig,
    meta: RealtimeCallMeta,
) -> OpenedRealtimeBridge:
    """Open OpenAI Realtime before the Inkbox websocket commits media mode."""
    if aiohttp is None:
        raise RealtimeBridgeConnectError("aiohttp not available")
    if not config.has_credential:
        raise RealtimeBridgeConnectError("no OpenAI API key configured")

    session = aiohttp.ClientSession()
    openai_ws = None
    try:
        bearer = await _resolve_realtime_bearer(session, config)
        if not bearer:
            raise RealtimeBridgeConnectError("no OpenAI bearer token resolved")

        separator = "&" if "?" in config.base_url else "?"
        url = f"{config.base_url}{separator}{urlencode({'model': config.model})}"
        headers = _openai_realtime_ws_headers(bearer)
        openai_ws = await asyncio.wait_for(
            session.ws_connect(url, headers=headers, heartbeat=30),
            timeout=config.connect_timeout_s,
        )
        await _send_session_update(openai_ws, config, meta)
        return OpenedRealtimeBridge(
            session=session,
            openai_ws=openai_ws,
            state=_BridgeState(),
            config=config,
            meta=meta,
        )
    except RealtimeBridgeConnectError:
        if openai_ws is not None:
            with suppress(Exception):
                await openai_ws.close()
        with suppress(Exception):
            await session.close()
        raise
    except Exception as exc:
        if openai_ws is not None:
            with suppress(Exception):
                await openai_ws.close()
        with suppress(Exception):
            await session.close()
        raise RealtimeBridgeConnectError(exc) from exc


async def run_inkbox_realtime_bridge(
    *,
    inkbox_ws: Any,
    config: RealtimeConfig,
    meta: RealtimeCallMeta,
    on_agent_consult: AgentConsultCallback,
    on_post_call_actions: PostCallActionsCallback,
    on_call_ended: CallEndedCallback,
) -> None:
    """Run the bridge for the duration of one call.

    Returns when either side closes the WebSocket. Caller is responsible for
    accepting ``inkbox_ws`` *with* the correct realtime headers before invoking
    this function — see :func:`accept_realtime_inkbox_ws`.

    Errors are logged; the function does not re-raise so a partial failure
    doesn't crash the gateway's WS handler chain.
    """
    try:
        bridge = await open_inkbox_realtime_bridge(config=config, meta=meta)
    except RealtimeBridgeConnectError as exc:
        logger.error("[Inkbox realtime] Failed to connect to OpenAI Realtime: %s", exc.cause)
        return
    await bridge.run(
        inkbox_ws=inkbox_ws,
        on_agent_consult=on_agent_consult,
        on_post_call_actions=on_post_call_actions,
        on_call_ended=on_call_ended,
    )


async def _cancel_consult_tasks(state: _BridgeState) -> None:
    """Cancel any in-flight background consult tasks and await their teardown."""
    tasks = list(state.consult_tasks)
    state.consult_tasks.clear()
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError, Exception):
            await task


async def _dispatch_post_call(
    state: _BridgeState,
    meta: RealtimeCallMeta,
    on_post_call_actions: "PostCallActionsCallback",
    on_call_ended: "CallEndedCallback",
) -> None:
    """Dispatch exactly one follow-up turn after a call ends."""
    if state.post_call_actions:
        try:
            await on_post_call_actions(
                meta,
                state.post_call_actions,
                list(state.transcript),
                list(state.consult_results),
            )
        except Exception as exc:
            logger.warning("[Inkbox realtime] Post-call action dispatch failed: %s", exc)
    else:
        try:
            await on_call_ended(meta, list(state.transcript))
        except Exception as exc:
            logger.warning("[Inkbox realtime] Call-ended dispatch failed: %s", exc)


async def _resolve_realtime_bearer(session: Any, config: RealtimeConfig) -> str:
    """Return the bearer token to use on the OpenAI Realtime WebSocket."""
    if config.api_key:
        return config.api_key
    return ""


def _normalize_consult_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+]+", " ", value.lower())).strip()


def _quoted_consult_text(value: str) -> Optional[str]:
    match = re.search(r'["“]([^"”]{8,280})["”]', value)
    if not match:
        return None
    normalized = _normalize_consult_text(match.group(1))
    return normalized or None


def _realtime_consult_dedupe_key(request: str) -> Optional[str]:
    normalized = _normalize_consult_text(request)
    phone_match = re.search(r"\+\d{8,15}", normalized)
    is_sms = re.search(r"\b(sms|text|message)\b", normalized) is not None
    if not phone_match or not is_sms:
        return None
    quoted = _quoted_consult_text(request) or "generic"
    return f"sms:{phone_match.group(0)}:{quoted}"


def _realtime_consult_allows_repeat(request: str) -> bool:
    return re.search(r"\b(again|another|different|new|repeat|second)\b", request, re.I) is not None


def _consult_result_text(output: Dict[str, Any]) -> str:
    result = output.get("answer") or output.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    error = output.get("error")
    if isinstance(error, str) and error.strip():
        return f"ERROR: {error.strip()}"
    return json.dumps(output)


async def _maybe_send_greeting(
    openai_ws: Any, state: _BridgeState, meta: RealtimeCallMeta,
) -> None:
    """Fire the proactive opening line once, so calls don't start with silence."""
    if state.greeting_triggered:
        return
    state.greeting_triggered = True
    try:
        await openai_ws.send_str(json.dumps({
            "type": "response.create",
            "response": {"instructions": build_realtime_greeting(meta)},
        }))
        logger.info(
            "[Inkbox realtime] greeting sent for call_id=%s direction=%s",
            meta.call_id, meta.direction,
        )
    except Exception as exc:
        logger.debug("[Inkbox realtime] greeting send failed: %s", exc)


async def _send_session_update(
    openai_ws: Any, config: RealtimeConfig, meta: RealtimeCallMeta,
) -> None:
    """Send the initial ``session.update`` to configure the OpenAI Realtime session."""
    instructions = build_realtime_instructions(meta, config.additional_instructions)
    payload = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": config.model,
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": AUDIO_FORMAT_TELEPHONY,
                    "noise_reduction": None,
                    "transcription": {"model": INPUT_TRANSCRIPTION_MODEL},
                    # Server-side VAD with default settings — the model
                    # auto-detects caller speech start/stop and decides when
                    # to respond. The bridge does NOT manually trigger
                    # response.create for each turn.
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": AUDIO_FORMAT_TELEPHONY,
                    "voice": config.voice,
                },
            },
            "tools": [
                _agent_consult_tool_schema(),
                _post_call_action_tool_schema(),
                _edit_post_call_action_tool_schema(),
                _delete_post_call_action_tool_schema(),
                _hang_up_call_tool_schema(),
            ],
            "tool_choice": "auto",
        },
    }
    await openai_ws.send_str(json.dumps(payload))


async def _inkbox_to_openai_pump(
    inkbox_ws: Any, openai_ws: Any, state: _BridgeState, meta: RealtimeCallMeta,
) -> None:
    """Forward caller audio from Inkbox to OpenAI; fire the opening greeting.

    Inkbox sends frames as ``{"event": "media", "media": {"payload": "<b64>"}}``.
    We re-emit as ``input_audio_buffer.append``; server-side VAD handles turns.
    The proactive greeting fires once on the ``start`` event, or on first media
    if no ``start`` is sent.
    """
    async for msg in inkbox_ws:
        if state.closed:
            return
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                frame = json.loads(msg.data)
            except (TypeError, ValueError):
                continue
            event = (frame.get("event") or "").lower()
            if event == "start":
                state.stream_id = frame.get("stream_id") or state.stream_id
                await _maybe_send_greeting(openai_ws, state, meta)
            elif event == "media":
                if not state.greeting_triggered:
                    await _maybe_send_greeting(openai_ws, state, meta)
                payload_b64 = (frame.get("media") or {}).get("payload")
                if payload_b64:
                    await openai_ws.send_str(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload_b64,
                    }))
            elif event in {"stop", "closed", "hangup"}:
                logger.info("[Inkbox realtime] Inkbox WS signaled %s", event)
                return
        elif msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
            return


async def _openai_to_inkbox_pump(
    *,
    openai_ws: Any,
    inkbox_ws: Any,
    state: _BridgeState,
    config: RealtimeConfig,
    meta: RealtimeCallMeta,
    on_agent_consult: AgentConsultCallback,
) -> None:
    """Forward audio + handle tool calls from OpenAI back to Inkbox."""
    # Function-call accumulation keyed by item_id: {call_id, name, args}.
    # The name is reliably present on ``response.output_item.added``; args
    # stream via ``...arguments.delta`` and finalize on ``...arguments.done``.
    # We also accept the completed function_call item on
    # ``response.output_item.done`` / ``conversation.item.done`` as a fallback,
    # and dedupe by call_id so a call is dispatched at most once.
    fn_calls: Dict[str, Dict[str, str]] = {}
    dispatched: set = set()

    async def _finalize_fn_call(entry: Dict[str, str]) -> None:
        cid = (entry or {}).get("call_id") or ""
        if not cid or cid in dispatched:
            return
        dispatched.add(cid)
        name = entry.get("name") or ""
        coro = _dispatch_tool_call(
            openai_ws=openai_ws,
            call_id=cid,
            name=name,
            arguments_json=entry.get("args") or "{}",
            state=state,
            config=config,
            meta=meta,
            on_agent_consult=on_agent_consult,
            inkbox_ws=inkbox_ws,
        )
        # hermes_agent_consult runs the full main agent loop, which can take many
        # seconds. Awaiting it inline here would block this read loop for the
        # whole consult — no audio deltas (not even the "one moment" filler) and
        # no barge-in would reach the caller, so the agent appears to go silent.
        # Dispatch it as a background task instead; the pump keeps streaming
        # audio while the agent thinks, and the task submits the tool result when
        # it finishes (this is exactly the async-tool flow gpt-realtime expects).
        # Every other tool is an instant in-memory op, so it stays inline.
        if name == AGENT_CONSULT_TOOL_NAME:
            task = asyncio.create_task(coro, name=f"realtime-consult-{cid}")
            state.consult_tasks.add(task)
            task.add_done_callback(state.consult_tasks.discard)
        else:
            await coro

    async def _relay_transcript(party: str, text: str) -> None:
        # Realtime runs the WS in raw-media mode (OpenAI does STT/TTS, Inkbox
        # does neither — see ``_prepare_call_ws`` in adapter.py), so the platform
        # never records a transcript on its own. Mirror each finalized turn back
        # as a client ``transcript`` event so it lands in the Inkbox call record.
        # party: local=agent, remote=caller.
        with suppress(Exception):
            await inkbox_ws.send_str(json.dumps({
                "event": "transcript",
                "party": party,
                "text": text,
                "is_final": True,
            }))

    async for msg in openai_ws:
        if state.closed:
            return
        if msg.type != aiohttp.WSMsgType.TEXT:
            if msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                return
            continue
        try:
            frame = json.loads(msg.data)
        except (TypeError, ValueError):
            continue
        if not isinstance(frame, dict):
            continue
        ftype = frame.get("type", "")

        # GA emits ``response.output_audio.delta``; beta ``response.audio.delta``.
        if ftype in ("response.output_audio.delta", "response.audio.delta"):
            # Already μ-law base64. Forward as an outbound Inkbox media frame,
            # echoing the stream_id and tagging the track per the Inkbox media
            # protocol.
            delta_b64 = frame.get("delta") or ""
            if delta_b64:
                out = {
                    "event": "media",
                    "media": {"payload": delta_b64, "track": "outbound"},
                }
                if state.stream_id:
                    out["stream_id"] = state.stream_id
                try:
                    await inkbox_ws.send_str(json.dumps(out))
                except Exception as exc:
                    logger.debug("[Inkbox realtime] Inkbox WS send failed: %s", exc)
                    return

        # Outbound audio for a response finished — tell Inkbox to flush/play.
        elif ftype in ("response.output_audio.done", "response.audio.done"):
            done = {"event": "audio_done"}
            if state.stream_id:
                done["stream_id"] = state.stream_id
            try:
                await inkbox_ws.send_str(json.dumps(done))
            except Exception:
                pass

        # Caller started speaking (barge-in) — drop any queued outbound audio.
        elif ftype == "input_audio_buffer.speech_started":
            try:
                await inkbox_ws.send_str(json.dumps({"event": "clear"}))
            except Exception:
                pass

        # GA: response.output_audio_transcript.done; beta: response.audio_transcript.done
        elif ftype in (
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        ):
            text = (frame.get("transcript") or "").strip()
            if text:
                state.transcript.append(("agent", text))
                await _relay_transcript("local", text)

        elif ftype == "conversation.item.input_audio_transcription.completed":
            text = (frame.get("transcript") or "").strip()
            if text:
                state.transcript.append(("caller", text))
                await _relay_transcript("remote", text)

        # Function-call item announced — capture name/call_id by item_id.
        elif ftype == "response.output_item.added":
            item = frame.get("item") or {}
            if item.get("type") == "function_call":
                iid = item.get("id") or frame.get("item_id") or ""
                if iid:
                    fn_calls[iid] = {
                        "call_id": item.get("call_id") or "",
                        "name": item.get("name") or "",
                        "args": item.get("arguments") or "",
                    }

        elif ftype == "response.function_call_arguments.delta":
            key = frame.get("item_id") or frame.get("call_id") or ""
            entry = fn_calls.setdefault(key, {"call_id": "", "name": "", "args": ""})
            if not entry.get("call_id") and frame.get("call_id"):
                entry["call_id"] = frame["call_id"]
            entry["args"] = (entry.get("args") or "") + (frame.get("delta") or "")

        elif ftype == "response.function_call_arguments.done":
            key = frame.get("item_id") or frame.get("call_id") or ""
            entry = fn_calls.get(key) or fn_calls.get(frame.get("call_id") or "") or {}
            if frame.get("call_id"):
                entry["call_id"] = frame["call_id"]
            if frame.get("name"):
                entry["name"] = frame["name"]
            if frame.get("arguments"):
                entry["args"] = frame["arguments"]
            await _finalize_fn_call(entry)

        # Fallback: a completed function_call item we haven't dispatched yet.
        elif ftype in ("response.output_item.done", "conversation.item.done"):
            item = frame.get("item") or {}
            if item.get("type") == "function_call":
                await _finalize_fn_call({
                    "call_id": item.get("call_id") or "",
                    "name": item.get("name") or "",
                    "args": item.get("arguments") or "",
                })

        elif ftype == "response.done":
            resp = frame.get("response") or {}
            rid = resp.get("id")
            if rid:
                state.last_response_id = rid

        elif ftype == "error":
            err = frame.get("error") or frame
            logger.warning("[Inkbox realtime] OpenAI error event: %s", err)

        # Other event types (session.created, session.updated,
        # rate_limits.updated, …) are ignored.


async def _dispatch_tool_call(
    *,
    openai_ws: Any,
    call_id: str,
    name: str,
    arguments_json: str,
    state: _BridgeState,
    config: RealtimeConfig,
    meta: RealtimeCallMeta,
    on_agent_consult: AgentConsultCallback,
    inkbox_ws: Any = None,
) -> None:
    """Handle a function-call event from the realtime model."""
    try:
        args = json.loads(arguments_json or "{}")
    except (TypeError, ValueError):
        args = {}

    if name == POST_CALL_ACTION_TOOL_NAME:
        action = (args.get("action") or "").strip()
        if not action:
            await _submit_tool_result(
                openai_ws, call_id, {"error": "missing action argument"},
            )
            return
        state.post_call_actions.append({
            "action": action,
            "details": (args.get("details") or "").strip(),
        })
        await _submit_tool_result(openai_ws, call_id, {
            "status": "queued",
            "action_index": len(state.post_call_actions),
            "action_count": len(state.post_call_actions),
            "message": (
                "Action queued for after the call. Tell the caller the action "
                "is queued; do not claim it is already done."
            ),
        })
        return

    if name == EDIT_POST_CALL_ACTION_TOOL_NAME:
        raw_index = args.get("action_index")
        try:
            action_index = int(raw_index)
        except (TypeError, ValueError):
            action_index = 0
        if action_index < 1 or action_index > len(state.post_call_actions):
            await _submit_tool_result(openai_ws, call_id, {
                "error": "invalid action_index",
                "action_count": len(state.post_call_actions),
            })
            return

        has_action = "action" in args
        has_details = "details" in args
        if not has_action and not has_details:
            await _submit_tool_result(openai_ws, call_id, {
                "error": "missing action or details argument",
            })
            return

        queued = state.post_call_actions[action_index - 1]
        if has_action:
            new_action = (args.get("action") or "").strip()
            if not new_action:
                await _submit_tool_result(openai_ws, call_id, {
                    "error": "action cannot be empty",
                })
                return
            queued["action"] = new_action
        if has_details:
            queued["details"] = (args.get("details") or "").strip()

        await _submit_tool_result(openai_ws, call_id, {
            "status": "updated",
            "action_index": action_index,
            "action_count": len(state.post_call_actions),
            "action": queued,
            "message": (
                "Queued after-call action updated. If the caller needs to know, "
                "confirm briefly that the queued work was changed."
            ),
        })
        return

    if name == DELETE_POST_CALL_ACTION_TOOL_NAME:
        raw_index = args.get("action_index")
        try:
            action_index = int(raw_index)
        except (TypeError, ValueError):
            action_index = 0
        if action_index < 1 or action_index > len(state.post_call_actions):
            await _submit_tool_result(openai_ws, call_id, {
                "error": "invalid action_index",
                "action_count": len(state.post_call_actions),
            })
            return

        deleted = state.post_call_actions.pop(action_index - 1)
        await _submit_tool_result(openai_ws, call_id, {
            "status": "deleted",
            "deleted_action": deleted,
            "action_index": action_index,
            "action_count": len(state.post_call_actions),
            "remaining_actions": list(state.post_call_actions),
            "message": (
                "Queued after-call action deleted. If the caller needs to know, "
                "confirm briefly that it was canceled."
            ),
        })
        return

    if name == HANG_UP_CALL_TOOL_NAME:
        if inkbox_ws is None:
            await _submit_tool_result(openai_ws, call_id, {
                "error": "hangup unavailable without Inkbox websocket",
            })
            return

        now = time.monotonic()
        armed = state.hangup_armed_at
        # First attempt (or a stale arm past the window) → arm the hangup and
        # have the model say goodbye instead of dropping the line mid-farewell.
        if armed is None or (now - armed) > HANGUP_CONFIRM_WINDOW_S:
            state.hangup_armed_at = now
            # Default create_response=True so the model speaks the goodbye.
            await _submit_tool_result(openai_ws, call_id, {
                "status": "confirm_goodbye",
                "message": (
                    "Don't hang up yet. Say a brief, natural goodbye to the "
                    "caller now, then call hang_up_call once more to actually "
                    "end the call."
                ),
            })
            return

        # Second attempt within the window → perform the real hangup.
        reason = (args.get("reason") or "").strip()
        hangup_frame: Dict[str, Any] = {"event": "hangup"}
        if reason:
            hangup_frame["reason"] = reason
        if state.stream_id:
            hangup_frame["stream_id"] = state.stream_id

        await _submit_tool_result(
            openai_ws,
            call_id,
            {
                "status": "hangup_requested",
                "reason": reason,
                "message": "The call is ending now.",
            },
            create_response=False,
        )
        try:
            await asyncio.sleep(HANGUP_CLOSE_DELAY_S)
            await inkbox_ws.send_str(json.dumps(hangup_frame))
        except Exception as exc:
            logger.debug("[Inkbox realtime] hangup frame send failed: %s", exc)
        state.closed = True
        await _maybe_close_ws(inkbox_ws)
        await _maybe_close_ws(openai_ws)
        return

    if name == AGENT_CONSULT_TOOL_NAME:
        query = (args.get("query") or "").strip()
        if not query:
            await _submit_tool_result(
                openai_ws, call_id, {"error": "missing query argument"},
            )
            return

        consult_key = _realtime_consult_dedupe_key(query)
        if consult_key and not _realtime_consult_allows_repeat(query):
            pending_call_id = state.pending_consult_keys.get(consult_key)
            if pending_call_id:
                await _submit_tool_result(openai_ws, call_id, {
                    "status": "already_running",
                    "existing_tool_call_id": pending_call_id,
                    "answer": (
                        "Hermes is already handling this same in-call request. "
                        "Do not call the consult tool again or queue a duplicate "
                        "post-call action; wait briefly for the existing result."
                    ),
                })
                return
            completed = next(
                (
                    entry
                    for entry in reversed(state.consult_results)
                    if entry.dedupe_key == consult_key
                ),
                None,
            )
            if completed:
                await _submit_tool_result(openai_ws, call_id, {
                    "status": "already_handled",
                    "existing_tool_call_id": completed.id,
                    "answer": (
                        "Hermes already handled this same in-call request: "
                        f"{completed.result}. Do not send it again unless the "
                        "caller explicitly asks for another, repeat, or different message."
                    ),
                })
                return

        # OpenAI Realtime doesn't have a native "I'll continue later" mechanism
        # for one tool call, but we can ALSO inject an interim instruction so
        # the model says "one moment" while the agent thinks. The final tool
        # result is what the model uses to compose the actual spoken answer.
        try:
            # Override instructions for just this turn so the model says a
            # short filler line while the agent runs. No modalities field —
            # it inherits the session's output_modalities (GA rejects
            # output_modalities inside response.create).
            await openai_ws.send_str(json.dumps({
                "type": "response.create",
                "response": {
                    "instructions": (
                        "Say only 'One moment.' Do not mention waiting for "
                        "context or checking a lookup."
                    ),
                },
            }))
        except Exception:
            # The interim cue is best-effort; the final tool result is
            # authoritative.
            pass

        if consult_key:
            state.pending_consult_keys[consult_key] = call_id
        try:
            answer = await asyncio.wait_for(
                on_agent_consult(
                    meta,
                    query,
                    list(state.transcript),
                    list(state.post_call_actions),
                    list(state.consult_results),
                ),
                timeout=config.consult_timeout_s,
            )
        except asyncio.TimeoutError:
            output = {
                "error": "agent_consult timed out",
                "message": (
                    "Tell the caller you couldn't get an answer right now. "
                    "Offer to follow up after the call."
                ),
            }
            state.consult_results.append(RealtimeConsultResult(
                id=call_id,
                request=query,
                result=_consult_result_text(output),
                created_at=time.time(),
                dedupe_key=consult_key,
            ))
            if consult_key and state.pending_consult_keys.get(consult_key) == call_id:
                state.pending_consult_keys.pop(consult_key, None)
            await _submit_tool_result(openai_ws, call_id, output)
            return
        except Exception as exc:
            logger.warning("[Inkbox realtime] agent_consult failed: %s", exc)
            output = {
                "error": f"agent_consult error: {exc}",
                "message": "Apologize briefly and ask if you can help another way.",
            }
            state.consult_results.append(RealtimeConsultResult(
                id=call_id,
                request=query,
                result=_consult_result_text(output),
                created_at=time.time(),
                dedupe_key=consult_key,
            ))
            if consult_key and state.pending_consult_keys.get(consult_key) == call_id:
                state.pending_consult_keys.pop(consult_key, None)
            await _submit_tool_result(openai_ws, call_id, output)
            return

        output = {
            "status": "ok",
            "answer": answer,
            "instructions": (
                "Read the answer back to the caller in your own spoken voice. "
                "Keep it natural and concise."
            ),
        }
        if state.post_call_actions:
            output["post_call_action_guidance"] = (
                "If this result completed, queued, canceled, or superseded a "
                "pending after-call action, call delete_post_call_action for "
                "that action_index before the call ends."
            )
        state.consult_results.append(RealtimeConsultResult(
            id=call_id,
            request=query,
            result=_consult_result_text(output),
            created_at=time.time(),
            dedupe_key=consult_key,
        ))
        if consult_key and state.pending_consult_keys.get(consult_key) == call_id:
            state.pending_consult_keys.pop(consult_key, None)
        await _submit_tool_result(openai_ws, call_id, output)
        return

    # Unknown tool — refuse politely.
    await _submit_tool_result(openai_ws, call_id, {
        "error": f"Tool '{name}' is not available on live calls.",
    })


async def _submit_tool_result(
    openai_ws: Any,
    call_id: str,
    output: Dict[str, Any],
    *,
    create_response: bool = True,
) -> None:
    """Submit a function call output and trigger a model response.

    The OpenAI Realtime protocol takes function call output via a
    ``conversation.item.create`` event of type ``function_call_output``,
    followed by ``response.create`` so the model speaks based on the result.
    """
    try:
        await openai_ws.send_str(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(output),
            },
        }))
        if not create_response:
            return
        # Bare response.create — let the session's configured output
        # modalities + audio settings apply. Passing a beta-style
        # ``modalities`` field here would be rejected by GA models.
        await openai_ws.send_str(json.dumps({
            "type": "response.create",
        }))
    except Exception as exc:
        logger.debug("[Inkbox realtime] submit_tool_result failed: %s", exc)


async def _maybe_close_ws(ws: Any) -> None:
    close = getattr(ws, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass
