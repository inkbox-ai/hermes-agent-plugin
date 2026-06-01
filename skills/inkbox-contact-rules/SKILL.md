---
name: inkbox-contact-rules
description: Use when the user wants to block, allow, pause, delete, or list Inkbox contact-rule filters for the agent's mailbox or phone number, including email allow/block rules, SMS/call allow/block rules, allowlists, blocklists, spam blocking, or "only accept from" requests.
user-invocable: false
---

# Inkbox contact rules

Use this skill when managing who can reach the agent's Inkbox mailbox or phone number.

## Optional tools

- `inkbox_list_mail_contact_rules`
- `inkbox_create_mail_contact_rule`
- `inkbox_update_mail_contact_rule`
- `inkbox_delete_mail_contact_rule`
- `inkbox_list_phone_contact_rules`
- `inkbox_create_phone_contact_rule`
- `inkbox_update_phone_contact_rule`
- `inkbox_delete_phone_contact_rule`

## Workflow

1. List existing rules before making changes when the user is ambiguous.
2. For mailbox rules:
   - `matchType: "exact_email"` for one sender address.
   - `matchType: "domain"` for a whole sender domain.
   - `action: "block"` to reject matching mail.
   - `action: "allow"` to permit matching mail when whitelist mode is active.
3. For phone rules:
   - `matchType: "exact_number"` for E.164 numbers.
   - Rules apply to SMS and voice calls for that phone number.
4. Use `status: "paused"` to temporarily disable a rule without deleting it.
5. Explain that blocked inbound messages/calls may be rejected before the agent sees an event.

## Safety

Do not switch a channel into whitelist-only behavior unless a tool explicitly supports filter-mode changes and the user clearly requests that behavior. Whitelist mode blocks everyone who is not explicitly allowed.
