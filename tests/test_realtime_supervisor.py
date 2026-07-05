"""Unit tests for the proactive realtime supervisor.

Covers the pure decision-parsing / injection-frame builders and the async
supervisor loop (debounce, rate-limit, budget, dedup, silent-vs-speak
injection, teardown), plus the realtime-bridge wiring that publishes transcript
turns onto the supervisor bus. No network, fully deterministic.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import realtime as realtime_mod
from inkbox_plugin.realtime import RealtimeCallMeta, RealtimeConfig, _BridgeState, _openai_to_inkbox_pump
from inkbox_plugin.realtime_supervisor import (
    ACTION_INTERJECT,
    ACTION_NONE,
    ACTION_STEER,
    INJECT_MODE_CONTEXT,
    INJECT_MODE_SAY,
    SUPERVISOR_NOTE_PREFIX,
    SupervisorConfig,
    SupervisorDecision,
    build_inject_frame,
    inject_guidance,
    parse_supervisor_decision,
    run_supervisor_loop,
)


if realtime_mod.aiohttp is None:  # mirror the parity suite's aiohttp shim
    realtime_mod.aiohttp = types.SimpleNamespace(
        WSMsgType=types.SimpleNamespace(
            TEXT=object(), CLOSE=object(), CLOSED=object(), ERROR=object(),
        ),
    )


def _meta(**overrides):
    base = {
        "call_id": "call-sup",
        "contact_id": "c-1",
        "contact_name": "Alex Wilcox",
        "remote_phone_number": "+15555550101",
        "direction": "inbound",
        "contact_known": True,
    }
    base.update(overrides)
    return RealtimeCallMeta(**base)


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_str(self, payload):
        self.sent.append(json.loads(payload))


class _FakeMsg:
    def __init__(self, data):
        self.type = realtime_mod.aiohttp.WSMsgType.TEXT
        self.data = json.dumps(data)


class _FakeOpenAIWS(_FakeWS):
    def __init__(self, frames):
        super().__init__()
        self._frames = [_FakeMsg(frame) for frame in frames]

    def __aiter__(self):
        self._it = iter(self._frames)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


# ─────────────────────────────────────────────────────────────────────────────
# Decision parsing
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_none_shapes():
    for raw in ["", "  ", "NONE", "none", "[SILENT]", "n/a", None]:
        d = parse_supervisor_decision(raw)
        assert d.action == ACTION_NONE
        assert not d.is_actionable


def test_parse_verb_prefixes():
    steer = parse_supervisor_decision("STEER: mention the refund window is 30 days")
    assert steer.action == ACTION_STEER
    assert steer.guidance == "mention the refund window is 30 days"
    assert steer.is_actionable

    interject = parse_supervisor_decision("INTERJECT: correct the price, it's $49 not $39")
    assert interject.action == ACTION_INTERJECT
    assert interject.is_actionable

    # "speak" is normalized to interject.
    assert parse_supervisor_decision("speak: fix that").action == ACTION_INTERJECT


def test_parse_json_object_and_fenced_json():
    d = parse_supervisor_decision('{"action":"interject","guidance":"say hi","reason":"greeting"}')
    assert d.action == ACTION_INTERJECT
    assert d.guidance == "say hi"
    assert d.reason == "greeting"

    fenced = parse_supervisor_decision('```json\n{"action":"steer","guidance":"nudge"}\n```')
    assert fenced.action == ACTION_STEER
    assert fenced.guidance == "nudge"


def test_parse_json_none_action_wins_over_guidance():
    # Guidance present but action none → not actionable.
    d = parse_supervisor_decision('{"action":"none","guidance":"ignored"}')
    assert d.action == ACTION_NONE
    assert not d.is_actionable


def test_parse_freeform_text_becomes_silent_steer():
    d = parse_supervisor_decision("the caller sounds confused about billing")
    assert d.action == ACTION_STEER
    assert d.guidance == "the caller sounds confused about billing"


def test_parse_passthrough_decision_object():
    orig = SupervisorDecision(action="interject", guidance="go")
    assert parse_supervisor_decision(orig).action == ACTION_INTERJECT


def test_parse_invalid_action_falls_back_to_none():
    assert parse_supervisor_decision('{"action":"shout","guidance":"x"}').action == ACTION_NONE


# ─────────────────────────────────────────────────────────────────────────────
# Injection frame builders
# ─────────────────────────────────────────────────────────────────────────────


def test_build_inject_frame_shape():
    frame = build_inject_frame("correct the price", speak=False)
    assert frame["event"] == "inject"
    assert frame["mode"] == INJECT_MODE_CONTEXT
    assert frame["text"].startswith(SUPERVISOR_NOTE_PREFIX)
    assert "correct the price" in frame["text"]


def test_build_inject_frame_speak_mode():
    frame = build_inject_frame("say this now", speak=True)
    assert frame["mode"] == INJECT_MODE_SAY


def test_build_inject_frame_does_not_double_prefix():
    frame = build_inject_frame(f"{SUPERVISOR_NOTE_PREFIX} already tagged", speak=False)
    assert frame["text"].count(SUPERVISOR_NOTE_PREFIX) == 1


def test_inject_steer_sends_context_frame():
    ws = _FakeWS()
    asyncio.run(inject_guidance(ws, "add context", speak=False))
    assert len(ws.sent) == 1
    assert ws.sent[0]["event"] == "inject"
    assert ws.sent[0]["mode"] == INJECT_MODE_CONTEXT


def test_inject_interject_sends_single_say_frame():
    ws = _FakeWS()
    asyncio.run(inject_guidance(ws, "correct that now", speak=True))
    # One frame — the say mode replaces the old item + response.create pair.
    assert len(ws.sent) == 1
    assert ws.sent[0]["event"] == "inject"
    assert ws.sent[0]["mode"] == INJECT_MODE_SAY


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor loop
# ─────────────────────────────────────────────────────────────────────────────


async def _wait_until(pred, timeout=2.0, step=0.01):
    for _ in range(int(timeout / step)):
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


async def _run_loop_until(openai_ws, transcript, decisions, *, config=None, expect_sends, meta=None, grow=True):
    """Drive run_supervisor_loop, feeding one caller turn per queued decision.

    ``decisions`` is a list the on_supervise stub returns in order (then NONE).
    Events are fed one at a time, each only after the prior review has been
    consumed — real caller turns arrive spread out in time, and the loop's
    debounce drains anything already queued, so feeding all at once would
    collapse them into a single review. Returns the prior-guidance snapshot the
    stub observed on each call, for dedup assertions.
    """
    config = config or SupervisorConfig(
        enabled=True, debounce_s=0.0, min_review_interval_s=0.0, poll_interval_s=0.01,
    )
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}
    seen_prior = []
    calls = {"n": 0}

    async def _on_supervise(_meta, _transcript, prior_guidance):
        seen_prior.append(list(prior_guidance))
        i = calls["n"]
        calls["n"] += 1
        return decisions[i] if i < len(decisions) else SupervisorDecision(action=ACTION_NONE)

    task = asyncio.create_task(run_supervisor_loop(
        ws=openai_ws,
        transcript_events=events,
        transcript_snapshot=lambda: list(transcript),
        is_closed=lambda: closed["v"],
        config=config,
        meta=meta or _meta(),
        on_supervise=_on_supervise,
    ))

    for i in range(len(decisions)):
        target = calls["n"] + 1
        # Real calls grow the transcript each turn; the loop deliberately skips a
        # review when the transcript hasn't grown since the last one, so a static
        # transcript would collapse every feed into one review. Grow it here to
        # mirror reality (guard-specific tests pass grow=False).
        if grow:
            transcript.append(("caller", f"caller-turn-{i}"))
        events.put_nowait(("caller", "hello"))
        # Wait for this review to be consumed. If a guard (min_caller_turns,
        # max_interjections) legitimately blocks the review, this just times out
        # and we move on — the assertions cover those cases.
        await _wait_until(lambda: calls["n"] >= target, timeout=0.4)

    if expect_sends:
        await _wait_until(lambda: len(openai_ws.sent) >= expect_sends, timeout=1.0)
    else:
        # Give the loop a beat to consult (and decline) before we tear it down.
        await asyncio.sleep(0.15)
    closed["v"] = True
    await asyncio.wait_for(task, timeout=2.0)
    return seen_prior


def test_loop_injects_silent_steer_on_caller_turn():
    ws = _FakeWS()
    asyncio.run(_run_loop_until(
        ws,
        transcript=[("caller", "what's my balance?")],
        decisions=[SupervisorDecision(action=ACTION_STEER, guidance="offer to check the balance")],
        expect_sends=1,
    ))
    assert [f["event"] for f in ws.sent] == ["inject"]
    assert ws.sent[0]["mode"] == INJECT_MODE_CONTEXT
    assert "offer to check the balance" in ws.sent[0]["text"]


def test_loop_speaks_on_interject():
    ws = _FakeWS()
    asyncio.run(_run_loop_until(
        ws,
        transcript=[("caller", "so it's free, right?"), ("agent", "yes, totally free")],
        decisions=[SupervisorDecision(action=ACTION_INTERJECT, guidance="correct: there's a $5 fee")],
        expect_sends=1,
    ))
    # One say-mode inject replaces the old item + response.create pair.
    assert [f["event"] for f in ws.sent] == ["inject"]
    assert ws.sent[0]["mode"] == INJECT_MODE_SAY


def test_loop_does_nothing_on_none():
    ws = _FakeWS()
    seen = asyncio.run(_run_loop_until(
        ws,
        transcript=[("caller", "hi")],
        decisions=[SupervisorDecision(action=ACTION_NONE)],
        expect_sends=0,  # never satisfied; loop polls then we close it
    ))
    assert ws.sent == []
    assert seen  # the supervisor was consulted, it just declined


def test_loop_respects_max_interjections():
    ws = _FakeWS()
    config = SupervisorConfig(
        enabled=True, debounce_s=0.0, min_review_interval_s=0.0,
        poll_interval_s=0.01, max_interjections=1,
    )
    asyncio.run(_run_loop_until(
        ws,
        transcript=[("caller", "a"), ("caller", "b"), ("caller", "c")],
        decisions=[
            SupervisorDecision(action=ACTION_STEER, guidance="first"),
            SupervisorDecision(action=ACTION_STEER, guidance="second"),
        ],
        config=config,
        expect_sends=1,
    ))
    inject_frames = [f for f in ws.sent if f["event"] == "inject"]
    assert len(inject_frames) == 1
    assert "first" in inject_frames[0]["text"]


def test_loop_passes_prior_guidance_for_dedup():
    ws = _FakeWS()
    seen_prior = asyncio.run(_run_loop_until(
        ws,
        transcript=[("caller", "a"), ("caller", "b")],
        decisions=[
            SupervisorDecision(action=ACTION_STEER, guidance="mention the deadline"),
            SupervisorDecision(action=ACTION_STEER, guidance="mention the fee"),
        ],
        expect_sends=2,
    ))
    # First review sees no prior guidance; the second sees the first note so it
    # can avoid repeating itself.
    assert seen_prior[0] == []
    assert seen_prior[1] == ["mention the deadline"]


def test_loop_reviews_after_agent_turns_to_catch_errors():
    # An agent turn (not just a caller turn) must wake a review — that's how the
    # supervisor catches a wrong statement the moment the mouth makes it and
    # interjects a correction, rather than waiting for the next caller turn.
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}
    consulted = {"n": 0}

    async def _on_supervise(*_a):
        consulted["n"] += 1
        return SupervisorDecision(action=ACTION_INTERJECT, guidance="correct that")

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            # A caller turn already happened (min_caller_turns satisfied); the
            # agent just answered.
            transcript_snapshot=lambda: [("caller", "is it free?"), ("agent", "yes, totally free")],
            is_closed=lambda: closed["v"],
            config=SupervisorConfig(
                enabled=True, debounce_s=0.0, min_review_interval_s=0.0, poll_interval_s=0.01,
            ),
            meta=_meta(),
            on_supervise=_on_supervise,
        ))
        events.put_nowait(("agent", "yes, totally free"))
        await _wait_until(lambda: consulted["n"] >= 1, timeout=1.0)
        closed["v"] = True
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    assert consulted["n"] >= 1
    # An interject speaks now: a single say-mode inject frame.
    assert [f["event"] for f in ws.sent] == ["inject"]
    assert ws.sent[0]["mode"] == INJECT_MODE_SAY


def test_loop_respects_min_caller_turns():
    ws = _FakeWS()
    config = SupervisorConfig(
        enabled=True, debounce_s=0.0, min_review_interval_s=0.0,
        poll_interval_s=0.01, min_caller_turns=2,
    )
    # Only one caller turn in the transcript, but min_caller_turns=2 → no review.
    asyncio.run(_run_loop_until(
        ws,
        transcript=[("caller", "just one turn")],
        decisions=[SupervisorDecision(action=ACTION_STEER, guidance="nope")],
        config=config,
        expect_sends=0,
        grow=False,
    ))
    assert ws.sent == []


def test_loop_exits_when_closed_without_events():
    # With no transcript events, the loop must still exit promptly once closed.
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}

    async def _on_supervise(*_a):
        return SupervisorDecision(action=ACTION_NONE)

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            transcript_snapshot=lambda: [],
            is_closed=lambda: closed["v"],
            config=SupervisorConfig(enabled=True, poll_interval_s=0.01),
            meta=_meta(),
            on_supervise=_on_supervise,
        ))
        await asyncio.sleep(0.05)
        closed["v"] = True
        await asyncio.wait_for(task, timeout=2.0)
        assert task.done()

    asyncio.run(_run())


def test_loop_survives_supervise_exception():
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}

    async def _boom(*_a):
        raise RuntimeError("supervisor model down")

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            transcript_snapshot=lambda: [("caller", "x")],
            is_closed=lambda: closed["v"],
            config=SupervisorConfig(
                enabled=True, debounce_s=0.0, min_review_interval_s=0.0, poll_interval_s=0.01,
            ),
            meta=_meta(),
            on_supervise=_boom,
        ))
        events.put_nowait(("caller", "x"))
        await asyncio.sleep(0.1)
        closed["v"] = True
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    # A failing supervisor never disrupts the call: no frames, clean exit.
    assert ws.sent == []


# ─────────────────────────────────────────────────────────────────────────────
# Bridge wiring: transcript turns are published onto the supervisor bus
# ─────────────────────────────────────────────────────────────────────────────


def test_pump_publishes_transcript_turns_to_bus():
    inkbox_ws = _FakeWS()
    openai_ws = _FakeOpenAIWS([
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "can you check the invoice"},
        {"type": "response.output_audio_transcript.done",
         "transcript": "sure, one moment"},
    ])
    state = _BridgeState()
    state.stream_id = "s-1"
    state.supervisor_active = True  # a supervisor is consuming the bus

    async def _noop(*_a, **_k):
        return ""

    asyncio.run(_openai_to_inkbox_pump(
        openai_ws=openai_ws,
        inkbox_ws=inkbox_ws,
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop,
    ))

    drained = []
    while not state.transcript_events.empty():
        drained.append(state.transcript_events.get_nowait())
    assert drained == [
        ("caller", "can you check the invoice"),
        ("agent", "sure, one moment"),
    ]


def test_maybe_start_supervisor_disabled_returns_none():
    async def _run():
        cfg = RealtimeConfig(enabled=True, api_key="sk-test")  # supervisor default: disabled
        task = realtime_mod._maybe_start_supervisor(
            ws=_FakeWS(),
            state=_BridgeState(),
            config=cfg,
            meta=_meta(),
            on_supervise=lambda *_a: None,
        )
        assert task is None

    asyncio.run(_run())


def test_maybe_start_supervisor_none_callback_returns_none():
    async def _run():
        cfg = RealtimeConfig(enabled=True, api_key="sk-test", supervisor=SupervisorConfig(enabled=True))
        task = realtime_mod._maybe_start_supervisor(
            ws=_FakeWS(),
            state=_BridgeState(),
            config=cfg,
            meta=_meta(),
            on_supervise=None,
        )
        assert task is None

    asyncio.run(_run())


def test_maybe_start_supervisor_enabled_spawns_and_cancels():
    async def _run():
        cfg = RealtimeConfig(
            enabled=True, api_key="sk-test",
            supervisor=SupervisorConfig(enabled=True, poll_interval_s=0.01),
        )
        state = _BridgeState()

        async def _on_supervise(*_a):
            return SupervisorDecision(action=ACTION_NONE)

        task = realtime_mod._maybe_start_supervisor(
            ws=_FakeWS(),
            state=state,
            config=cfg,
            meta=_meta(),
            on_supervise=_on_supervise,
        )
        assert task is not None
        await realtime_mod._cancel_supervisor_task(task)
        assert task.cancelled() or task.done()

    asyncio.run(_run())


# ─────────────────────────────────────────────────────────────────────────────
# Guard coverage: rate-limit, debounce, timeout, reviewed_turns, response-active,
# cancellation, and parse edge cases (added after adversarial review)
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_empty_guidance_normalizes_to_none():
    # A verb with only whitespace guidance is not actionable.
    for raw in ["STEER:   ", "INTERJECT:", '{"action":"steer","guidance":"   "}']:
        d = parse_supervisor_decision(raw)
        assert d.action == ACTION_NONE, raw
        assert not d.is_actionable


def test_parse_truncated_json_is_not_injected_as_steer():
    # A max_tokens-truncated JSON object must NOT fall through to a freeform
    # steer that injects half a JSON blob as spoken guidance.
    truncated = '{"action":"steer","guidance":"tell them the refund is'
    d = parse_supervisor_decision(truncated)
    assert d.action == ACTION_NONE
    assert not d.is_actionable


def test_loop_rate_limits_reviews_with_min_interval():
    # With a large min_review_interval and a clock that does not advance, a
    # second turn must NOT trigger a second review.
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}
    reviews = {"n": 0}
    transcript = [("caller", "a")]

    async def _on_supervise(*_a):
        reviews["n"] += 1
        return SupervisorDecision(action=ACTION_STEER, guidance="note")

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            transcript_snapshot=lambda: list(transcript),
            is_closed=lambda: closed["v"],
            config=SupervisorConfig(
                enabled=True, debounce_s=0.0, min_review_interval_s=1000.0, poll_interval_s=0.01,
            ),
            meta=_meta(),
            on_supervise=_on_supervise,
            clock=lambda: 100.0,  # frozen clock → interval never elapses
        ))
        events.put_nowait(("caller", "a"))
        await _wait_until(lambda: reviews["n"] >= 1, timeout=1.0)
        transcript.append(("caller", "b"))
        events.put_nowait(("caller", "b"))
        await asyncio.sleep(0.1)
        closed["v"] = True
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    assert reviews["n"] == 1, "min_review_interval did not throttle the second review"


def test_loop_debounce_coalesces_burst_into_one_review():
    # A burst of fragments within the debounce window collapses into ONE review
    # that sees the whole transcript.
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}
    reviews = {"n": 0}
    seen_len = []
    transcript = [("caller", "part one"), ("caller", "part two"), ("caller", "part three")]

    async def _on_supervise(_m, t, _g):
        reviews["n"] += 1
        seen_len.append(len(t))
        return SupervisorDecision(action=ACTION_NONE)

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            transcript_snapshot=lambda: list(transcript),
            is_closed=lambda: closed["v"],
            config=SupervisorConfig(
                enabled=True, debounce_s=0.08, min_review_interval_s=0.0, poll_interval_s=0.01,
            ),
            meta=_meta(),
            on_supervise=_on_supervise,
        ))
        # Three fragments arrive close together (within the debounce window).
        events.put_nowait(("caller", "part one"))
        events.put_nowait(("caller", "part two"))
        events.put_nowait(("caller", "part three"))
        await _wait_until(lambda: reviews["n"] >= 1, timeout=1.0)
        await asyncio.sleep(0.1)
        closed["v"] = True
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    assert reviews["n"] == 1, "debounce did not coalesce the burst into one review"
    assert seen_len == [3], "the single review should see the full coalesced transcript"


def test_loop_review_timeout_skips_without_injecting():
    # A hung supervisor model must be abandoned after review_timeout_s and must
    # not inject anything; the loop keeps running.
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}
    started = {"v": False}

    async def _hang(*_a):
        started["v"] = True
        await asyncio.Event().wait()  # never returns

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            transcript_snapshot=lambda: [("caller", "x")],
            is_closed=lambda: closed["v"],
            config=SupervisorConfig(
                enabled=True, debounce_s=0.0, min_review_interval_s=0.0,
                review_timeout_s=0.05, poll_interval_s=0.01,
            ),
            meta=_meta(),
            on_supervise=_hang,
        ))
        events.put_nowait(("caller", "x"))
        await _wait_until(lambda: started["v"], timeout=1.0)
        await asyncio.sleep(0.15)  # let the timeout trip
        closed["v"] = True
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    assert ws.sent == [], "a timed-out review must not inject guidance"


def test_loop_skips_review_when_transcript_has_not_grown():
    # reviewed_turns guard: a second wake with no new turns must not re-review
    # (and thus not re-inject duplicate guidance).
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}
    reviews = {"n": 0}
    static = [("caller", "a"), ("agent", "b")]  # never grows

    async def _on_supervise(*_a):
        reviews["n"] += 1
        return SupervisorDecision(action=ACTION_NONE)

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            transcript_snapshot=lambda: list(static),
            is_closed=lambda: closed["v"],
            config=SupervisorConfig(
                enabled=True, debounce_s=0.0, min_review_interval_s=0.0, poll_interval_s=0.01,
            ),
            meta=_meta(),
            on_supervise=_on_supervise,
        ))
        events.put_nowait(("caller", "a"))
        await _wait_until(lambda: reviews["n"] >= 1, timeout=1.0)
        # A second wake with the SAME transcript length must be skipped.
        events.put_nowait(("agent", "b"))
        await asyncio.sleep(0.1)
        closed["v"] = True
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    assert reviews["n"] == 1, "reviewed_turns guard did not skip an unchanged transcript"


def test_loop_interject_suppressed_to_silent_when_response_active():
    # When a response is already being generated, a speak-now interject must
    # downgrade to a silent context inject (not talk over it); the note still
    # lands so the guidance is not lost.
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    closed = {"v": False}
    reviews = {"n": 0}

    async def _on_supervise(*_a):
        reviews["n"] += 1
        return SupervisorDecision(action=ACTION_INTERJECT, guidance="correct it")

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            transcript_snapshot=lambda: [("caller", "x"), ("agent", "wrong")],
            is_closed=lambda: closed["v"],
            config=SupervisorConfig(
                enabled=True, debounce_s=0.0, min_review_interval_s=0.0, poll_interval_s=0.01,
            ),
            meta=_meta(),
            on_supervise=_on_supervise,
            is_response_active=lambda: True,  # a response is mid-flight
        ))
        events.put_nowait(("agent", "wrong"))
        await _wait_until(lambda: reviews["n"] >= 1, timeout=1.0)
        await asyncio.sleep(0.05)
        closed["v"] = True
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    # Downgraded to a silent context inject while a response is active.
    assert [f["event"] for f in ws.sent] == ["inject"]
    assert ws.sent[0]["mode"] == INJECT_MODE_CONTEXT


def test_loop_cancellation_mid_review_propagates():
    # Cancelling the loop while a review is in-flight must cancel cleanly (the
    # CancelledError is re-raised, not swallowed) and inject nothing.
    ws = _FakeWS()
    events: "asyncio.Queue" = asyncio.Queue()
    entered = {"v": False}

    async def _block(*_a):
        entered["v"] = True
        await asyncio.Event().wait()

    async def _run():
        task = asyncio.create_task(run_supervisor_loop(
            ws=ws,
            transcript_events=events,
            transcript_snapshot=lambda: [("caller", "x")],
            is_closed=lambda: False,
            config=SupervisorConfig(
                enabled=True, debounce_s=0.0, min_review_interval_s=0.0, poll_interval_s=0.01,
                review_timeout_s=100.0,
            ),
            meta=_meta(),
            on_supervise=_block,
        ))
        events.put_nowait(("caller", "x"))
        await _wait_until(lambda: entered["v"], timeout=1.0)
        await realtime_mod._cancel_supervisor_task(task)
        assert task.cancelled() or task.done()

    asyncio.run(_run())
    assert ws.sent == []


def test_pump_toggles_response_active_flag():
    state = _BridgeState()
    assert state.response_active is False
    openai_ws = _FakeOpenAIWS([
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_audio.delta", "delta": "AA"},
        {"type": "response.done", "response": {"id": "r1"}},
    ])

    async def _noop(*_a, **_k):
        return ""

    asyncio.run(_openai_to_inkbox_pump(
        openai_ws=openai_ws,
        inkbox_ws=_FakeWS(),
        state=state,
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop,
    ))
    # After response.done, the flag is cleared.
    assert state.response_active is False


def test_publish_transcript_turn_noop_without_supervisor():
    # The pump must not fill the bus when no supervisor is consuming it.
    state = _BridgeState()
    assert state.supervisor_active is False
    realtime_mod._publish_transcript_turn(state, "caller", "hello")
    assert state.transcript_events.empty()
    state.supervisor_active = True
    realtime_mod._publish_transcript_turn(state, "caller", "hello")
    assert not state.transcript_events.empty()
