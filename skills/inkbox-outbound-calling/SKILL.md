---
name: inkbox-outbound-calling
description: Use when the user asks the agent to place an outbound Inkbox phone call, call a phone number/contact, or call someone with a specific purpose or opening message.
user-invocable: false
---

# Inkbox outbound calling

Use this skill when the user asks you to call someone.

## Two calling paths

A call can go out over one of two lines. Match the line to the channel you're
already talking to the person on:

- **Your dedicated number** (`origination: "dedicated_number"`) — the agent's
  own phone number, the same line SMS and voice conversations use. Use this to
  call anyone reachable by phone, and whenever you're continuing a phone/SMS
  conversation.
- **The shared Inkbox iMessage line** (`origination: "shared_imessage_number"`)
  — call someone over the shared iMessage line you're already messaging them
  on. This only works if that person is connected to you over iMessage; if they
  aren't, the call is rejected and you should either fall back to your dedicated
  number or ask them to message you on iMessage first. The agent never sees or
  chooses the underlying shared number — Inkbox resolves it from the connection.

If only one path is available (you have just a number, or just iMessage), you
can omit `origination` and it resolves automatically. When both are available,
set it explicitly to match the conversation's channel.

## Optional tool

- `inkbox_place_call` — place an outbound call over either line.

## Workflow

1. Resolve the recipient to an E.164 phone number. If the user names a contact, use Inkbox contact lookup tools first.
2. Decide the line: are you continuing an iMessage conversation (use the shared iMessage line) or a phone/SMS conversation, or calling someone new (use your dedicated number)?
3. Call `inkbox_place_call` with:
   - `to_number`
   - `purpose` — required. Include the reason/topic the user gave; if none was given, say the user asked for a general call.
   - `origination` — set to match the channel when both paths are available; omit to auto-resolve when only one is.
   - `opening_message` — include when the user told you what to say first.
   - `context` — concise background the voice agent may need during the call.
4. Do not invent or request `client_websocket_url`; the plugin supplies the active Inkbox call bridge when the channel gateway is running.
5. When the callee answers, the call session starts with the supplied purpose/context instead of a generic greeting.

## Follow-ups

If the user asks you to call and then send a post-call email/SMS/note, include that request in the call context. During realtime calls, the voice agent can register post-call actions for the main agent to execute after hangup.
