"""Live, LLM-judged proof that the proactive supervisor yields better calls.

This is the "real models" half of the supervisor proof. For each scenario it
runs the SAME scripted caller against the SAME fast voice model twice —
baseline (no supervisor) and enhanced (the real supervisor prompt) — then has a
stronger judge model blind-score both transcripts on a scenario rubric. It
asserts the enhanced calls score higher in aggregate, and prints a scoreboard.

It deliberately uses a small, error-prone voice model (the "mouth") and a
context that the mouth must respect — exactly the regime where a supervisor
earns its keep. The only variable between the two runs is whether the
supervisor is attached.

Gated: needs OPENAI_API_KEY and LIVE_REAL_MODEL=1. Skipped otherwise. Models are
overridable via env:

  SUPERVISOR_VOICE_MODEL   (default gpt-4o-mini)   — the fast "mouth"
  SUPERVISOR_BRAIN_MODEL   (default gpt-4o-mini)   — the supervisor
  SUPERVISOR_JUDGE_MODEL   (default gpt-4o)        — the blind judge

Run locally:
    OPENAI_API_KEY=sk-... LIVE_REAL_MODEL=1 \
      python -m pytest tests/live/test_supervisor_quality.py -v -s
"""

from __future__ import annotations

import json
import os
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.realtime_supervisor import SupervisorDecision, parse_supervisor_decision
from tests.proof_harness import Scenario, format_transcript, run_call

API_KEY = os.environ.get("OPENAI_API_KEY")
BASE_URL = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
REAL = os.environ.get("LIVE_REAL_MODEL") == "1"
VOICE_MODEL = os.environ.get("SUPERVISOR_VOICE_MODEL", "gpt-4o-mini")
BRAIN_MODEL = os.environ.get("SUPERVISOR_BRAIN_MODEL", "gpt-4o-mini")
JUDGE_MODEL = os.environ.get("SUPERVISOR_JUDGE_MODEL", "gpt-4o")

pytestmark = pytest.mark.skipif(
    not (API_KEY and REAL),
    reason="supervisor quality suite: needs OPENAI_API_KEY + LIVE_REAL_MODEL=1",
)


# ─────────────────────────────────────────────────────────────────────────────
# Minimal OpenAI chat helper (stdlib only)
# ─────────────────────────────────────────────────────────────────────────────


def _chat(model, messages, *, temperature=0.4, json_mode=False, max_tokens=300):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


# ─────────────────────────────────────────────────────────────────────────────
# LLM-backed voice model, supervisor, and judge
# ─────────────────────────────────────────────────────────────────────────────

_VOICE_SYSTEM = (
    "You are a fast phone voice assistant on a live call. Keep every reply to one "
    "or two short spoken sentences. Answer the caller directly and naturally. You "
    "were briefed before the call:\n{brief}\n"
    "If you receive a message tagged [SUPERVISOR], it is private guidance from your "
    "back-office team that the caller cannot hear — follow it in your next reply."
)

_SUPERVISOR_SYSTEM = (
    "You are a silent SUPERVISOR monitoring a live phone call handled by a fast "
    "voice AI (the 'agent'); the caller cannot hear you. You were briefed:\n{brief}\n"
    "Watch the transcript. The voice AI can state facts wrongly, omit briefed "
    "caveats, or over-promise. Decide if it needs a nudge RIGHT NOW.\n"
    'Reply with ONLY JSON: {{"action":"none"|"steer"|"interject","guidance":"<one '
    'short instruction to the agent>","reason":"<why>"}}. Prefer "none" unless '
    "there is a concrete problem. Never invent facts beyond the briefing."
)


def _make_voice_model(brief):
    def voice_model(transcript, _context, notes):
        messages = [{"role": "system", "content": _VOICE_SYSTEM.format(brief=brief)}]
        for t in transcript:
            if t.party == "caller":
                messages.append({"role": "user", "content": t.text})
            elif t.party == "agent":
                messages.append({"role": "assistant", "content": t.text})
            # supervisor turns are injected below as private guidance, not history
        for note in notes:
            messages.append({"role": "system", "content": f"[SUPERVISOR] {note}"})
        return _chat(VOICE_MODEL, messages, temperature=0.5, max_tokens=120)

    return voice_model


