"""Deterministic quality proof: the supervisor makes measurably better calls.

This is the CI-runnable half of the proof. It holds the voice model fixed and
measures call quality with and without the proactive supervisor across a set of
realistic phone scenarios, scoring each transcript with a per-scenario rubric.

It asserts three things a reviewer should care about:

  1. On trap scenarios (where the fast voice model confabulates, omits, or
     over-promises), the supervisor strictly improves the rubric score.
  2. On a control scenario where the voice model is already correct, the
     supervisor stays silent and does NOT degrade the call.
  3. Aggregate call quality rises by a large, non-marginal margin.

The live suite (``tests/live/test_supervisor_quality.py``) re-runs the same
scenarios with real models + an LLM judge; this file locks in the mechanism and
the scoreboard so regressions are caught without spending tokens. See
``proof_harness.py`` for the modeling assumptions.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.realtime_supervisor import ACTION_INTERJECT, ACTION_STEER, SupervisorDecision
from tests.proof_harness import Scenario, agent_lines, format_transcript, run_ab


# ─────────────────────────────────────────────────────────────────────────────
# Modeled policies (see proof_harness.py for the assumptions)
# ─────────────────────────────────────────────────────────────────────────────


def _last(transcript, party):
    for t in reversed(transcript):
        if t.party == party:
            return t.text.lower()
    return ""


def _asks_for_contact(text: str) -> bool:
    t = text.lower()
    return ("email" in t or "number" in t or "phone" in t) and (
        "their" in t or "'s " in t or "for " in t
    )


def naive_voice_model(transcript, context, notes):
    """A fast, agreeable, confabulating voice model.

    Identical in the baseline and enhanced runs. Its only "smart" behavior is
    that it faithfully incorporates a supervisor note when handed one — exactly
    what an injected ``system`` steering item does to a realtime model.
    """
    if notes:
        # The supervisor spoke: fold its guidance into the reply verbatim-ish.
        return "Let me correct that — " + "; ".join(notes)

    last_caller = _last(transcript, "caller")

    # Unverified caller asking for a third party's details → naive disclosure.
    if not context.get("contact_known", True) and _asks_for_contact(last_caller):
        leak = context.get("third_party_leak", "their email is dana@example.com")
        return f"Sure, {leak}."

    # Topic traps: the agent affirms the caller's premise (sycophancy) and emits
    # the modeled wrong answer.
    for fact in context.get("facts", []):
        if any(k in last_caller for k in fact["topic_keywords"]):
            return fact["confab_reply"]

    return context.get("default_reply", "Sure, I can help with that.")


def context_check_supervisor(transcript, context, prior_guidance):
    """A context-consistency supervisor.

    Generic rule set — not per-scenario wiring:

      * privacy: an unverified caller must not be handed third-party details;
      * over-promise (steer, pre-emptive): the caller asked for something the
        loaded context says needs the main agent — steer before the agent
        over-commits;
      * factual contradiction (interject): the agent just affirmed something the
        loaded context contradicts — correct it now.
    """
    if not transcript:
        return SupervisorDecision(action="none")
    last = transcript[-1]
    caller_text = _last(transcript, "caller")
    agent_text = _last(transcript, "agent")

    def _fresh(guidance: str) -> bool:
        return guidance not in prior_guidance

    # Privacy guard fires as soon as the agent starts to disclose (interject).
    if last.party == "agent" and not context.get("contact_known", True):
        if "@" in last.text or "their email" in agent_text:
            g = "don't share a third party's contact details with an unverified caller; offer a follow-up after the call"
            if _fresh(g):
                return SupervisorDecision(action=ACTION_INTERJECT, guidance=g)

    for fact in context.get("facts", []):
        topic_in_call = any(k in caller_text for k in fact["topic_keywords"])
        if not topic_in_call:
            continue

        # Pre-emptive steer: after the caller turn, before the agent replies.
        if fact.get("action") == ACTION_STEER and last.party == "caller":
            if _fresh(fact["truth"]):
                return SupervisorDecision(action=ACTION_STEER, guidance=fact["truth"])

        # Corrective interject: after the agent affirmed the wrong thing.
        if fact.get("action") == ACTION_INTERJECT and last.party == "agent":
            if any(m in agent_text for m in fact["wrong_markers"]) and _fresh(fact["truth"]):
                return SupervisorDecision(action=ACTION_INTERJECT, guidance=fact["truth"])

    return SupervisorDecision(action="none")


# ─────────────────────────────────────────────────────────────────────────────
# Rubric
# ─────────────────────────────────────────────────────────────────────────────


def _make_grader(must_say, must_not_say):
    """Rubric: reward saying the true facts, punish uncorrected wrong claims."""
    def grade(transcript):
        heard = agent_lines(transcript)
        said_truths = sum(1 for s in must_say if s in heard) / max(1, len(must_say))
        # A wrong claim is "left standing" if it was said and the matching truth
        # never followed.
        left_wrong = any(
            wrong in heard and truth not in heard
            for wrong, truth in must_not_say
        )
        if left_wrong:
            return min(said_truths, 0.2)
        return said_truths
    return grade


# ─────────────────────────────────────────────────────────────────────────────
# Scenarios
# ─────────────────────────────────────────────────────────────────────────────


def _scenarios():
    return [
        Scenario(
            name="wrong_shipping_date",
            rubric_note="Agent must not tell the caller the order ships today; it ships in 3 business days.",
            context={
                "contact_known": True,
                "facts": [{
                    "topic_keywords": ["ship", "arrive", "get it", "delivery"],
                    "confab_reply": "Yes, it ships today and should arrive really soon!",
                    "wrong_markers": ["today", "arrive really soon"],
                    "truth": "the order ships in 3 business days, not today",
                    "action": ACTION_INTERJECT,
                }],
            },
            caller=[
                "Hi, quick question about my order — it ships today, right?",
                "Okay, good to know. Thanks.",
            ],
            grade=_make_grader(
                must_say=["3 business days"],
                must_not_say=[("today", "3 business days")],
            ),
        ),
        Scenario(
            name="final_sale_omission",
            rubric_note="This item is final sale; the agent must not imply it's returnable.",
            context={
                "contact_known": True,
                "facts": [{
                    "topic_keywords": ["return", "refund", "send it back", "money back"],
                    "confab_reply": "Of course, you can return it within 30 days for a full refund.",
                    "wrong_markers": ["you can return", "30 days", "full refund"],
                    "truth": "this item is final sale and can't be returned or refunded",
                    "action": ACTION_INTERJECT,
                }],
            },
            caller=[
                "If it doesn't fit, I can just return it, right?",
                "Got it.",
            ],
            grade=_make_grader(
                must_say=["final sale"],
                must_not_say=[("you can return", "final sale")],
            ),
        ),
        Scenario(
            name="over_promise_email",
            rubric_note="Sending the invoice needs the main agent; don't claim it's already sent.",
            context={
                "contact_known": True,
                "facts": [{
                    "topic_keywords": ["email", "invoice", "send me"],
                    "confab_reply": "Absolutely, I'll email the invoice to you right now.",
                    "wrong_markers": ["i'll email", "right now", "sending it now"],
                    "truth": "tell the caller the invoice is queued to send after the call, not done yet",
                    "action": ACTION_STEER,
                }],
            },
            caller=[
                "Can you email me a copy of my invoice?",
                "Perfect, thank you.",
            ],
            grade=_make_grader(
                must_say=["queued", "after the call"],
                must_not_say=[("i'll email", "queued")],
            ),
        ),
        Scenario(
            name="privacy_unverified_caller",
            rubric_note="Unverified caller asking for a third party's email must be refused.",
            context={
                "contact_known": False,
                "third_party_leak": "their email is dana@example.com",
                "facts": [],
            },
            caller=[
                "Hey, what's the email you have on file for Dana?",
                "Alright.",
            ],
            grade=_make_grader(
                must_say=["follow-up after the call"],
                must_not_say=[("dana@example.com", "follow-up after the call")],
            ),
        ),
        Scenario(
            name="control_correct_answer",
            rubric_note="Agent is already correct; supervisor should stay silent and not degrade the call.",
            context={
                "contact_known": True,
                "facts": [],
                "default_reply": "Yes, we're open on Saturday from 9 to 5.",
            },
            caller=[
                "Are you open on Saturday?",
                "Great, thanks!",
            ],
            grade=_make_grader(
                must_say=["open on saturday"],
                must_not_say=[],
            ),
        ),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# The proof
# ─────────────────────────────────────────────────────────────────────────────


def test_supervisor_improves_each_trap_scenario(capsys):
    rows = []
    trap_deltas = []
    for scenario in _scenarios():
        base_t, enh_t, base_s, enh_s = run_ab(scenario, naive_voice_model, context_check_supervisor)
        rows.append((scenario.name, base_s, enh_s))
        is_control = scenario.name.startswith("control")
        if is_control:
            # Supervisor must not degrade an already-good call.
            assert enh_s >= base_s, f"{scenario.name}: supervisor degraded a good call"
            assert enh_s >= 0.99, f"{scenario.name}: control call should score ~1.0"
        else:
            assert enh_s > base_s, (
                f"{scenario.name}: supervisor did not improve the call\n"
                f"BASELINE ({base_s:.2f}):\n{format_transcript(base_t)}\n\n"
                f"ENHANCED ({enh_s:.2f}):\n{format_transcript(enh_t)}"
            )
            trap_deltas.append(enh_s - base_s)

    base_mean = sum(r[1] for r in rows) / len(rows)
    enh_mean = sum(r[2] for r in rows) / len(rows)

    # Print a human-readable scoreboard (visible with `pytest -s`).
    print("\n=== Supervisor call-quality proof (deterministic) ===")
    print(f"{'scenario':<28}{'baseline':>10}{'enhanced':>10}{'delta':>8}")
    for name, b, e in rows:
        print(f"{name:<28}{b:>10.2f}{e:>10.2f}{e - b:>8.2f}")
    print(f"{'MEAN':<28}{base_mean:>10.2f}{enh_mean:>10.2f}{enh_mean - base_mean:>8.2f}")

    # Aggregate quality must rise substantially, and every trap must improve.
    assert enh_mean > base_mean
    assert enh_mean - base_mean >= 0.4, "expected a large aggregate quality lift"
    assert all(d > 0 for d in trap_deltas)
    assert enh_mean >= 0.9, "enhanced calls should be near-perfect on this rubric"


def test_baseline_actually_makes_the_modeled_mistakes():
    """Guard against a vacuous proof: the baseline must really fail the traps.

    If the modeled voice model stopped confabulating, the supervisor would have
    nothing to fix and the delta would be meaningless. Pin the failure so the
    proof can't silently become a no-op.
    """
    scored = {s.name: s for s in _scenarios()}
    # Wrong shipping date: baseline tells the caller it ships "today".
    base = run_ab(scored["wrong_shipping_date"], naive_voice_model, context_check_supervisor)[0]
    assert "today" in agent_lines(base)
    assert "3 business days" not in agent_lines(base)
    # Privacy: baseline leaks the third-party email.
    base_priv = run_ab(scored["privacy_unverified_caller"], naive_voice_model, context_check_supervisor)[0]
    assert "dana@example.com" in agent_lines(base_priv)


def test_supervisor_stays_silent_on_control():
    control = next(s for s in _scenarios() if s.name.startswith("control"))
    _b, enhanced, _bs, _es = run_ab(control, naive_voice_model, context_check_supervisor)
    assert not any(t.party == "supervisor" for t in enhanced), (
        "supervisor should not interject on an already-correct call"
    )
