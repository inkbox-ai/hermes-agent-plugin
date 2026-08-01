---
name: inkbox-call-review
description: Use when the user asks about Inkbox calls, call transcripts, missed calls, or follow-up work. Hermes can use current live/post-call context, but does not expose historical call-read tools.
user-invocable: false
---

# Inkbox call review

Use this skill when the user asks about Inkbox phone calls, transcripts, or post-call summaries.

## Hermes tool availability

- Hermes exposes `inkbox_place_call` for outbound calls.
- OpenAI Realtime and Inkbox Voice AI calls provide transcript and post-call
  context to Hermes during call wrap-up.
- Hermes does not register historical call-read tools such as `inkbox_list_calls` or `inkbox_list_call_transcripts`.

## Workflow

1. **Current call wrap-up.** If the current call just ended and transcript,
   outcome, reason, or open action context is present in the turn, use that
   supplied context. Reconcile actions against the transcript before executing
   them, and do not duplicate completed, canceled, or superseded work. Do not
   claim to have fetched unrelated historical call data. For a callback to the
   remote party, use the remote phone number supplied in the current call
   context. It is authoritative for that call; contact memories must not
   replace it.
2. **Past call requests.** If the user asks to inspect old calls, missed calls, or transcripts, explain that this Hermes installation does not expose historical call-read tools.
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

- Historical call review is available in the OpenClaw power tier, not in the Hermes social tier.
- Contact-rule-blocked calls may be rejected before Hermes sees an event.
