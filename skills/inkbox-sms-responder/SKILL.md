---
name: inkbox-sms-responder
description: Use when the user asks to send a text, reply to an SMS, or process the SMS queue — also use automatically when an inbound `text.received` event arrives from Inkbox. Handles per-conversation context, opt-in/opt-out gates, and the 10DLC carrier propagation window.
user-invocable: false
---

# Inkbox SMS responder

The Inkbox plugin gives you a working phone number under an agent identity. Use this skill for any SMS or MMS conversation, including group chats — short, conversational, opt-in-gated.

## Required tools

- `inkbox_list_text_conversations` — start here for triage; includes group chats and returns conversation IDs
- `inkbox_get_text_conversation` — pull message history by `conversationId` or legacy `remotePhoneNumber`
- `inkbox_send_sms` — outbound by `conversationId`, one E.164 recipient, or a 2-8 recipient group

## Optional (allowlist needed)

- `inkbox_list_texts`, `inkbox_get_text` — low-level access
- `inkbox_mark_text_read`, `inkbox_mark_text_conversation_read` — clear unread state

## Workflow

1. **Pull conversations.** Call `inkbox_list_text_conversations` (defaults: `limit: 25`, newest-updated first, groups included). Each row shows `id`, `participants`, `isGroup`, `remotePhoneNumber` for 1:1, `latestText`, `unreadCount`, `totalCount`.

2. **Pick a conversation to handle.** Read the latest text in the row. If you need history, call `inkbox_get_text_conversation` with `conversationId: row.id` and a reasonable `limit` (50 is fine). Use `remotePhoneNumber` only for old 1:1 rows that do not have an ID.

3. **Compose and send.** Prefer `inkbox_send_sms` with `conversationId` when replying to an existing conversation, especially a group. For a new text, pass `to` as one E.164 number or a list of 2-8 E.164 numbers. Keep the tone conversational — SMS isn't email.

4. **Mark as handled** if you have the optional tool allowlisted: `inkbox_mark_text_conversation_read` with `conversationId`.

## Gates and errors

- **Opt-in required.** Recipients must have texted `START` to one of your Inkbox numbers. If they haven't, `inkbox_send_sms` returns the plain-language error "Recipient has not opted in to SMS." Surface this to the user; do not try to bypass.
- **Opt-out is final.** If a recipient texted `STOP`, sending returns "Recipient has opted out of SMS." Do not attempt to message them again on the same number.
- **Carrier propagation window.** Newly provisioned local numbers take ~10–15 min to propagate to carriers. During this window, sends return "Your Inkbox phone number is still propagating to carriers." Wait it out; don't retry tight-loop.
- **Toll-free numbers cannot send SMS** today. If the identity's phone is toll-free, sends will fail — recommend the user provision a local number via the setup wizard.
- **Rate cap.** Roughly 15 outbound sends per number per 24h. The plugin surfaces this as a 409. Pause sending and wait.
- **Group chats.** Reply only when the sender clearly addresses this agent or asks it to act. Do not comment on every group message. If an inbound group turn says no visible reply is warranted, return exactly `[SILENT]`.

## SMS-specific style

- Short. Often a single sentence is right.
- Don't include subject lines, signatures, or links unless explicitly asked.
- One thought per message; if it needs multiple parts, send them as separate messages rather than a 1600-char wall.

## What this skill does NOT cover

- Provisioning phone numbers (that's the setup wizard).
- Org-level SMS opt-in registry writes (admin-only, customer-managed 10DLC campaigns only).
- Creating a group with more than 8 recipients; carrier group MMS caps are lower than email-style threads.

## When you need more — raw Inkbox docs

If something here doesn't match what you're seeing, or you need API behavior this skill doesn't describe (field names, error codes, edge cases), go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

Prefer fetching these over guessing.
