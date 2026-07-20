---
name: inkbox-imessage-responder
description: Use when the user asks to send an iMessage, reply on iMessage, or explain how to reach the agent over iMessage — also use automatically when an inbound `imessage.received` event arrives from Inkbox. Handles the connect/router model, the recipient-first rule, and tapback reactions.
user-invocable: false
---

# Inkbox iMessage responder

The Inkbox plugin makes this agent reachable over iMessage. Unlike SMS, the agent does not own an iMessage number: people connect through the Inkbox iMessage router, and each connected person gets a dedicated conversation with this agent. Use this skill for any iMessage conversation — short, conversational, reply-driven.

## How the channel works

- A person texts the connect command (e.g. `connect @agent-handle`) to the Inkbox iMessage router number from their iPhone. Get both with `inkbox_imessage_triage_number`.
- Inkbox texts them back from the number assigned to their conversation with this agent. All chat happens in that thread.
- **Recipient-first:** the agent cannot message anyone over iMessage until that person has messaged it first. There is no cold outreach on this channel. If an outbound send returns a 409-style error saying the recipient hasn't messaged yet or is no longer connected, tell the user the person needs to (re)connect and send a message first.
- If someone asks "how do I iMessage you?", answer with the router number and connect command from `inkbox_imessage_triage_number`.

## Calling someone on iMessage Shared Line

If a person you're connected to over iMessage asks you to call them (or you decide to call), place the call over the **shared iMessage line** — the same line you're already messaging them on — with `inkbox_place_call` and `origination: "shared_imessage_number"`. Because the current conversation is on iMessage, that's already the default line, but set it explicitly to be sure. Do **not** call an iMessage contact from your dedicated phone number; they reach you on iMessage, and shared-line calling only works while they stay connected. If the call is refused because they aren't connected, ask them to reconnect over iMessage first (or, only if you have their number for that purpose, call your dedicated line instead).

## Required tools

- `inkbox_list_imessage_conversations` — start here for triage; returns conversation IDs, latest-message previews, unread counts, and assignment status
- `inkbox_get_imessage_conversation` — pull message history (includes live tapback reactions on each message)
- `inkbox_send_imessage` — outbound by `conversationId` (preferred) or `to` E.164

## Optional (allowlist needed)

- `inkbox_imessage_triage_number` — router number + connect command for onboarding new people
- `inkbox_list_imessage_assignments` — who is actively connected to this agent right now (one row per recipient)
- `inkbox_send_imessage_reaction` — tapback (love/like/dislike/laugh/emphasize/question) on a received message
- `inkbox_mark_imessage_conversation_read` — send a read receipt and clear unread state

## Workflow

1. **Pull conversations.** Call `inkbox_list_imessage_conversations` (defaults: `limit: 25`). Each row includes the conversation ID, remote number, latest text, unread count, total count, and assignment status. Field names may be snake_case or camelCase depending on the host. `released` means that person disconnected, so a reply will fail until they reconnect through the router; tell them how instead of retrying.

2. **Pick a conversation to handle.** If you need history, call `inkbox_get_imessage_conversation` with `conversationId: row.id`. Inbound messages may carry `reactions` — live tapbacks the person put on a message.

3. **Compose and send — reply vs. reach out.** These are different, and mixing them double-sends:
   - **Replying to the iMessage that just woke you** (this turn carries an `[inkbox:imessage …]` marker): **just write your reply.** It is delivered automatically as an iMessage into that same thread. Do **NOT** also call `inkbox_send_imessage` for that reply — the tool would send the same message a second time.
   - **Reaching a different conversation or recipient:** use `inkbox_send_imessage` with `conversationId` (preferred) or `to`. (Remember iMessage is recipient-first — no cold outreach.)

   Keep the tone conversational — iMessage is a chat thread, not email. A `sendStyle` (confetti, balloons, …) is available for celebratory moments; use sparingly.

   **Attachments:** when replying in the current iMessage thread, include `MEDIA:/absolute/local/path` in the reply and the Inkbox channel uploads it as a native attachment. When sending through `inkbox_send_imessage`, pass local files with `mediaPaths`; use `mediaUrls` only for already-hosted public HTTP(S) URLs. Never put `/tmp/...`, `file://...`, or another local path in `mediaUrls`.

4. **React when a reply would be noise.** A tapback via `inkbox_send_imessage_reaction` (e.g. `like` on an acknowledgment) often beats a filler message.

5. **Mark as handled** if you have the optional tool allowlisted: `inkbox_mark_imessage_conversation_read` with `conversationId` — this also shows the sender a read receipt.

## Inbound markers

Inbound iMessages arrive prefixed `[inkbox:imessage from=+1555… conversation_id=… | contact…]`. Bursts may arrive as `[inkbox:imessage_burst …]`, and attachments may add `[inkbox:imessage_attachment …]` lines. Use the marker for routing context; never echo it back.

While you compose a reply, the recipient automatically sees a typing indicator (the Inkbox bridge pulses it until your message sends), so there is no separate "I'm typing" tool to call.

## Reacting to your messages (tapbacks)

When someone puts a tapback on one of **your** messages, you receive a turn prefixed `[inkbox:imessage_reaction from=+1555… reaction=<type> conversation_id=… target_message_id=… | contact…]` followed by a short response policy. A reaction is a lightweight signal, not always a request for a reply:

- A `question` tapback usually asks for clarification or a follow-up — replying is normally warranted.
- `emphasize` may invite a brief acknowledgement or follow-up.
- `love` / `like` / `laugh` / `dislike` are usually just acknowledgements that need no response.

Decide based on the reaction and the conversation. **If no visible reply is warranted, return exactly `[SILENT]`** — the Inkbox bridge drops it and nothing is sent. If a response genuinely adds value, write the normal reply once and let the current iMessage thread deliver it automatically; use `inkbox_send_imessage` only for a different conversation.
