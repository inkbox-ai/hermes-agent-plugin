---
name: inkbox-call-review
description: Use when the user asks to review recent Inkbox calls, inspect missed calls, fetch call transcripts, summarize a past call, or prepare follow-up work based on call history.
user-invocable: false
---

# Inkbox call review

Use this skill when the user asks about previous Inkbox phone calls, missed calls, transcripts, or post-call summaries.

## Required tools

- `inkbox_list_calls` — recent inbound + outbound calls, newest first
- `inkbox_list_call_transcripts` — transcript segments for a call

## Workflow

1. **Find calls.** Call `inkbox_list_calls` with a `limit` matching the requested window. Each call has `id`, `direction`, `remotePhoneNumber`, `status`, and timing fields.
2. **Fetch transcripts.** For any call worth summarizing, call `inkbox_list_call_transcripts` with the call `id`. Segments are ordered by `seq`; `remote` is the outside caller/callee and `local` is the agent side.
3. **Summarize carefully.** Transcripts are speech-to-text output, not exact quotes. Hedge appropriately unless the user asks for exact transcript text.
4. **Prepare follow-ups.** If the user asks for a follow-up email/SMS/note after a past call, use the transcript and then the appropriate Inkbox tool.

## Caveats

- Very short or dropped calls may have no transcript segments.
- Contact-rule-blocked calls may be hidden from identity-scoped API keys.
