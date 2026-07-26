---
name: inkbox-contact-rules
description: Use when the user wants to block, allow, delete, or list Inkbox contact-rule filters for the agent's mailbox or phone number. Hermes does not expose contact-rule edit tools; guide the user to Inkbox Console or explain the limitation.
user-invocable: false
---

# Inkbox contact rules

Use this skill when discussing who can reach the agent's Inkbox mailbox or phone number.

## Hermes tool availability

- Hermes relies on Inkbox server-side mailbox and phone contact rules for admission.
- This Hermes plugin does not register `inkbox_list_mail_contact_rules`, `inkbox_create_mail_contact_rule`, `inkbox_update_mail_contact_rule`, `inkbox_delete_mail_contact_rule`, `inkbox_list_phone_contact_rules`, `inkbox_create_phone_contact_rule`, `inkbox_update_phone_contact_rule`, or `inkbox_delete_phone_contact_rule`.
- If the user asks to change allow/block rules, tell them the Hermes plugin cannot edit those rules directly and direct them to Inkbox Console.
- If the user provides a specific rule they want, summarize the exact setting they should create in Console.

## Workflow

1. Identify whether the request is for mailbox rules, phone/SMS/call rules, or both.
2. For mailbox rules, explain the intended settings:
   - `matchType: "exact_email"` for one sender address.
   - `matchType: "domain"` for a whole sender domain.
   - `action: "block"` to reject matching mail.
   - `action: "allow"` to permit matching mail when whitelist mode is active.
3. For phone rules, explain the intended settings:
   - `matchType: "exact_number"` for E.164 numbers.
   - Rules apply to SMS and voice calls for that phone number.
4. Explain that blocked inbound messages/calls may be rejected before the agent sees an event.

## Safety

Do not switch a channel into whitelist-only behavior unless a tool explicitly supports filter-mode changes and the user clearly requests that behavior. Whitelist mode blocks everyone who is not explicitly allowed.
