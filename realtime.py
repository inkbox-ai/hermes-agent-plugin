"""Inkbox ↔ OpenAI Realtime API voice bridge.

When ``inkbox.realtime.enabled`` is true and an OpenAI API key or Codex OAuth
token is configured, the call WebSocket handler in :mod:`gateway.platforms.inkbox`
delegates inbound calls to :func:`run_inkbox_realtime_bridge` instead of using
Inkbox-side STT/TTS.

The bridge:

1. Accepts the Inkbox call WS with ``x-use-inkbox-text-to-speech: false`` and
   ``x-use-inkbox-speech-to-text: false`` headers, so Inkbox forwards raw
   G.711 μ-law @ 8 kHz frames in both directions.
2. Opens an OpenAI Realtime API WebSocket
   (``wss://api.openai.com/v1/realtime?model=<model>``) and sends
   ``session.update`` configuring tools, instructions, and the
   ``g711_ulaw`` input/output audio format.
3. Bridges audio bidirectionally: Inkbox → OpenAI as
   ``input_audio_buffer.append`` events, OpenAI → Inkbox as
   ``media`` frames carrying ``response.audio.delta`` payloads.
4. Exposes two tools to the realtime model:

   - ``hermes_agent_consult`` — pauses the conversation, dispatches a synthetic
     SMS-mode turn through Hermes' main agent loop, and submits the agent's
     reply as the tool result so the realtime model can speak it.
   - ``register_post_call_action`` — queues a follow-up task. When the call
     ends, all queued actions are dispatched as a single synthetic SMS-mode
     turn so the main agent can execute them (send email, create note, etc.).

The shape mirrors Inkbox's channel-plugin ``RealtimeCallWebSocket``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:  # pragma: no cover — aiohttp is a core dep on this fork
    aiohttp = None  # type: ignore

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"
REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
DEFAULT_MODEL = "gpt-realtime-2"
DEFAULT_VOICE = "cedar"
AUDIO_FORMAT_TELEPHONY = {"type": "audio/pcmu"}
INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

AGENT_CONSULT_TOOL_NAME = "hermes_agent_consult"
POST_CALL_ACTION_TOOL_NAME = "register_post_call_action"

# How long to wait for the agent_consult tool to complete before giving up and
# returning an error tool result. The realtime model is sitting idle while this
# runs; longer values risk dead air, shorter values cut off legitimate work.
DEFAULT_CONSULT_TIMEOUT_S = 60.0


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
    outbound_purpose: Optional[str] = None
    outbound_opening: Optional[str] = None


@dataclass
class RealtimeConfig:
    """Per-account realtime voice configuration.

    Populated from ``platforms.inkbox.realtime`` in config.yaml, with env
    overrides on a few common fields.
    """

    enabled: bool = False
    api_key: str = ""
    oauth_token: str = ""
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    additional_instructions: str = ""
    consult_timeout_s: float = DEFAULT_CONSULT_TIMEOUT_S
    # ``api.openai.com`` by default; override for Azure / proxies.
    base_url: str = REALTIME_URL

    @property
    def has_credential(self) -> bool:
        return bool(self.api_key or self.oauth_token)


@dataclass
class _ToolCallEvent:
    name: str
    call_id: str
    arguments_json: str


@dataclass
class _BridgeState:
    transcript: List[Tuple[str, str]] = field(default_factory=list)
    post_call_actions: List[Dict[str, str]] = field(default_factory=list)
    last_response_id: Optional[str] = None
    closed: bool = False


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
        lines.append(f"Caller phone number: {meta.remote_phone_number}.")
    if meta.contact_name and meta.contact_name not in ("unknown", ""):
        lines.append(f"Caller name: {meta.contact_name}.")
    else:
        lines.append(
            "No matching contact record is loaded; use the phone number or a neutral greeting.",
        )
    if meta.direction == "outbound":
        if meta.outbound_purpose:
            lines.append(f"This is an outbound call you placed. Purpose: {meta.outbound_purpose}")
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
        f"If the caller asks for work to happen after the call, call "
        f"{POST_CALL_ACTION_TOOL_NAME}. Tell the caller the action is queued for "
        f"after the call; do not claim it has already been completed.",
        f"Call {AGENT_CONSULT_TOOL_NAME} only when the caller asks for current "
        f"external data, session history, a calendar lookup, or other work that "
        f"requires the full Hermes agent. Do not call it for greetings, identity, "
        f"or generic chat.",
    ])
    if additional_instructions.strip():
        lines.append(additional_instructions.strip())
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Bridge
# ─────────────────────────────────────────────────────────────────────────────


# Type alias for the agent-consult callback. The bridge calls this when the
# realtime model invokes hermes_agent_consult; the platform supplies an
# implementation that runs a synthetic SMS-mode turn through the main agent
# and returns the agent's reply as plain text.
AgentConsultCallback = Callable[
    [RealtimeCallMeta, str, List[Tuple[str, str]]],
    Awaitable[str],
]

# Called when the call ends, with the accumulated post-call actions list.
# Platform dispatches them as a synthetic SMS-mode turn to the main agent.
PostCallActionsCallback = Callable[
    [RealtimeCallMeta, List[Dict[str, str]], List[Tuple[str, str]]],
    Awaitable[None],
]


async def run_inkbox_realtime_bridge(
    *,
    inkbox_ws: Any,
    config: RealtimeConfig,
    meta: RealtimeCallMeta,
    on_agent_consult: AgentConsultCallback,
    on_post_call_actions: PostCallActionsCallback,
) -> None:
    """Run the bridge for the duration of one call.

    Returns when either side closes the WebSocket. Caller is responsible for
    accepting ``inkbox_ws`` *with* the correct realtime headers before invoking
    this function — see :func:`accept_realtime_inkbox_ws`.

    Errors are logged; the function does not re-raise so a partial failure
    doesn't crash the gateway's WS handler chain.
    """
    if aiohttp is None:
        logger.error("[Inkbox realtime] aiohttp not available; cannot open Realtime API WS")
        return
    if not config.has_credential:
        logger.error("[Inkbox realtime] No OpenAI credential configured; refusing to bridge")
        return

    state = _BridgeState()
    separator = "&" if "?" in config.base_url else "?"
    url = f"{config.base_url}{separator}{urlencode({'model': config.model})}"

    session = aiohttp.ClientSession()
    try:
        try:
            bearer = await _resolve_realtime_bearer(session, config)
            headers = {"Authorization": f"Bearer {bearer}"}
            openai_ws = await session.ws_connect(url, headers=headers, heartbeat=30)
        except Exception as exc:
            logger.error("[Inkbox realtime] Failed to connect to OpenAI Realtime: %s", exc)
            return

        try:
            await _send_session_update(openai_ws, config, meta)
            # Two concurrent pumps:
            inkbox_task = asyncio.create_task(
                _inkbox_to_openai_pump(inkbox_ws, openai_ws, state),
                name=f"realtime-inkbox-pump-{meta.call_id}",
            )
            openai_task = asyncio.create_task(
                _openai_to_inkbox_pump(
                    openai_ws=openai_ws,
                    inkbox_ws=inkbox_ws,
                    state=state,
                    config=config,
                    meta=meta,
                    on_agent_consult=on_agent_consult,
                ),
                name=f"realtime-openai-pump-{meta.call_id}",
            )

            done, pending = await asyncio.wait(
                {inkbox_task, openai_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc:
                    logger.warning(
                        "[Inkbox realtime] Pump %s raised: %s",
                        task.get_name(),
                        exc,
                    )
        finally:
            state.closed = True
            try:
                await openai_ws.close()
            except Exception:
                pass

        # Dispatch any queued post-call actions outside the call WS lifecycle.
        if state.post_call_actions:
            try:
                await on_post_call_actions(
                    meta, state.post_call_actions, list(state.transcript),
                )
            except Exception as exc:
                logger.warning(
                    "[Inkbox realtime] Post-call action dispatch failed: %s", exc,
                )
    finally:
        await session.close()


async def _resolve_realtime_bearer(session: Any, config: RealtimeConfig) -> str:
    """Return the bearer token to use on the OpenAI Realtime WebSocket."""
    if config.api_key:
        return config.api_key

    body = {
        "session": {
            "type": "realtime",
            "model": config.model,
            "audio": {"output": {"voice": config.voice}},
        },
    }
    headers = {
        "Authorization": f"Bearer {config.oauth_token}",
        "Content-Type": "application/json",
    }
    async with session.post(REALTIME_CLIENT_SECRETS_URL, headers=headers, json=body) as resp:
        if resp.status >= 400:
            detail = (await resp.text())[:200]
            raise RuntimeError(f"client_secrets HTTP {resp.status}: {detail}")
        data = await resp.json()

    secret = data.get("value")
    if not secret and isinstance(data.get("client_secret"), dict):
        secret = data["client_secret"].get("value")
    if not secret:
        raise RuntimeError("client_secrets response had no value")
    return str(secret)


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
            ],
            "tool_choice": "auto",
        },
    }
    await openai_ws.send_str(json.dumps(payload))


async def _inkbox_to_openai_pump(
    inkbox_ws: Any, openai_ws: Any, state: _BridgeState,
) -> None:
    """Forward caller audio frames from Inkbox to the OpenAI Realtime session.

    Inkbox sends each audio frame as a JSON message of the form
    ``{"event": "media", "media": {"payload": "<base64-mulaw>"}}`` over the
    accepted WebSocket. We unwrap and re-emit as
    ``input_audio_buffer.append`` events. With server-side VAD enabled, the
    realtime model auto-detects speech boundaries.
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
            if event == "media":
                payload_b64 = (frame.get("media") or {}).get("payload")
                if payload_b64:
                    await openai_ws.send_str(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload_b64,
                    }))
            elif event in {"stop", "closed", "hangup"}:
                logger.info("[Inkbox realtime] Inkbox WS signaled %s", event)
                return
            # Other Inkbox event types ("start", "mark", ...) are ignored —
            # they're book-keeping for the Inkbox side, not for the model.
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
    # Accumulator for streaming function call arguments. The Realtime API
    # delivers args as a sequence of `response.function_call_arguments.delta`
    # events terminated by `response.function_call_arguments.done`; we collect
    # by call_id then dispatch on done.
    pending_calls: Dict[str, Dict[str, str]] = {}
    dispatched_tool_items: set[str] = set()

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

        if ftype in {
            "conversation.output_audio.delta",
            "response.audio.delta",
            "response.output_audio.delta",
        }:
            # Audio bytes are already μ-law base64. Forward as an Inkbox media
            # frame. Streaming is handled by the realtime model's pacing; we
            # don't need our own jitter buffer for telephony @ 8 kHz.
            delta_b64 = frame.get("delta") or frame.get("data") or ""
            if delta_b64:
                try:
                    await inkbox_ws.send_str(json.dumps({
                        "event": "media",
                        "media": {"payload": delta_b64},
                    }))
                except Exception as exc:
                    logger.debug("[Inkbox realtime] Inkbox WS send failed: %s", exc)
                    return

        elif ftype in {
            "response.output_text.done",
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
        }:
            text = (frame.get("transcript") or frame.get("text") or "").strip()
            if text:
                state.transcript.append(("agent", text))

        elif ftype == "conversation.item.input_audio_transcription.completed":
            text = (frame.get("transcript") or "").strip()
            if text:
                state.transcript.append(("caller", text))

        elif ftype == "response.function_call_arguments.delta":
            item_id = frame.get("item_id") or frame.get("call_id") or ""
            delta = frame.get("delta") or ""
            if item_id:
                pending = pending_calls.setdefault(
                    item_id,
                    {"name": "", "call_id": "", "args": ""},
                )
                pending["args"] += delta
                if frame.get("name"):
                    pending["name"] = str(frame.get("name"))
                if frame.get("call_id"):
                    pending["call_id"] = str(frame.get("call_id"))

        elif ftype == "response.function_call_arguments.done":
            item_id = str(frame.get("item_id") or frame.get("call_id") or "")
            if item_id and item_id in dispatched_tool_items:
                continue
            buffered = pending_calls.pop(item_id, {})
            call_id = str(frame.get("call_id") or buffered.get("call_id") or item_id)
            name = str(frame.get("name") or buffered.get("name") or "")
            args_json = (
                frame.get("arguments")
                or buffered.get("args")
                or "{}"
            )
            if item_id:
                dispatched_tool_items.add(item_id)
            await _dispatch_tool_call(
                openai_ws=openai_ws,
                call_id=call_id,
                name=name,
                arguments_json=args_json,
                state=state,
                config=config,
                meta=meta,
                on_agent_consult=on_agent_consult,
            )

        elif ftype == "conversation.item.done":
            item = frame.get("item") or {}
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            item_id = str(
                item.get("id")
                or frame.get("item_id")
                or item.get("call_id")
                or frame.get("call_id")
                or ""
            )
            if item_id and item_id in dispatched_tool_items:
                continue
            call_id = str(
                item.get("call_id")
                or frame.get("call_id")
                or item.get("id")
                or item_id
            )
            name = str(item.get("name") or frame.get("name") or "")
            args_json = str(item.get("arguments") or frame.get("arguments") or "{}")
            if item_id:
                dispatched_tool_items.add(item_id)
            await _dispatch_tool_call(
                openai_ws=openai_ws,
                call_id=call_id,
                name=name,
                arguments_json=args_json,
                state=state,
                config=config,
                meta=meta,
                on_agent_consult=on_agent_consult,
            )

        elif ftype == "response.done":
            resp = frame.get("response") or {}
            rid = resp.get("id")
            if rid:
                state.last_response_id = rid

        elif ftype == "error":
            err = frame.get("error") or frame
            logger.warning("[Inkbox realtime] OpenAI error event: %s", err)

        # All other event types (session.created, session.updated,
        # rate_limits.updated, response.output_item.added, …) are ignored.


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
            "action_count": len(state.post_call_actions),
            "message": (
                "Action queued for after the call. Tell the caller the action "
                "is queued; do not claim it is already done."
            ),
        })
        return

    if name == AGENT_CONSULT_TOOL_NAME:
        query = (args.get("query") or "").strip()
        if not query:
            await _submit_tool_result(
                openai_ws, call_id, {"error": "missing query argument"},
            )
            return

        # OpenAI Realtime doesn't have a native "I'll continue later" mechanism
        # for one tool call, but we can ALSO inject an interim instruction so
        # the model says "one moment" while the agent thinks. The final tool
        # result is what the model uses to compose the actual spoken answer.
        try:
            await openai_ws.send_str(json.dumps({
                "type": "response.create",
                "response": {
                    "output_modalities": ["audio"],
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

        try:
            answer = await asyncio.wait_for(
                on_agent_consult(meta, query, list(state.transcript)),
                timeout=config.consult_timeout_s,
            )
        except asyncio.TimeoutError:
            await _submit_tool_result(openai_ws, call_id, {
                "error": "agent_consult timed out",
                "message": (
                    "Tell the caller you couldn't get an answer right now. "
                    "Offer to follow up after the call."
                ),
            })
            return
        except Exception as exc:
            logger.warning("[Inkbox realtime] agent_consult failed: %s", exc)
            await _submit_tool_result(openai_ws, call_id, {
                "error": f"agent_consult error: {exc}",
                "message": "Apologize briefly and ask if you can help another way.",
            })
            return

        await _submit_tool_result(openai_ws, call_id, {
            "status": "ok",
            "answer": answer,
            "instructions": (
                "Read the answer back to the caller in your own spoken voice. "
                "Keep it natural and concise."
            ),
        })
        return

    # Unknown tool — refuse politely.
    await _submit_tool_result(openai_ws, call_id, {
        "error": f"Tool '{name}' is not available on live calls.",
    })


async def _submit_tool_result(
    openai_ws: Any, call_id: str, output: Dict[str, Any],
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
        await openai_ws.send_str(json.dumps({
            "type": "response.create",
        }))
    except Exception as exc:
        logger.debug("[Inkbox realtime] submit_tool_result failed: %s", exc)
