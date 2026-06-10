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

## Required tools

- `inkbox_list_imessage_conversations` — start here for triage; returns conversation IDs, latest-message previews, unread counts, and `assignment_status`
- `inkbox_get_imessage_conversation` — pull message history (includes live tapback reactions on each message)
- `inkbox_send_imessage` — outbound by `conversationId` (preferred) or `to` E.164

## Optional (allowlist needed)

- `inkbox_imessage_triage_number` — router number + connect command for onboarding new people
- `inkbox_list_imessage_assignments` — who is actively connected to this agent right now (one row per recipient)
- `inkbox_send_imessage_reaction` — tapback (love/like/dislike/laugh/emphasize/question) on a received message
- `inkbox_mark_imessage_conversation_read` — send a read receipt and clear unread state

## Workflow

1. **Pull conversations.** Call `inkbox_list_imessage_conversations` (defaults: `limit: 25`). Each row shows `id`, `remote_number`, `latest_text`, `unread_count`, `total_count`, and `assignment_status` — `released` means that person disconnected, so a reply will fail until they reconnect through the router; tell them how instead of retrying.

2. **Pick a conversation to handle.** If you need history, call `inkbox_get_imessage_conversation` with `conversationId: row.id`. Inbound messages may carry `reactions` — live tapbacks the person put on a message.

3. **Compose and send.** Reply with `inkbox_send_imessage` using `conversationId`. Keep the tone conversational — iMessage is a chat thread, not email. A `sendStyle` (confetti, balloons, …) is available for celebratory moments; use sparingly.

4. **React when a reply would be noise.** A tapback via `inkbox_send_imessage_reaction` (e.g. `like` on an acknowledgment) often beats a filler message.

5. **Mark as handled** if you have the optional tool allowlisted: `inkbox_mark_imessage_conversation_read` with `conversationId` — this also shows the sender a read receipt.

## Inbound markers

Inbound iMessages arrive prefixed `[inkbox:imessage from=+1555… conversation_id=… | contact…]` (bursts arrive as `[inkbox:imessage_burst …]` with one line per fragment). Use the marker for routing context; never echo it back.
