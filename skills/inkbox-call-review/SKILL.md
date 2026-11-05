---
name: inkbox-call-review
description: Use when the user asks about Inkbox call history, transcripts, missed calls, active-call status, or post-call follow-up work.
user-invocable: false
---

# Inkbox call review

Use this skill when the user asks about Inkbox phone calls, transcripts, or post-call summaries.

## Hermes tool availability

- Hermes exposes `inkbox_place_call` for outbound calls.
- Live voice and Inkbox Voice AI calls provide transcript and post-call
  context to Hermes during call wrap-up.
- `inkbox_list_calls`, `inkbox_get_call`, and `inkbox_get_call_transcripts` expose accessible call history, details, and full transcripts.
- `inkbox_hangup_call` requests hangup; use `inkbox_get_call` to confirm teardown.

## Workflow

1. **Current call wrap-up.** If the current call just ended and transcript,
   outcome, reason, or open action context is present in the turn, use that
   supplied context. Reconcile actions against the transcript before executing
   them, and do not duplicate completed, canceled, or superseded work. Do not
   claim to have fetched unrelated historical call data. For a callback to the
   remote party, use the remote phone number supplied in the current call
   context. It is authoritative for that call; contact memories must not
   replace it.
2. **Past call requests.** List calls with date bounds, get the relevant call, and fetch its transcript. Preserve available speaker phone numbers and distinguish conference guests from transfers using connection history. Missing attribution stays unknown.
3. **Prepare follow-ups from supplied context.** If the user gives the transcript or call summary in the conversation, use that text and the available Inkbox send tools for follow-up.
4. **Complete SMS commitments with the SMS tool.** For a still-needed SMS
   requested during the call, use `inkbox_send_sms` with `to` equal to the exact
   authoritative remote phone number from the current-call context and `text`
   equal to the requested message. Do not use `conversationId` for this
   post-call send. Require a tool result with `ok: true`; plain text does not
   complete the commitment. If the first call has a recoverable argument or
   format error, correct it once and retry once. Do not duplicate a well-formed
   send. If the first error is nonrecoverable or the corrected call also fails,
   do not call again; use `[SILENT]`. Do not use `[SILENT]` while a safe SMS
   commitment remains unattempted.
5. **Avoid exact-quote claims.** Speech-to-text can be imperfect; hedge unless the user supplies exact transcript text.

## Caveats

- A hangup request can return a still-active call while teardown completes; do not claim it has ended until confirmed.
- Contact-rule-blocked calls may be rejected before Hermes sees an event.
