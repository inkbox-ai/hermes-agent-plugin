---
name: inkbox-outbound-calling
description: Use when the user asks the agent to place an outbound Inkbox phone call, call a phone number/contact, or call someone with a specific purpose or opening message.
user-invocable: false
---

# Inkbox outbound calling

Use this skill when the user asks you to call someone from the configured Inkbox phone number.

## Optional tool

- `inkbox_place_call` — place an outbound call from the configured Inkbox identity.

## Workflow

1. Resolve the recipient to an E.164 phone number. If the user names a contact, use Inkbox contact lookup tools first.
2. Call `inkbox_place_call` with:
   - `toNumber`
   - `purpose` — required. Include the reason/topic the user gave; if none was given, say the user asked for a general call.
   - `openingMessage` — include when the user told you what to say first.
   - `context` — concise background the voice agent may need during the call.
3. Do not invent or request `clientWebsocketUrl`; the plugin supplies the active Inkbox call bridge when the channel gateway is running.
4. When the callee answers, the call session starts with the supplied purpose/context instead of a generic greeting.

## Follow-ups

If the user asks you to call and then send a post-call email/SMS/note, include that request in the call context. During realtime calls, the voice agent can register post-call actions for the main agent to execute after hangup.
