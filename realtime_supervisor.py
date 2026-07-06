"""Proactive supervisor loop for Inkbox realtime voice calls.

The base realtime bridge (:mod:`realtime`) is *pull-only*: the OpenAI Realtime
voice model — a fast, low-latency "mouth" — drives the whole call, and the
heavier main Hermes agent (the "brain") only engages when the voice model
chooses to invoke the ``consult_agent`` tool. If the voice model is confidently
wrong, forgets a piece of loaded context, or simply doesn't realize it needs
help, the brain never engages and the call quality suffers.

This module adds the missing *push* channel: a background **supervisor loop**
that runs concurrently with the two audio pumps, watches the call transcript as
it accumulates, and — on its own initiative — injects steering guidance into the
live OpenAI Realtime session. It closes the loop between the fast speaking model
and the smart backend so the brain can:

  * receive regular transcript updates as the call proceeds (streaming), and
  * proactively interject to add context or correct/redirect the voice model,
    without waiting to be asked.

Three-tier model split (fast → mid → smart):

  1. **Mouth** — the OpenAI Realtime voice model. Owns turn-taking, backchannel,
     and speech. Cheapest per token, lowest latency, least capable at reasoning.
  2. **Supervisor** — a cheaper *reasoning* model (this module's callback).
     Runs once per caller turn (debounced), reads the running transcript + call
     context, and decides whether the mouth needs a nudge. New tier.
  3. **Brain** — the full main Hermes agent (``consult_agent`` / post-call
     actions). Heavy; only runs when real tool work is required. Existing, but
     the supervisor can now trigger it *proactively* by steering the mouth to
     consult, instead of relying on the mouth to notice on its own.

Injection mechanism (OpenAI Realtime, over the same WebSocket):

  * **Silent steer** — inject a ``conversation.item.create`` message item
    carrying a bracketed supervisor note. No ``response.create`` follows, so the
    model absorbs the note and applies it on its next natural turn. Used to add
    context or gently redirect without interrupting.
  * **Speak-now interject** — inject the same note *and* a ``response.create`` so
    the model speaks immediately (e.g. to correct a fact it just stated wrong).

The supervisor is deliberately conservative: it is rate-limited, debounced, and
defaults to doing nothing. A note is only injected when the callback returns an
explicit ``steer`` or ``interject`` decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Role used for injected supervisor items. The OpenAI Realtime API accepts
# "system" message items mid-session as out-of-band steering that is not spoken
# by itself; the model treats it as an authoritative instruction for subsequent
# turns. Kept as a module constant so it is trivial to switch to "user"/
# "developer" if a future model family narrows accepted roles.
SUPERVISOR_ITEM_ROLE = "system"

# Every injected note is prefixed so the model (and the call transcript) can tell
# supervisor guidance apart from caller speech.
SUPERVISOR_NOTE_PREFIX = "[SUPERVISOR]"

# Decision verbs the supervisor callback may return.
ACTION_NONE = "none"
ACTION_STEER = "steer"          # inject a note; model uses it on its next turn
ACTION_INTERJECT = "interject"  # inject a note AND make the model speak now
_VALID_ACTIONS = frozenset({ACTION_NONE, ACTION_STEER, ACTION_INTERJECT})


# ─────────────────────────────────────────────────────────────────────────────
# Config + decision types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SupervisorConfig:
    """Per-call supervisor tuning.

    Populated from ``platforms.inkbox.realtime.supervisor`` in config.yaml.
    Defaults are conservative: the supervisor stays quiet unless it has a
    concrete reason to speak, and it never dominates the call.
    """

    enabled: bool = False
    # Which brain runs a review. "hermes" (default) is the real main agent via
    # ``hermes -z`` — it has tools, so it can VERIFY facts against live data and
    # catch tool-grounded errors a context-only model cannot. "model" is a cheap
    # chat-completions proxy: fast and cheap, but no tools/live data, so it only
    # catches guardrail/consistency problems. The loop is backend-agnostic; the
    # adapter maps this field to the matching callback.
    backend: str = "hermes"
    # Wait this long for the caller's turn to settle before reviewing, so we
    # review once per *thought* rather than per transcription fragment.
    debounce_s: float = 1.2
    # How long the supervisor model may take before we abandon a review. The
    # call keeps flowing regardless — a slow review just means no nudge.
    review_timeout_s: float = 12.0
    # Floor on the gap between reviews, as a backstop against pathological
    # rapid-fire. 0 = review every settled turn (the debounce already coalesces
    # fragments within a turn), which is what "regular updates as the call
    # proceeds" wants; raise it to trade responsiveness for fewer model calls.
    min_review_interval_s: float = 0.0
    # Hard cap on total injected notes per call. Prevents a misbehaving
    # supervisor from turning the call into a lecture.
    max_interjections: int = 8
    # Don't review until at least this many caller turns exist, so the opening
    # exchange (greeting + first ask) isn't second-guessed prematurely.
    min_caller_turns: int = 1
    # Idle wake interval for the loop so it notices call teardown promptly even
    # when no transcript events arrive.
    poll_interval_s: float = 0.5


@dataclass
class SupervisorDecision:
    """The supervisor's verdict for one review pass.

    ``action`` is one of the ``ACTION_*`` constants. ``guidance`` is the short,
    spoken-context note to inject (ignored when ``action`` is ``none``).
    ``reason`` is optional and only logged — never injected.
    """

    action: str = ACTION_NONE
    guidance: str = ""
    reason: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.action in (ACTION_STEER, ACTION_INTERJECT) and bool(self.guidance.strip())


# The supervisor callback: given call metadata, a snapshot of the transcript so
# far (list of (party, text) where party is "caller"/"agent"), and the notes
# already injected this call, return a decision. The platform supplies an
# implementation that runs a cheap reasoning model. Returning ACTION_NONE (or
# raising) leaves the call untouched.
SuperviseCallback = Callable[
    [Any, List[Tuple[str, str]], List[str]],
    Awaitable[SupervisorDecision],
]


# ─────────────────────────────────────────────────────────────────────────────
# Decision parsing
# ─────────────────────────────────────────────────────────────────────────────

# A text-returning agent (e.g. `hermes -z`, or a chat-completions call) is the
# expected backend, so we accept several shapes and normalize them. Preferred is
# a JSON object; we also accept a "VERB: guidance" line and bare NONE/[SILENT].
_LEADING_VERB = re.compile(
    r"^\s*(none|silent|steer|interject|speak)\b[:\-\s]*",
    re.IGNORECASE,
)
_SILENT_MARKERS = {"none", "silent", "[silent]", "no", "n/a", ""}


def parse_supervisor_decision(raw: Any) -> SupervisorDecision:
    """Parse a supervisor backend's reply into a :class:`SupervisorDecision`.

    Robust to three shapes so the callback contract stays simple:

    * a :class:`SupervisorDecision` (passed straight through),
    * a JSON object ``{"action": "...", "guidance": "...", "reason": "..."}``,
    * plain text: a leading ``STEER:``/``INTERJECT:`` verb, or ``NONE``/
      ``[SILENT]``/empty for "do nothing".
    """
    if isinstance(raw, SupervisorDecision):
        return _normalize_decision(raw)
    if raw is None:
        return SupervisorDecision(action=ACTION_NONE)

    text = raw if isinstance(raw, str) else str(raw)
    stripped = text.strip()
    if not stripped:
        return SupervisorDecision(action=ACTION_NONE)

    # Try JSON first (possibly wrapped in ```json fences).
    json_obj = _extract_json_object(stripped)
    if json_obj is not None:
        return _normalize_decision(SupervisorDecision(
            action=str(json_obj.get("action") or ACTION_NONE),
            guidance=str(json_obj.get("guidance") or ""),
            reason=str(json_obj.get("reason") or ""),
        ))

    # Looks like JSON but didn't parse (e.g. a max_tokens-truncated object). Do
    # NOT fall through to the freeform-steer path — injecting a half-JSON blob as
    # a spoken note would be worse than staying silent.
    if stripped.startswith("{") or stripped.startswith("```"):
        return SupervisorDecision(action=ACTION_NONE)

    # Plain-text convention.
    if stripped.lower() in _SILENT_MARKERS:
        return SupervisorDecision(action=ACTION_NONE)
    verb_match = _LEADING_VERB.match(stripped)
    if verb_match:
        verb = verb_match.group(1).lower()
        guidance = stripped[verb_match.end():].strip()
        if verb in ("none", "silent"):
            return SupervisorDecision(action=ACTION_NONE)
        action = ACTION_INTERJECT if verb in ("interject", "speak") else ACTION_STEER
        return _normalize_decision(SupervisorDecision(action=action, guidance=guidance))

    # No recognized verb but non-empty text → treat the whole thing as a silent
    # steer note. This is the safe default: add context without interrupting.
    return _normalize_decision(SupervisorDecision(action=ACTION_STEER, guidance=stripped))


def _normalize_decision(decision: SupervisorDecision) -> SupervisorDecision:
    action = (decision.action or ACTION_NONE).strip().lower()
    if action == "speak":
        action = ACTION_INTERJECT
    if action not in _VALID_ACTIONS:
        action = ACTION_NONE
    guidance = (decision.guidance or "").strip()
    if action != ACTION_NONE and not guidance:
        action = ACTION_NONE
    return SupervisorDecision(action=action, guidance=guidance, reason=(decision.reason or "").strip())


def _extract_json_object(text: str) -> Optional[dict]:
    """Best-effort pull of the first JSON object out of a text blob."""
    candidate = text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            candidate = brace.group(0)
        else:
            return None
    try:
        obj = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


# ─────────────────────────────────────────────────────────────────────────────
# Injection
# ─────────────────────────────────────────────────────────────────────────────


def build_supervisor_item(guidance: str) -> dict:
    """Build the ``conversation.item.create`` frame for a supervisor note."""
    note = guidance.strip()
    if not note.startswith(SUPERVISOR_NOTE_PREFIX):
        note = f"{SUPERVISOR_NOTE_PREFIX} {note}"
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": SUPERVISOR_ITEM_ROLE,
            "content": [{"type": "input_text", "text": note}],
        },
    }


async def inject_guidance(openai_ws: Any, guidance: str, *, speak: bool) -> None:
    """Inject a supervisor note into the live OpenAI Realtime session.

    Always sends the note as an out-of-band conversation item. When ``speak`` is
    true, follows with a bare ``response.create`` so the model voices a
    correction/addition immediately; otherwise the model silently absorbs the
    note and applies it on its next natural turn.

    A ``response.create`` sent while the model already has an active response
    will be rejected by OpenAI with an error event (logged by the pump) — the
    note itself still lands, so the guidance is not lost.
    """
    try:
        await openai_ws.send_str(json.dumps(build_supervisor_item(guidance)))
        if speak:
            await openai_ws.send_str(json.dumps({"type": "response.create"}))
    except Exception as exc:  # best-effort; the call continues regardless
        logger.debug("[Inkbox supervisor] guidance injection failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor loop
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _SupervisorRuntime:
    """Mutable per-call supervisor bookkeeping, kept off the shared bridge state."""

    interjections: int = 0
    # -inf so the very first review is never throttled by min_review_interval_s,
    # regardless of the clock's absolute value.
    last_review_at: float = float("-inf")
    reviewed_turns: int = 0
    guidance: List[str] = field(default_factory=list)


async def run_supervisor_loop(
    *,
    openai_ws: Any,
    transcript_events: "asyncio.Queue[Tuple[str, str]]",
    transcript_snapshot: Callable[[], List[Tuple[str, str]]],
    is_closed: Callable[[], bool],
    config: SupervisorConfig,
    meta: Any,
    on_supervise: SuperviseCallback,
    is_response_active: Optional[Callable[[], bool]] = None,
    clock: Callable[[], float] = None,  # type: ignore[assignment]
    runtime: Optional[_SupervisorRuntime] = None,
) -> None:
    """Watch the transcript and inject proactive guidance until the call ends.

    Consumes finalized ``(party, text)`` turns from ``transcript_events``. After
    each new turn — caller or agent — and a short debounce so we review a settled
    thought, it snapshots the full transcript and asks ``on_supervise`` for a
    decision, subject to rate-limit / budget guards. Reviewing after *agent*
    turns too (not only caller turns) is what lets the supervisor catch a wrong
    statement the moment the mouth makes it and interject a correction, rather
    than waiting for the next caller turn. Actionable decisions are injected into
    ``openai_ws`` via :func:`inject_guidance`.

    Designed to run as a background task next to the two audio pumps; it exits
    when ``is_closed()`` becomes true (and is cancelled on teardown regardless).
    """
    if clock is None:
        clock = asyncio.get_event_loop().time
    rt = runtime if runtime is not None else _SupervisorRuntime()

    while not is_closed():
        got = await _next_turn(transcript_events, config, is_closed)
        if got is None:
            continue  # timed out waiting; re-check is_closed and loop

        # Debounce: let any trailing fragments of the same thought arrive.
        await _drain_for(transcript_events, config.debounce_s, is_closed)
        if is_closed():
            return

        transcript = transcript_snapshot()
        caller_turns = sum(1 for role, _ in transcript if role == "caller")
        if caller_turns < config.min_caller_turns:
            continue
        if len(transcript) <= rt.reviewed_turns:
            continue  # nothing new since the last review
        now = clock()
        if now - rt.last_review_at < config.min_review_interval_s:
            continue
        if rt.interjections >= config.max_interjections:
            continue

        rt.reviewed_turns = len(transcript)
        rt.last_review_at = now

        try:
            raw = await asyncio.wait_for(
                on_supervise(meta, transcript, list(rt.guidance)),
                timeout=config.review_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.debug("[Inkbox supervisor] review timed out; skipping nudge")
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[Inkbox supervisor] review raised: %s", exc)
            continue

        decision = parse_supervisor_decision(raw)
        if not decision.is_actionable:
            continue
        if is_closed():
            return

        rt.guidance.append(decision.guidance)
        rt.interjections += 1
        # Speak-now only when the model is NOT already mid-response. Firing
        # response.create while a response is active collides with the bridge's
        # own server-VAD auto-response and OpenAI rejects one
        # (conversation_already_has_active_response). When busy, we still inject
        # the note silently — it lands in context and the in-flight/next response
        # picks it up — so the guidance is never lost, we just don't double-fire.
        speak = decision.action == ACTION_INTERJECT and not (
            is_response_active is not None and is_response_active()
        )
        logger.info(
            "[Inkbox supervisor] %s note #%d (speak=%s) for call_id=%s",
            decision.action,
            rt.interjections,
            speak,
            getattr(meta, "call_id", "?"),
        )
        await inject_guidance(openai_ws, decision.guidance, speak=speak)


async def _next_turn(
    transcript_events: "asyncio.Queue[Tuple[str, str]]",
    config: SupervisorConfig,
    is_closed: Callable[[], bool],
) -> Optional[str]:
    """Block until any finalized turn arrives; return its party, or None on timeout.

    Both caller and agent turns wake a review: a caller turn is a chance to steer
    the agent's next reply, an agent turn is a chance to catch and correct what it
    just said. The ``min_review_interval_s`` guard keeps that from getting noisy.
    """
    try:
        party, _text = await asyncio.wait_for(
            transcript_events.get(), timeout=config.poll_interval_s,
        )
    except asyncio.TimeoutError:
        return None
    return party


async def _drain_for(
    transcript_events: "asyncio.Queue[Tuple[str, str]]",
    window_s: float,
    is_closed: Callable[[], bool],
) -> None:
    """Sleep ``window_s`` then discard any events queued during it.

    The authoritative transcript is read from ``transcript_snapshot`` at review
    time, so queued events during the debounce window only need to be cleared,
    not inspected. Draining prevents them from immediately re-triggering a
    second review on the next loop iteration.
    """
    if window_s > 0:
        await asyncio.sleep(window_s)
    while not transcript_events.empty():
        try:
            transcript_events.get_nowait()
        except asyncio.QueueEmpty:
            break
