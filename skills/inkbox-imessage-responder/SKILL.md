---
name: inkbox-imessage-responder
description: Use when the user asks to send an iMessage, start or reply to an iMessage group, or explain how to reach the agent over iMessage — also use automatically when an inbound `imessage.received` event arrives from Inkbox. Handles shared and dedicated lines, group response policy, the recipient-first rule, and tapback reactions.
user-invocable: false
---

# Inkbox iMessage responder

The Inkbox plugin makes this agent reachable over iMessage. Most identities use the shared Inkbox router, while some have a dedicated inbound or dedicated outbound iMessage number. Use this skill for any iMessage conversation — short, conversational, reply-driven.

## How the channel works

- On the shared service, a person texts the connect command (e.g. `connect @agent-handle`) to the Inkbox iMessage router number from their iPhone. Get both with `inkbox_imessage_triage_number`. Inkbox texts them back from the number assigned to their conversation with this agent.
- **Recipient-first lines:** shared service and dedicated inbound lines cannot initiate a conversation. The person must message first. If an outbound send returns a 409-style error saying the recipient has not messaged yet or is no longer connected, explain the required setup instead of retrying.
- **Dedicated outbound lines:** the agent may initiate a 1:1 conversation or a group with 2–8 distinct E.164 recipients, subject to contact rules. Group creation is not available on shared or dedicated inbound lines.
- Existing 1:1 and group conversations are always addressed by `conversationId` when replying.
- If someone asks "how do I iMessage you?", answer with the router number and connect command from `inkbox_imessage_triage_number`.

## Calling someone on iMessage Shared Line

If a person you're connected to over iMessage asks you to call them (or you decide to call), place the call over the **shared iMessage line** — the same line you're already messaging them on — with `inkbox_place_call` and `origination: "shared_imessage_number"`. Because the current conversation is on iMessage, that's already the default line, but set it explicitly to be sure. Do **not** call an iMessage contact from your dedicated phone number; they reach you on iMessage, and shared-line calling only works while they stay connected. If the call is refused because they aren't connected, ask them to reconnect over iMessage first (or, only if you have their number for that purpose, call your dedicated line instead).

## Required tools

- `inkbox_list_imessage_conversations` — start here for triage; includes groups by default and returns conversation IDs, participants, latest-message previews, unread counts, and assignment status
- `inkbox_get_imessage_conversation` — pull 1:1 or group history (includes sender metadata and live tapback reactions)
- `inkbox_send_imessage` — reply by `conversationId`, or initiate by `to`; a 2–8 recipient `to` list requires a dedicated outbound line

## Optional (allowlist needed)

- `inkbox_imessage_triage_number` — router number + connect command for onboarding new people
- `inkbox_list_imessage_assignments` — who is actively connected to this agent right now (one row per recipient)
- `inkbox_send_imessage_reaction` — tapback (love/like/dislike/laugh/emphasize/question) on a received message
- `inkbox_mark_imessage_conversation_read` — send a read receipt and clear unread state for a 1:1 conversation; groups do not support read receipts

## Workflow

1. **Pull conversations.** Call `inkbox_list_imessage_conversations` (defaults: `limit: 25`, groups included). Group rows have `is_group`, `participants`, and no assignment; 1:1 rows include a remote number and assignment status. Field names may be snake_case or camelCase depending on the host. On a 1:1 row, `released` means that person disconnected, so a reply will fail until they reconnect through the router; tell them how instead of retrying.

2. **Pick a conversation to handle.** If you need history, call `inkbox_get_imessage_conversation` with `conversationId: row.id`. Inbound messages may carry `reactions` — live tapbacks the person put on a message.

3. **Compose and send — reply vs. reach out.** These are different, and mixing them double-sends:
   - **Replying to the iMessage that just woke you** (this turn carries an `[inkbox:imessage …]` or `[inkbox:group_imessage …]` marker): **just write your reply.** It is delivered automatically into that same thread. Do **NOT** also call `inkbox_send_imessage` for that reply — the tool would send the same message a second time.
   - **Reaching a different existing conversation:** use `inkbox_send_imessage` with `conversationId`.
   - **Starting a new conversation:** use `to` with one E.164 number for 1:1 or 2–8 distinct numbers for a group. Starting a group requires a dedicated outbound iMessage line. Shared and dedicated inbound lines are recipient-first.

   Keep the tone conversational — iMessage is a chat thread, not email. A `sendStyle` (confetti, balloons, …) is available for celebratory moments; use sparingly.

   **Attachments:** when replying in the current iMessage thread, include `MEDIA:/absolute/local/path` in the reply and the Inkbox channel uploads it as a native attachment. When sending through `inkbox_send_imessage`, pass local files with `mediaPaths`; use `mediaUrls` only for already-hosted public HTTP(S) URLs. Never put `/tmp/...`, `file://...`, or another local path in `mediaUrls`.

4. **React when a reply would be noise.** A tapback via `inkbox_send_imessage_reaction` (e.g. `like` on an acknowledgment) often beats a filler message.

5. **Mark a 1:1 as handled** if you have the optional tool allowlisted: `inkbox_mark_imessage_conversation_read` with `conversationId` — this also shows the sender a read receipt. Do not call it for a group.

## Inbound markers

Inbound 1:1 iMessages arrive prefixed `[inkbox:imessage from=+1555… conversation_id=… | contact…]`. Groups use `[inkbox:group_imessage conversation_id=… from=+1555… participants=… reply_mode=conversation_id | contact…]` followed by a response policy. Bursts use the corresponding `*_burst` marker, and attachments may add `[inkbox:imessage_attachment …]` lines. Use the marker for routing context; never echo it back.

In a 1:1, the recipient automatically sees a typing indicator while you compose a reply. Group iMessage does not support typing indicators or read receipts.

## Group response policy

You receive every inbound group message so the session retains context. Reply only when the latest message clearly addresses this agent, asks it to act, or a visible answer would be expected from the agent. Treat ordinary chatter as context only. If no visible reply is warranted, return exactly `[SILENT]`.

## Reacting to your messages (tapbacks)

When someone puts a tapback on one of **your** messages, you receive a turn prefixed `[inkbox:imessage_reaction …]` for a 1:1 or `[inkbox:group_imessage_reaction …]` for a group, followed by a short response policy. A reaction is a lightweight signal, not always a request for a reply:

- A `question` tapback usually asks for clarification or a follow-up — replying is normally warranted.
- `emphasize` may invite a brief acknowledgement or follow-up.
- `love` / `like` / `laugh` / `dislike` are usually just acknowledgements that need no response.

Decide based on the reaction and the conversation. **If no visible reply is warranted, return exactly `[SILENT]`** — the Inkbox bridge drops it and nothing is sent. Reply normally (via `inkbox_send_imessage`) only when a response genuinely adds value.