def _make_supervisor(brief):
    def supervisor(transcript, _context, prior_guidance):
        convo = "\n".join(
            f"{'CALLER' if t.party == 'caller' else 'AGENT'}: {t.text}"
            for t in transcript if t.party in ("caller", "agent")
        )
        prior = (" Already sent (don't repeat): " + " | ".join(prior_guidance)) if prior_guidance else ""
        user = f"Transcript so far:\n{convo}\n{prior}\nDecide now. JSON only."
        try:
            raw = _chat(
                BRAIN_MODEL,
                [
                    {"role": "system", "content": _SUPERVISOR_SYSTEM.format(brief=brief)},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                json_mode=True,
                max_tokens=200,
            )
        except Exception:
            return SupervisorDecision(action="none")
        return parse_supervisor_decision(raw)

    return supervisor


def _judge(brief, rubric, transcript):
    convo = format_transcript([t for t in transcript if t.party in ("caller", "agent")])
    prompt = (
        "You are grading a phone call for quality. The agent was briefed:\n"
        f"{brief}\n\nA good call, specifically:\n{rubric}\n\n"
        f"TRANSCRIPT:\n{convo}\n\n"
        'Score the AGENT from 0 to 10 and reply ONLY as JSON: {"score": <0-10>, '
        '"reason": "<one sentence>"}.'
    )
    raw = _chat(
        JUDGE_MODEL,
        [{"role": "user", "content": prompt}],
        temperature=0.0,
        json_mode=True,
        max_tokens=200,
    )
    try:
        obj = json.loads(raw)
        return float(obj.get("score", 0)), str(obj.get("reason", ""))
    except (ValueError, TypeError):
        return 0.0, "unparseable judge reply"


# ─────────────────────────────────────────────────────────────────────────────
# Live scenarios
# ─────────────────────────────────────────────────────────────────────────────

_LIVE_SCENARIOS = [
    {
        "name": "wrong_shipping_date",
        "brief": (
            "You are the phone agent for Acme Store. FACT: the caller's order (#8842) "
            "ships in 3 business days. It does NOT ship today and will NOT arrive tomorrow."
        ),
        "caller": [
            "Hi, calling about order 8842 — that's going out today and I'll have it tomorrow, right?",
            "Okay. And it's the blue one?",
        ],
        "rubric": (
            "clearly tells the caller the order ships in about 3 business days and does "
            "NOT confirm same-day shipping or next-day arrival."
        ),
    },
    {
        "name": "final_sale_omission",
        "brief": (
            "You are the phone agent for a boutique. FACT: the item the caller is asking "
            "about (the clearance jacket) is FINAL SALE — no returns, no refunds, no exchanges."
        ),
        "caller": [
            "If the clearance jacket doesn't fit, I can just mail it back for a refund, yeah?",
            "Hm, okay.",
        ],
        "rubric": (
            "clearly tells the caller the clearance jacket is final sale and cannot be "
            "returned/refunded, rather than confirming a return or refund is possible."
        ),
    },
    {
        "name": "over_promise_email",
        "brief": (
            "You are a fast phone agent. You CANNOT send email yourself — only the main "
            "back-office agent can, after the call. If asked to email something, you must "
            "say it's queued to be sent after the call, not that you're sending it now."
        ),
        "caller": [
            "Can you shoot me an email with my invoice right now while we're on the phone?",
            "Great.",
        ],
        "rubric": (
            "tells the caller the invoice/email is queued to be sent after the call and "
            "does NOT claim to be sending it right now or that it's already sent."
        ),
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# The proof
# ─────────────────────────────────────────────────────────────────────────────


def test_supervisor_beats_baseline_by_llm_judge(capsys):
    rows = []
    for sc in _LIVE_SCENARIOS:
        scenario = Scenario(
            name=sc["name"],
            context={"contact_known": True},
            caller=sc["caller"],
            grade=lambda _t: 0.0,  # unused; the judge scores here
            rubric_note=sc["rubric"],
        )
        voice_model = _make_voice_model(sc["brief"])
        supervisor = _make_supervisor(sc["brief"])

        base_t = run_call(scenario, voice_model, supervisor=None)
        enh_t = run_call(scenario, voice_model, supervisor=supervisor)

        base_score, base_reason = _judge(sc["brief"], sc["rubric"], base_t)
        enh_score, enh_reason = _judge(sc["brief"], sc["rubric"], enh_t)
        rows.append({
            "name": sc["name"],
            "base": base_score, "enh": enh_score,
            "base_reason": base_reason, "enh_reason": enh_reason,
            "base_t": base_t, "enh_t": enh_t,
            "interjected": any(t.party == "supervisor" for t in enh_t),
        })

    base_mean = sum(r["base"] for r in rows) / len(rows)
    enh_mean = sum(r["enh"] for r in rows) / len(rows)

    print("\n=== Supervisor call-quality proof (live, LLM-judged) ===")
    print(f"voice={VOICE_MODEL}  supervisor={BRAIN_MODEL}  judge={JUDGE_MODEL}")
    print(f"{'scenario':<24}{'baseline':>10}{'enhanced':>10}{'delta':>8}{'nudged':>8}")
    for r in rows:
        print(f"{r['name']:<24}{r['base']:>10.1f}{r['enh']:>10.1f}"
              f"{r['enh'] - r['base']:>8.1f}{('yes' if r['interjected'] else 'no'):>8}")
    print(f"{'MEAN':<24}{base_mean:>10.2f}{enh_mean:>10.2f}{enh_mean - base_mean:>8.2f}")
    for r in rows:
        print(f"\n--- {r['name']} ---")
        print(f"BASELINE {r['base']:.1f} ({r['base_reason']}):\n{format_transcript(r['base_t'])}")
        print(f"ENHANCED {r['enh']:.1f} ({r['enh_reason']}):\n{format_transcript(r['enh_t'])}")

    # The proof: with real models, the supervised calls score higher on average.
    assert enh_mean > base_mean, (
        f"supervisor did not improve judged quality: baseline={base_mean:.2f} "
        f"enhanced={enh_mean:.2f}"
    )
    # And the supervisor actually did something on at least one scenario (guards
    # against a vacuous pass where nothing was ever injected).
    assert any(r["interjected"] for r in rows), "supervisor never intervened"
