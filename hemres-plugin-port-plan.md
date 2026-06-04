# Hermes Plugin Port Plan

Date: 2026-06-04

Scope: compare the current Hermes Inkbox plugin against the OpenClaw plugin work that landed in PR #4
(`realtime-post-call-hangup`) and PR #5 (`realtime-connect-fallback`). This doc lists what is already
ported, what still needs to move over, and how to sequence the remaining work.

Implementation note: the branch that adds this document also ports the P0/P1 items below in one PR. The matrix is
kept as the audit record that drove the implementation.

## Current Baseline

Hermes plugin branch inspected: `main` at `2e0e7a2` (`Reuse Hermes OpenAI API credentials for realtime setup`).

OpenClaw changes used as reference:

- PR #4: realtime setup wizard auth, GA OpenAI Realtime API-key path, edit/delete post-call tools, delayed two-step hangup, SMS conversation-id handling, group SMS behavior, email subject preservation, post-call prompt cleanup, README gateway restart docs.
- PR #5: connect to OpenAI Realtime before accepting raw Inkbox media, with fallback to Inkbox STT/TTS when the realtime connection fails.

## Summary Matrix

| OpenClaw item | Hermes status | Port action |
| --- | --- | --- |
| Realtime setup wizard detects existing OpenAI API keys and validates `gpt-realtime-2` | Already ported | No runtime port needed. `setup_wizard.py` checks plugin config, `INKBOX_REALTIME_API_KEY`, Hermes OpenAI API credentials, and `OPENAI_API_KEY`; it retries after validation failure. |
| GA OpenAI Realtime API-key-only auth | Already ported | No port needed. Hermes removed Codex/OAuth minting for GA Realtime and uses OpenAI API keys. |
| Realtime websocket GA header shape | Already ported | No port needed. Hermes sends the bearer auth header and uses the GA `session.update` payload shape; tests assert the old beta header is not sent. |
| `register_post_call_action`, `edit_post_call_action`, `delete_post_call_action` | Already ported | No port needed. Tool schemas and tests exist in `realtime.py` and `tests/test_realtime_bridge_parity.py`. |
| Two-step `hang_up_call` with a short final delay | Already ported | No port needed. Hermes arms hangup first, then sleeps for 2 seconds on the actual close. |
| SMS conversation-id centric replies | Already ported | No port needed. `adapter.py` replies with `conversation_id`; tests cover direct and group SMS. |
| Group SMS silence policy | Already ported | No port needed. Group messages include explicit silence policy and `[SILENT]` handling. |
| Email subject and `In-Reply-To` preservation | Already ported | No port needed. Hermes stashes inbound subject/message id and replies with `Re:` plus `in_reply_to_message_id`. |
| Voice progress/post-call leakage suppression | Already ported | No port needed. Existing tests cover voice progress suppression. |
| README install/update/restart flow | Already ported | No port needed. README has `hermes gateway run`, `hermes plugins update inkbox`, and `hermes gateway restart`. |
| OpenClaw auto-skill warning fix | Already ported | No port needed. Hermes has the `inkbox-call-review` split and no post-call auto-skill warning path. |
| OpenAI Realtime connect-before-accept fallback | Missing | Port as P0. Hermes currently accepts the Inkbox call WebSocket with raw-media headers before OpenAI connection succeeds. |
| Runtime fallback config equivalent to `voiceRealtime.fallbackToInkboxSttTts` | Missing/partial | Port as part of P0. Hermes can disable realtime with `INKBOX_REALTIME_ENABLED=false`, but there is no explicit "do not fallback" switch for runtime connect failure. |
| In-call consult result memory | Missing | Port as P1. Hermes returns consult answers to Realtime but does not store them for post-call reconciliation. |
| Post-call handoff includes consult results and full transcript | Partial | Port as P1. Hermes includes queued actions and recent transcript, but not consult results or the stronger stale-action instructions from OpenClaw. |
| Duplicate same SMS consult guard | Missing | Port as P1. OpenClaw dedupes repeated in-call SMS sends while pending or already completed unless caller asks for another/repeat/different message. |
| Realtime prompt clarity for "do it now" vs "after the call" | Partial | Port as P1/P2. Hermes has basic guidance, but OpenClaw is more explicit that live tool work should use consult, after-call deferral should use queued actions, and completed consults should delete duplicate queued actions. |
| OpenClaw `visibleReplySent` / `pendingFinalDelivery` delivery fix | Not directly applicable | Do not port literally. Hermes consults run through a one-shot `hermes -z` subprocess, but the equivalent behavior is consult result tracking and post-call dedupe. |

