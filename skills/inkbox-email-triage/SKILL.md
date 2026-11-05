---
name: inkbox-email-triage
description: Use for email triage, unread mail, historical messages and threads, attachments, forwarding, and user-requested outbound email.
user-invocable: false
---

# Inkbox email triage

Use the configured identity's mailbox and current inbound email context.

## Hermes tool availability

- `inkbox_list_emails`: list a bounded page with date/direction filters and optional unread or sender/subject/snippet matching.
- `inkbox_get_email` and `inkbox_get_email_thread`: read full messages or chronological thread summaries.
- `inkbox_download_email_attachment`: save a stored attachment locally.
- `inkbox_send_email` and `inkbox_forward_email`: send, reply-all, or forward with attachments.
- `inkbox_lookup_message_send`: recover a message ID after an uncertain send.

## Workflow

1. **Current inbound email.** Use supplied context; fetch the message or thread when additional history is needed.
2. **Reply — write vs. call the tool.** These are different, and mixing them double-sends:
   - **Replying to the email that just woke you** (this turn carries an `[inkbox:email …]` marker): **just write your reply.** It is delivered automatically as a threaded email reply to that sender. Do **NOT** also call `inkbox_send_email` for that reply — the tool would send the same message a second time.
   - **Emailing a different thread or recipient:** use `inkbox_send_email` with the reply/thread metadata when available so mail clients group the response correctly.
3. **New outbound email.** If the user provides a recipient address, subject, and body, use `inkbox_send_email`.
4. **Unread/history requests.** Use `inkbox_list_emails` with `unread_only` or `q` as appropriate. These filters apply within each scanned page; follow `next_offset` even when no messages matched. Search does not include full bodies. Narrow date bounds for large mailboxes.
5. **Read and forward.** Get thread summaries by thread UUID and full bodies by message UUID. Reading one inbound email marks it read; reading a thread does not. Forward a stored message with `inkbox_forward_email`, which includes original attachments by default.
6. **Attachments.** Send approved local files with `attachments: [{"path": "/path/to/file"}]`. Inline images need a unique `content_id` and a matching `cid:` reference in `body_html`. Download stored attachments by their exact metadata filename.
7. **Uncertain sends.** Retain `idempotency_key` and `operation`, then inspect `inkbox_lookup_message_send`. Retry only with the same key and unchanged content. A missing lookup is not proof of failure, and acceptance is not confirmed delivery.

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
