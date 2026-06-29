---
name: inkbox-call-review
description: Use when the user asks about Inkbox calls, call transcripts, missed calls, or follow-up work. Hermes can use current live/post-call context, but does not expose historical call-read tools.
user-invocable: false
---

# Inkbox call review

Use this skill when the user asks about Inkbox phone calls, transcripts, or post-call summaries.

## Hermes tool availability

- Hermes exposes `inkbox_place_call` for outbound calls.
- Hermes Realtime calls provide live transcript and post-call context to the agent during call wrap-up.
- Hermes does not register historical call-read tools such as `inkbox_list_calls` or `inkbox_list_call_transcripts`.

## Workflow

1. **Current call wrap-up.** If the current Realtime call just ended and transcript/context is present in the turn, use that supplied context. Do not claim to have fetched historical call data.
2. **Past call requests.** If the user asks to inspect old calls, missed calls, or transcripts, explain that this Hermes installation does not expose historical call-read tools.
3. **Prepare follow-ups from supplied context.** If the user gives the transcript or call summary in the conversation, use that text and the available Inkbox send tools for follow-up.
4. **Avoid exact-quote claims.** Speech-to-text can be imperfect; hedge unless the user supplies exact transcript text.

## Caveats

- Historical call review is available in the OpenClaw power tier, not in the Hermes social tier.
- Contact-rule-blocked calls may be rejected before Hermes sees an event.