## P0: Realtime Connect Fallback Before Inkbox Accept

### Problem

Hermes currently does this in `adapter.py`:

1. Build `web.WebSocketResponse`.
2. If realtime is enabled, set:
   - `x-use-inkbox-text-to-speech: false`
   - `x-use-inkbox-speech-to-text: false`
3. `await ws.prepare(request)`.
4. Call `run_inkbox_realtime_bridge(...)`.
5. `run_inkbox_realtime_bridge` then opens the OpenAI Realtime websocket.

If OpenAI auth, model access, DNS, or Realtime service connection fails after step 3, the call has already been
accepted in raw-media mode. At that point Inkbox STT/TTS is disabled and fallback is too late.

OpenClaw fixed this by connecting to Realtime before accepting the Inkbox raw-media websocket. If Realtime connect
fails before accept, it accepts the same phone call in Inkbox STT/TTS mode instead.

### Proposed Hermes Port

Refactor `realtime.py` so Realtime connection can be preflighted before `ws.prepare(request)`.

One clean shape:

- Add `open_inkbox_realtime_bridge(...)` or similar in `realtime.py`.
- That function:
  - resolves the OpenAI bearer/API key,
  - opens the OpenAI Realtime websocket with an 8 second timeout,
  - sends the session update once metadata is available,
  - returns a bridge object with `run(inkbox_ws)` and `close()`.
- Update `adapter.py`:
  - resolve call metadata and outbound call context before choosing media mode,
  - if realtime is enabled, attempt the preflight connection first,
  - on success, set raw-media headers, `await ws.prepare(request)`, then run the bridge,
  - on failure and fallback is allowed, set Inkbox STT/TTS headers, `await ws.prepare(request)`, and continue down the existing text-mode call path,
  - on failure and fallback is disabled, reject/close with a clear log.

Add a config/env knob equivalent to OpenClaw's `voiceRealtime.fallbackToInkboxSttTts`:

- config: `platforms.inkbox.realtime.fallback_to_inkbox_stt_tts`
- env: `INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS`
- default: `true`

### Tests

- Realtime connect failure before `prepare()` falls back to Inkbox STT/TTS and sets both Inkbox headers to `true`.
- Realtime connect failure with fallback disabled closes/rejects and does not enter the text-mode call path.
- Successful realtime connection still sets both Inkbox headers to `false` and runs the realtime bridge.
- Connect attempt times out after the configured timeout and falls back.

## P1: Consult Result Tracking And Post-Call Reconciliation

### Problem

Hermes has the tools, but the post-call handoff is missing the main evidence that prevents duplicate work:

- `_BridgeState` tracks transcript and post-call actions only.
- `hermes_agent_consult` returns a result to OpenAI but does not persist it in bridge state.
- `_dispatch_post_call` passes only actions and transcript to `_realtime_post_call_actions`.
- `_realtime_post_call_actions` tells the main agent to execute queued actions, but it does not include in-call consult results or the stronger "only still-needed work" instructions.

This is the path that can make the main Hermes agent repeat work after hangup, even if the realtime model already used
`hermes_agent_consult` to send an SMS/email or queue the work during the live call.

### Proposed Hermes Port

- Add `consult_results` to `_BridgeState`.
- Store each consult result with:
  - tool call id,
  - request text,
  - result text or error,
  - timestamp,
  - optional dedupe key.
- Extend `AgentConsultCallback` so `_realtime_agent_consult` receives pending post-call actions and prior consult results.
- Extend `PostCallActionsCallback` so `_realtime_post_call_actions` receives consult results.
- Update `_realtime_agent_consult` prompt to include:
  - pending after-call actions,
  - prior consult results from the same call,
  - instruction to say explicitly if the consult completed, queued, canceled, or superseded a queued after-call action.
