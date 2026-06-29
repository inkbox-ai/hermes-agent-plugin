---
name: inkbox-outreach-sequence
description: Use when the user asks the agent to follow up with someone over multiple days, run a multi-step outreach campaign across email and SMS, or "ping them again in 2 days if they don't respond". Coordinates send tools with delayed scheduling and reply detection.
user-invocable: false
---

# Inkbox outreach sequence

Use this skill whenever the user asks for **multi-step outbound communication** — typically over several days, often combining email + SMS, with branching on whether the recipient replied.

## Hermes tool availability

- `inkbox_send_email` — primary outbound for longer-form steps
- `inkbox_send_sms` — short nudges; respects opt-in/opt-out
- `inkbox_list_contacts` — find a named recipient before starting
- `inkbox_lookup_contact` — resolve a known email/phone to a contact
- `inkbox_get_contact` — inspect a specific contact returned by list/lookup
- `inkbox_create_contact` / `inkbox_update_contact` / `inkbox_delete_contact` — maintain address-book entries when the user explicitly asks
- `inkbox_list_text_conversations` — check whether the recipient has replied to past SMS
- `inkbox_get_text_conversation` — inspect a specific SMS thread when the conversation id is known

Hermes does not expose historical email-read, note, vCard export/import, or email-forwarding tools. Use contact lookup/list/get or literal email addresses/phone numbers supplied by the user. If a named recipient lookup is ambiguous or does not include a usable destination, ask the user for the concrete destination before starting.

## Workflow

A sequence is a small state machine: a fixed list of steps, each with a channel and a delay from the prior step. Before running a step, the agent checks whether the recipient has replied since the last step; if so, the sequence exits early.

### 1. Plan before doing

Ask the user enough to fill in:
- **Who** is the recipient (contact lookup result, literal email address/phone number, or resolved current-contact context)
- **Steps:** ordered list, each with channel (email/SMS), copy, and delay from previous step
- **Exit condition:** typically "any reply"; sometimes "specific keyword in reply"

If the user is vague, propose a default 3-step sequence: email today → SMS in 2 days → email in 5 days.

### 2. Send step N

For each step:
1. **Check for reply since last step.**
   - If last step was email: Hermes cannot list historical email replies directly; only continue if the current turn or user-supplied context confirms no reply.
   - If last step was SMS: `inkbox_get_text_conversation(remotePhoneNumber)` and look for new inbound messages.
   - Reply found → exit the sequence.
2. **Send the step.** Email → `inkbox_send_email` (thread to last outbound if possible via `inReplyToMessageId`). SMS → `inkbox_send_sms`.
3. **Schedule the next check.** This skill does not schedule the delay itself — Hermes's scheduling layer or the user's own cron is responsible for re-invoking the agent at the next step time.

### 3. Record state

If the user wants the sequence persisted across sessions, provide a compact state summary they can store in their scheduler or notes system:

```
Outreach to Ada Lovelace (contact UUID xxx):
- Step 1 sent 2026-05-21 (email, id=...)
- Step 2 scheduled 2026-05-23 (SMS)
- Step 3 scheduled 2026-05-26 (email)
- Exit: any reply
```

Future sessions need that summary or an external scheduler to recover the plan.

## Hygiene

- **Don't ignore opt-out.** If `inkbox_send_sms` returns "Recipient has opted out", the entire sequence stops for that contact — do not switch them to email-only without the user's explicit OK.
- **Respect quiet hours.** SMS in particular shouldn't fire at 3am local. If the user hasn't specified timing, default to business hours in the recipient's time zone (or US Eastern if unknown).
- **Cap the depth.** More than ~5 outbound touches without a reply is harassment. Surface this concern to the user before queueing step 6+.
- **Thread email.** Always pass `inReplyToMessageId` on follow-up emails so the recipient sees one conversation, not five separate cold pitches.

## What this skill does NOT cover

- Bulk outreach to many contacts at once (current SDK + plugin scope is one recipient at a time per sequence).
- A/B testing different copy.
- Inbound parsing (intent detection on the reply body) — the agent reads the reply and decides, no auto-classifier.

## When you need more — raw Inkbox docs

If a delivery-status field, reply-detection edge case, or rate-cap detail isn't covered here, go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

Especially useful for understanding inbound `text.received` / `message.received` webhook payloads when reasoning about reply detection.
