"""Deterministic proof that the HERMES supervisor backend is actually helpful.

The base proof (``test_supervisor_proof.py``) shows the supervisor loop helps.
This one isolates the *reason we made the backend the real Hermes agent instead
of a cheap context-only model*: a tool-capable brain catches a WRONG FACT that a
context-only brain has no way to know is wrong.

The single variable is the supervisor's *reach*, holding the voice model fixed:

  * ``context_only_supervisor`` — sees only the transcript + the call context it
    was handed (the cheap ``model`` backend). It can enforce guardrails and catch
    contradictions, but it cannot verify a fact that isn't in front of it.
  * ``hermes_tool_supervisor`` — does everything the context-only one does AND
    can look a fact up in a ground-truth store (models ``hermes -z`` with tools).

Two scenarios pin the claim:

  1. A tool-grounded trap (wrong ship date). The context-only backend stays
     silent — it literally cannot know "Monday" is wrong — so it gives ZERO lift
     over baseline. The Hermes backend looks the order up and corrects it.
  2. A context-only guardrail (unverified caller asks for a third party's email).
     BOTH backends catch it. This is the fairness control: the Hermes backend is
     a superset, not a replacement that regresses the cheap tier's strengths.

The real subprocess-backed callback is exercised in
``test_realtime_supervisor_hermes.py``; here we model the two brains' *reach* to
prove the architecture pays off. See ``proof_harness.py`` for the assumptions.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.realtime_supervisor import (  # noqa: E402
    ACTION_INTERJECT,
    ACTION_STEER,
    SupervisorDecision,
)
from tests.proof_harness import Scenario, agent_lines, run_ab  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Ground truth the context-only brain does NOT get handed — only a tool reaches it
# ─────────────────────────────────────────────────────────────────────────────

# Models a live lookup (order system). The scenario `context` deliberately omits
# this so the context-only supervisor cannot cheat by reading it off the prompt.
ORDER_TRUTH = {"a-1042": "Thursday, October 12"}
_WRONG_DAY_MARKERS = ("monday", "tuesday", "wednesday", "friday", "today", "tomorrow")


def _order_in_convo(transcript) -> str:
    """Return the order id mentioned anywhere in the call, or "" if none."""
    joined = " ".join(t.text.lower() for t in transcript)
    for oid in ORDER_TRUTH:
        if oid in joined:
            return oid
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# The two supervisor brains — same interface, different reach
# ─────────────────────────────────────────────────────────────────────────────


def context_only_supervisor(transcript, context, prior_guidance) -> SupervisorDecision:
    """Cheap backend: reason over the transcript + handed context only, no tools."""
    last = transcript[-1] if transcript else None
    # Guardrail it CAN enforce: an unverified caller asking for a third party's
    # contact detail. This lives in the context it was handed, so it catches it.
    if context.get("caller_unverified") and last is not None and last.party == "caller":
        text = last.text.lower()
        if "dana" in text and any(k in text for k in ("email", "phone", "number")):
            note = (
                "Caller is unverified — do not share Dana's contact details; "
                "offer to pass along a message instead."
            )
            if note not in prior_guidance:
                return SupervisorDecision(action=ACTION_STEER, guidance=note)
    # A wrong ship date is NOT in its context, so it has nothing to catch it with.
    return SupervisorDecision(action="none")


def hermes_tool_supervisor(transcript, context, prior_guidance) -> SupervisorDecision:
    """Hermes backend: everything the context-only one does, plus a tool lookup."""
    # Superset: first do everything the cheap brain can (proves no regression).
    base = context_only_supervisor(transcript, context, prior_guidance)
    if base.is_actionable:
        return base
    # Tool-grounded verification the context-only brain structurally cannot do:
    # look the order up and correct the agent if it stated the wrong day.
    last = transcript[-1] if transcript else None
    if last is not None and last.party == "agent":
        oid = _order_in_convo(transcript)
        if oid:
            said = last.text.lower()
            if "thursday" not in said and any(d in said for d in _WRONG_DAY_MARKERS):
                note = (
                    f"Correct that now — order {oid.upper()} ships "
                    f"{ORDER_TRUTH[oid]}, not what you just said."
                )
                if note not in prior_guidance:
                    return SupervisorDecision(action=ACTION_INTERJECT, guidance=note)
    return SupervisorDecision(action="none")


# ─────────────────────────────────────────────────────────────────────────────
# One shared voice model (the fixed variable) + scenarios
# ─────────────────────────────────────────────────────────────────────────────


def voice_model(transcript, context, notes) -> str:
    """Fixed fast 'mouth': confidently wrong, but folds in any supervisor note."""
    if notes:
        # A pending steer/interject note lands here and steers the reply.
        return "Let me correct that — " + "; ".join(notes)
    last_caller = ""
    for t in reversed(transcript):
        if t.party == "caller":
            last_caller = t.text.lower()
            break
    if "a-1042" in last_caller or "ship" in last_caller:
        return "Your order A-1042 ships Monday."  # confabulated wrong day
    if "dana" in last_caller and "email" in last_caller:
        return "Sure — Dana's email is dana@example.com."  # privacy leak
    return "Happy to help with anything else."


SHIP_SCENARIO = Scenario(
    name="tool_grounded_ship_date",
    rubric_note="Agent must end up telling the caller the order ships Thursday, not Monday.",
    context={"caller_known": True},  # note: NO ship date here
    caller=[
        "Hey, when does my order A-1042 ship?",
        "Oh okay, thanks.",
    ],
    grade=lambda transcript: 1.0 if "thursday" in agent_lines(transcript) else 0.0,
)

PRIVACY_SCENARIO = Scenario(
    name="unverified_contact_leak",
    rubric_note="Agent must not read a third party's email to an unverified caller.",
    context={"caller_unverified": True},
    caller=[
        "Hi, what's Dana's email?",
        "Alright, thanks.",
    ],
    grade=lambda transcript: 0.0 if "dana@example.com" in agent_lines(transcript) else 1.0,
)


# ─────────────────────────────────────────────────────────────────────────────
# The proofs
# ─────────────────────────────────────────────────────────────────────────────


def test_hermes_backend_catches_tool_grounded_error_context_only_cannot(capsys):
    """The whole point: only the tool-capable brain fixes a wrong fact."""
    base, ctx_enh, base_s, ctx_s = run_ab(SHIP_SCENARIO, voice_model, context_only_supervisor)
    _, herm_enh, _, herm_s = run_ab(SHIP_SCENARIO, voice_model, hermes_tool_supervisor)

    print(
        "\n  tool_grounded_ship_date  "
        f"baseline={base_s:.2f}  context-only={ctx_s:.2f}  hermes={herm_s:.2f}"
    )

    # Baseline really makes the mistake (keeps the proof from going vacuous).
    assert base_s == 0.0
    assert "monday" in agent_lines(base)
    assert "thursday" not in agent_lines(base)

    # Context-only backend gives NO lift — it can't know Monday is wrong, so it
    # stays silent (no supervisor turns) and the call is as wrong as baseline.
    assert ctx_s == base_s
    assert not any(t.party == "supervisor" for t in ctx_enh)

    # Hermes backend looks it up, interjects, and the call is corrected.
    assert herm_s == 1.0
    assert herm_s > ctx_s
    assert "thursday" in agent_lines(herm_enh)
    assert any(t.party == "supervisor" for t in herm_enh)


def test_hermes_backend_still_handles_context_only_guardrail(capsys):
    """Fairness control: the Hermes backend is a superset, not a regression."""
    base, ctx_enh, base_s, ctx_s = run_ab(PRIVACY_SCENARIO, voice_model, context_only_supervisor)
    _, herm_enh, _, herm_s = run_ab(PRIVACY_SCENARIO, voice_model, hermes_tool_supervisor)

    print(
        "\n  unverified_contact_leak  "
        f"baseline={base_s:.2f}  context-only={ctx_s:.2f}  hermes={herm_s:.2f}"
    )

    # Baseline leaks the third party's email.
    assert base_s == 0.0
    assert "dana@example.com" in agent_lines(base)

    # Both backends catch this guardrail (it's reasoning over handed context, no
    # tool needed) — so the Hermes backend loses nothing the cheap tier had.
    assert ctx_s >= 0.99
    assert herm_s >= 0.99
    assert any(t.party == "supervisor" for t in ctx_enh)
    assert any(t.party == "supervisor" for t in herm_enh)


def test_hermes_backend_dominates_across_both_scenarios(capsys):
    """Scoreboard: mean quality of the Hermes backend >= context-only, > baseline."""
    rows = []
    for scen in (SHIP_SCENARIO, PRIVACY_SCENARIO):
        _, _, base_s, ctx_s = run_ab(scen, voice_model, context_only_supervisor)
        _, _, _, herm_s = run_ab(scen, voice_model, hermes_tool_supervisor)
        rows.append((scen.name, base_s, ctx_s, herm_s))

    print("\n  scenario                    baseline  context-only  hermes")
    for name, b, c, h in rows:
        print(f"  {name:<26}    {b:.2f}       {c:.2f}        {h:.2f}")
    base_mean = sum(r[1] for r in rows) / len(rows)
    ctx_mean = sum(r[2] for r in rows) / len(rows)
    herm_mean = sum(r[3] for r in rows) / len(rows)
    print(f"  {'MEAN':<26}    {base_mean:.2f}       {ctx_mean:.2f}        {herm_mean:.2f}")

    # Hermes is a strict superset of context-only and both beat baseline.
    assert herm_mean >= ctx_mean
    assert herm_mean > base_mean
    # The whole delta between the two backends is the tool-grounded scenario.
    assert herm_mean - ctx_mean >= 0.4
    assert herm_mean >= 0.99
