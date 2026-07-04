# Realtime call supervisor — design

## The problem

The base realtime voice bridge (`realtime.py`) is **pull-only**. The OpenAI
Realtime voice model — a fast, low-latency "mouth" — drives the entire call, and
the heavier main Hermes agent (the "brain") only engages when the voice model
*chooses* to invoke the `consult_agent` tool. That tool spawns a one-shot
`hermes -z` subprocess and returns text for the voice model to read back.

Two things are missing:

1. **The brain has no visibility into the call as it happens.** It sees nothing
   until it is pulled, and then only the query the voice model chose to send.
2. **The brain cannot steer the call.** If the fast voice model is confidently
   wrong, forgets a piece of loaded context, over-promises, or simply doesn't
   realize it needs help, nothing corrects it. Small realtime models are fluent
   but error-prone in exactly this way — they answer agreeably even when unsure.

The supervisor closes that loop by adding a **push** channel alongside the
existing pull one.

## The three-tier split

| Tier | Model | Path | Owns |
|---|---|---|---|
| **Fast — "mouth"** | `gpt-realtime` | hard real-time, sub-second | turn-taking, backchannel, greetings, slot-filling, reading answers back |
| **Mid — "supervisor"** | cheap reasoning model (`gpt-4o-mini` class) | off-path, seconds OK | watches every caller turn; decides whether to nudge; **pushes** steering notes. **New.** |
| **Smart — "brain"** | full Hermes agent + tools (`consult_agent`) | off-path, heavy | multi-step reasoning, tool execution, irreversible actions. Pull-style, unchanged — but the supervisor can now proactively steer the mouth *to* consult. |

The supervisor is the new middle tier. It runs a single short model call per
caller turn (debounced + rate-limited), not the full agent loop, so it is cheap
and low-latency enough to run continuously. It never executes tools — actual
work still flows through `consult_agent` and post-call actions.

## How steering is injected

Guidance is pushed into the live session over the *same* OpenAI Realtime
WebSocket the audio pumps use. Two modes, both validated against the OpenAI
Realtime GA API:

### Silent steer (default)

Inject a `system`-role conversation item and send **no** `response.create`. With
server VAD (the default), the voice model absorbs the note and folds it into its
next natural reply — zero perceived interruption.

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "message",
    "role": "system",
    "content": [{ "type": "input_text", "text": "[SUPERVISOR] the order ships in 3 business days, not today" }]
  }
}
```

### Speak-now interject

Inject the same note, then fire a bare `response.create` so the model speaks a
correction immediately (e.g. it just told the caller something wrong).

```json
{ "type": "conversation.item.create", "item": { "...": "as above" } }
{ "type": "response.create" }
```

Notes:

- `system`/`developer` items are **text-only** (`input_text`) — never audio.
- `conversation.item.create` alone never produces speech; the `response.create`
  is what voices it. This is the #1 "nothing happened" mistake.
- A `response.create` sent while a response is already active is rejected by
  OpenAI (`conversation_already_has_active_response`); the injected note still
  lands and is used on the next turn, so the guidance is never lost.
- An alternative primitive, `response.create` with `conversation: "none"`, runs
  a silent out-of-band analysis using session context without speaking — useful
  if the supervisor is ever collapsed onto the realtime model itself. We keep a
  separate reasoning model instead, to honor the "smarter backend" goal.

## Control flow

```
Caller audio ──► Inkbox WS ──► OpenAI Realtime (mouth) ──► Inkbox WS ──► Caller
                                     │  finalized transcript turns
                                     ▼
                          state.transcript_events (asyncio.Queue)
                                     │
                                     ▼
                          run_supervisor_loop  ── on caller turn, debounced ──►  on_supervise()  (mid model)
                                     │                                                 │ decision
                                     └──────────── inject_guidance() ◄─────────────────┘
                                        (system note ± response.create)
```

The loop lives in `realtime_supervisor.py` and runs as a third background task
next to the two audio pumps (`OpenedRealtimeBridge.run`). It is not part of the
pump race — it lives for the call's duration and is cancelled on teardown.

### Guards (why it stays polite)

- **Debounce** — reviews once per settled caller *thought*, not per fragment.
- **Min review interval** — never more than one review per N seconds.
- **Max interjections** — a hard cap on notes per call.
- **Min caller turns** — doesn't second-guess the opening exchange.
- **Dedup** — prior guidance is passed back so the supervisor won't repeat itself.
- **Fail-open** — a supervisor timeout/error/`none` leaves the call untouched.
- Default **off**; the base pull-only behavior is unchanged unless opted in.

## Proving it yields better calls

Two proofs share one control (`tests/proof_harness.py`), which isolates a single
variable: whether a supervisor is attached. The voice model is byte-for-byte
identical in the baseline and enhanced runs of every scenario.

### Deterministic proof — `tests/test_supervisor_proof.py` (runs in CI)

A fixed model of a fallible, agreeable voice model plus a context-consistency
supervisor, scored by per-scenario rubrics across five realistic calls (wrong
fact, omitted caveat, over-promise, privacy leak, and a control where the agent
is already right). It asserts every trap scenario improves, the control is
untouched, and aggregate quality rises by a large margin. Representative run:

```
scenario                      baseline  enhanced   delta
wrong_shipping_date               0.00      1.00    1.00
final_sale_omission               0.00      1.00    1.00
over_promise_email                0.00      1.00    1.00
privacy_unverified_caller         0.00      1.00    1.00
control_correct_answer            1.00      1.00    0.00
MEAN                              0.20      1.00    0.80
```

A companion test pins the baseline *failures* so the proof can never silently
degrade into a no-op.

### Live LLM-judged proof — `tests/live/test_supervisor_quality.py` (gated)

The same scenarios and harness, but the voice model, supervisor, and caller are
real models and a stronger judge model blind-scores both transcripts. It asserts
the supervised calls score higher in aggregate and that the supervisor actually
intervened. Needs `OPENAI_API_KEY` + `LIVE_REAL_MODEL=1`; run on demand via the
`Live — supervisor quality` workflow. This is the real-world magnitude behind
the deterministic proof's ceiling.

## References

- OpenAI Realtime API — conversations & client events:
  <https://platform.openai.com/docs/guides/realtime>,
  <https://platform.openai.com/docs/api-reference/realtime-client-events/conversation/item/create>
- Out-of-band responses (`conversation: "none"`):
  <https://developers.openai.com/cookbook/examples/realtime_out_of_band_transcription>
- `openai-realtime-agents` chat-supervisor pattern (pull-only baseline):
  <https://github.com/openai/openai-realtime-agents>
- LiveKit observer-pattern voice-agent guardrails (push observer):
  <https://livekit.com/blog/observer-pattern-voice-agent-guardrails>
- Talker–Reasoner / Thinker–Talker dual-process framing:
  <https://arxiv.org/html/2410.08328v1>, <https://arxiv.org/pdf/2511.07397>