- Update `_realtime_post_call_actions` prompt to match OpenClaw's behavior:
  - review queued actions against the full transcript,
  - review in-call consult results,
  - execute only still-needed actions,
  - do not repeat SMS/email/note/contact work already completed or queued during the call,
  - ask for missing info only when necessary,
  - do not send confirmation follow-ups unless requested.

### Tests

- A consult result is recorded in `_BridgeState`.
- The post-call synthetic message includes consult results.
- The post-call synthetic message includes the full call transcript, not only the last few turns.
- The post-call instructions explicitly say not to repeat work already completed or queued by in-call consults.

## P1: Duplicate Same-SMS Consult Guard

### Problem

OpenClaw prevents the realtime model from sending the same SMS twice when it calls the consult tool repeatedly for the
same phone number and same quoted/generic SMS request. Hermes does not currently have this guard.

### Proposed Hermes Port

Add OpenClaw-style dedupe helpers to `realtime.py`:

- normalize consult request text,
- identify SMS/text/message requests with a phone number,
- include quoted message content when present,
- allow repeats only when the caller says `again`, `another`, `different`, `new`, `repeat`, or `second`.

Track:

- `pending_consult_keys` for in-flight requests,
- completed consult keys in `consult_results`.

Return tool results:

- `already_running` when the same request is in flight,
- `already_handled` when the same request already completed,
- proceed normally when the caller explicitly asks for another/repeat/different message.

### Tests

- Duplicate SMS consult while pending returns `already_running`.
- Duplicate SMS consult after completion returns `already_handled`.
- Requests containing `another`, `repeat`, or `different` are allowed through.
- Non-SMS consults are not deduped.

## P1/P2: Realtime Prompt Parity

Hermes should copy the stronger intent split from OpenClaw's realtime instructions:

- If caller asks for work to happen now during the live call and it needs Hermes/Inkbox tools, call `hermes_agent_consult`.
- If caller explicitly asks for work after the call, or accepts after-call deferral, call `register_post_call_action`.
- If a consult completes or queues work that matches a queued after-call action, call `delete_post_call_action` so it does not run twice after hangup.
- `hang_up_call` is two-step: first arm and say goodbye, then call it again to end the call.
- Do not call consult for greetings, caller identity at call start, or generic chat.

This is mostly a prompt/test parity change once P1 state tracking is in place.

## Not Recommended To Port Literally

- OpenClaw's `visibleReplySent` and `pendingFinalDelivery` fixes are tied to OpenClaw's channel runtime delivery API.
  Hermes uses a one-shot `hermes -z` consult subprocess, so the literal code does not apply.
- OpenClaw's realtime provider abstraction and tool policy calls are OpenClaw SDK-specific. Hermes should keep its own
  direct `aiohttp` Realtime bridge unless a Hermes-native provider abstraction appears.

## Suggested PR Sequence

1. PR A: realtime preflight connection and Inkbox STT/TTS fallback before `ws.prepare`.
2. PR B: consult result memory, post-call reconciliation prompt, and callback signature updates.
3. PR C: duplicate SMS consult dedupe and prompt/test polish.

## Full Test Plan

Run the focused Hermes suite:

```bash
python -m pytest tests/test_realtime_auth.py tests/test_realtime_bridge_parity.py tests/test_setup_wizard.py tests/test_sms_conversations.py tests/test_voice_progress_suppression.py
```

Manual smoke tests after PR A:

1. Force OpenAI Realtime connect to fail before accepting the Inkbox websocket.
2. Call the agent.
3. Verify the call stays alive through Inkbox STT/TTS.
4. Verify logs clearly say Realtime connect failed and fallback was used.
5. Remove the forced failure.
6. Call again and verify raw OpenAI Realtime is used.

Manual smoke tests after PR B/C:

1. During a realtime call, ask the agent to send an SMS now.
2. Ask the same SMS request again without saying repeat/another.
3. Verify the second request is not sent again.
4. Ask for an after-call email, then ask the live consult to do the same work now.
5. Verify the queued post-call action is deleted or the post-call handoff skips it.
