---
name: inkbox-email-triage
description: Use when an inbound email arrives at the agent's Inkbox mailbox or when the user asks the agent to send/reply to email. Hermes can send and reply from current inbound context, but does not expose mailbox queue/read/forward/archive tools.
user-invocable: false
---

# Inkbox email triage

Hermes gives the agent a working Inkbox mailbox and current inbound email context. This social-tier plugin does not register historical mailbox queue/read, forward, or mark-read tools.

## Hermes tool availability

- `inkbox_send_email` — send or reply to email from the configured Inkbox identity.
- There is no `inkbox_list_unread_emails`, `inkbox_get_email`, `inkbox_get_email_thread`, `inkbox_forward_email`, or `inkbox_mark_emails_read` tool in Hermes.

## Workflow

1. **Current inbound email.** Use the email body, sender, subject, thread id, and message id supplied in the current turn. Do not claim to have fetched additional mailbox history.
2. **Reply — write vs. call the tool.** These are different, and mixing them double-sends:
   - **Replying to the email that just woke you** (this turn carries an `[inkbox:email …]` marker): **just write your reply.** It is delivered automatically as a threaded email reply to that sender. Do **NOT** also call `inkbox_send_email` for that reply — the tool would send the same message a second time.
   - **Emailing a different thread or recipient:** use `inkbox_send_email` with the reply/thread metadata when available so mail clients group the response correctly.
3. **New outbound email.** If the user provides a recipient address, subject, and body, use `inkbox_send_email`.
4. **Queue/read/forward/archive requests.** If the user asks to triage the unread queue, inspect old threads, forward a stored email, or mark messages read, explain that this Hermes installation does not expose those mailbox tools.

## Reply hygiene

- Always thread replies when inbound metadata gives you a reply target.
- Keep the same subject (or prefix with `Re:` once, not stacked).
- If you lack thread context, ask the user for the missing prior-message context instead of inventing it.

## Errors you may see

- 403 with `recipient_not_opted_in` — only applies to SMS, not email. If you see this on email, surface it as-is.
- 404 — message id is wrong or the message has been deleted; skip and move on.

## When you need more — raw Inkbox docs

If something here doesn't match what you're seeing, or you need API behavior this skill doesn't describe (field names, error codes, edge cases), go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

Prefer fetching these over guessing.
