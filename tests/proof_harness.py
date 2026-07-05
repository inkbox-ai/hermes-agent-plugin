"""Reusable call-simulation harness for the supervisor quality proofs.

The same harness backs two proofs of the claim *"a proactive supervisor yields
better calls"*:

* the deterministic CI proof (``tests/test_supervisor_proof.py``) — scripted
  caller, a fixed model of a fallible fast voice model, and a rule-based
  supervisor, scored by per-scenario rubrics. Runs everywhere, no keys, and
  locks in the mechanism + the scoreboard.
* the live LLM-judged proof (``tests/live/test_supervisor_quality.py``) — the
  *same* scenarios and harness, but the voice model / supervisor / caller are
  real models and the grader is an LLM judge. That is the real-world evidence;
  this file is what makes the two share one control.

The harness isolates exactly one variable: whether a supervisor is attached.
The voice model is byte-for-byte identical between the baseline and enhanced
runs of a scenario — the only difference is that in the enhanced run a
supervisor may inject a note, and the voice model incorporates notes it is
given. That is the whole point: hold the "mouth" fixed, add the "brain", measure
the delta.

Modeling assumption (deterministic policies only): the fast voice model is
modeled as *agreeable/confabulating* — when a caller asks something it can't
answer from a shallow reading, it gives the agreeable answer ("yes", "no
problem") rather than checking the loaded context. This is the well-documented
failure mode of small realtime models, and it is what the supervisor exists to
catch. The live suite measures how strongly the effect holds with real models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# Import the real decision type + parser so the simulated supervisor speaks the
# exact same contract as the production supervisor loop.
try:
    from inkbox_plugin.realtime_supervisor import (
        ACTION_INTERJECT,
        SupervisorDecision,
    )
except ImportError:  # pragma: no cover — direct import fallback
    from realtime_supervisor import (  # type: ignore
        ACTION_INTERJECT,
        SupervisorDecision,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Transcript + callable contracts
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Turn:
    party: str  # "caller" | "agent" | "supervisor"
    text: str
    meta: Dict[str, Any] = field(default_factory=dict)


# A voice model: given the transcript so far, the call context, and any pending
# supervisor notes, return the agent's next spoken line.
VoiceModel = Callable[[List[Turn], Dict[str, Any], List[str]], str]

# A supervisor: given the transcript, context, and prior guidance already sent,
# return a decision (may be action="none").
Supervisor = Callable[[List[Turn], Dict[str, Any], List[str]], SupervisorDecision]

# A caller: given the transcript and context, return the next caller line, or
# None to hang up. (A plain list of strings is also accepted and wrapped.)
Caller = Callable[[List[Turn], Dict[str, Any]], Optional[str]]


@dataclass
class Scenario:
    name: str
    context: Dict[str, Any]
    caller: Any  # Caller callable OR list[str] of scripted lines
    # Rubric: score a finished transcript in [0, 1]. Higher = better call.
    grade: Callable[[List[Turn]], float]
    # Human-readable description of what a *good* call does here.
    rubric_note: str = ""


def _as_caller(caller: Any) -> Caller:
    """Normalize a scripted list of lines into a Caller callable."""
    if callable(caller):
        return caller
    lines = list(caller)

    def _scripted(transcript: List[Turn], _context: Dict[str, Any]) -> Optional[str]:
        # One scripted line per caller turn already taken.
        idx = sum(1 for t in transcript if t.party == "caller")
        return lines[idx] if idx < len(lines) else None

    return _scripted


# ─────────────────────────────────────────────────────────────────────────────
# The simulation
# ─────────────────────────────────────────────────────────────────────────────


def run_call(
    scenario: Scenario,
    voice_model: VoiceModel,
    supervisor: Optional[Supervisor] = None,
    *,
    max_turns: int = 12,
) -> List[Turn]:
    """Run one simulated call and return its transcript.

    Turn order per round:

      1. caller speaks (or hangs up → end),
      2. supervisor reviews (steer opportunity) — a note here reaches the agent
         *before* it replies, modeling a silent pre-emptive steer,
      3. agent replies, incorporating any pending notes,
      4. supervisor reviews again (interject opportunity) — if it interjects on
         a wrong agent turn, the agent immediately voices a correction, modeling
         the ``inject`` ``mode: "say"`` speak-now path.

    ``supervisor=None`` is the baseline (pull-only) architecture.

    Idealization to be aware of: this harness is *turn-synchronous* — a steer
    injected at step 2 is guaranteed to reach the agent before its reply at
    step 3. The production loop (``realtime_supervisor.run_supervisor_loop``) is
    *concurrent*, so a steer may land slightly after the model has already begun
    a reply, in which case it applies to the following turn instead. The
    deterministic proof therefore measures the *ceiling* of the mechanism; the
    live suite measures the effect under real concurrency.
    """
    caller = _as_caller(scenario.caller)
    transcript: List[Turn] = []
    prior_guidance: List[str] = []
    pending_notes: List[str] = []

    def _review(stage: str) -> Optional[SupervisorDecision]:
        if supervisor is None:
            return None
        decision = supervisor(list(transcript), scenario.context, list(prior_guidance))
        if decision is None or not decision.is_actionable:
            return None
        prior_guidance.append(decision.guidance)
        pending_notes.append(decision.guidance)
        transcript.append(Turn("supervisor", decision.guidance, meta={"action": decision.action, "stage": stage}))
        return decision

    rounds = 0
    while rounds < max_turns:
        rounds += 1
        line = caller(list(transcript), scenario.context)
        if line is None:
            break
        transcript.append(Turn("caller", line))

        _review("steer")

        reply = voice_model(list(transcript), scenario.context, list(pending_notes))
        pending_notes.clear()
        transcript.append(Turn("agent", reply))

        decision = _review("interject")
        if decision is not None and decision.action == ACTION_INTERJECT:
            # Speak-now: the agent immediately corrects itself using the note.
            correction = voice_model(list(transcript), scenario.context, list(pending_notes))
            pending_notes.clear()
            transcript.append(Turn("agent", correction, meta={"correction": True}))

    return transcript


def run_ab(
    scenario: Scenario,
    voice_model: VoiceModel,
    supervisor: Supervisor,
    **kwargs: Any,
) -> Tuple[List[Turn], List[Turn], float, float]:
    """Run baseline (no supervisor) and enhanced (supervisor) for one scenario.

    Returns (baseline_transcript, enhanced_transcript, baseline_score,
    enhanced_score).
    """
    baseline = run_call(scenario, voice_model, supervisor=None, **kwargs)
    enhanced = run_call(scenario, voice_model, supervisor=supervisor, **kwargs)
    return baseline, enhanced, scenario.grade(baseline), scenario.grade(enhanced)


def agent_lines(transcript: List[Turn]) -> str:
    """Concatenated agent speech (what the caller actually heard)."""
    return " ".join(t.text for t in transcript if t.party == "agent").lower()


def format_transcript(transcript: List[Turn]) -> str:
    """Render a transcript for logs / judge prompts. Supervisor notes are marked."""
    out = []
    for t in transcript:
        if t.party == "supervisor":
            out.append(f"    «supervisor/{t.meta.get('action', '?')}: {t.text}»")
        else:
            out.append(f"{t.party.upper()}: {t.text}")
    return "\n".join(out)
