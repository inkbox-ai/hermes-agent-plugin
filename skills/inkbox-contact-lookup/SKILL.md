---
name: inkbox-contact-lookup
description: Use when the user asks "who is X", "what's the email for Y", "find a contact named Z", "save this contact", or any question that needs contact context. Hermes can read Inkbox contacts visible to this identity, but does not expose contact write/admin tools.
user-invocable: false
---

# Inkbox contact lookup

Hermes is the Inkbox social-assistant tier. It receives contact context on inbound email, SMS, iMessage, and calls when Inkbox resolves the sender, and it can read contacts visible to the configured identity.

## Hermes tool availability

- Use `inkbox_list_contacts` for name-based searches like "who is Alex?".
- Use `inkbox_lookup_contact` when you have an exact or partial email/phone filter.
- Use `inkbox_get_contact` to fetch a full contact by UUID after list/lookup returns one.
- There is no `inkbox_create_contact`, `inkbox_update_contact`, `inkbox_delete_contact`, contact access, contact rule, or vCard export tool in Hermes.
- If an inbound message includes a resolved contact marker, treat that marker as the source of truth for the current sender.
- If the user asks to save or edit an address-book contact, explain that this Hermes plugin cannot modify Inkbox contacts directly and ask for a concrete email/phone/name to use in the current message instead.
- If the user asks for broad contact administration, direct them to Inkbox Console or to a host/plugin that exposes the Inkbox power-assistant contact tools.

## Workflow

1. **Use resolved inbound context first.** If the message starts with an `[inkbox:...]` marker containing contact fields, use those fields and do not invent missing identity details.
2. **Look up named people.** If the user asks about a named person, call `inkbox_list_contacts` with the name before saying you do not know.
3. **Use literal addresses when supplied.** If the user gives an email address or phone number, use it directly with `inkbox_send_email`, `inkbox_send_sms`, `inkbox_send_imessage`, or `inkbox_place_call`; optionally call `inkbox_lookup_contact` if the user asks who it belongs to.
4. **Ask when the target is ambiguous.** If lookup returns multiple plausible contacts, ask which contact the user means before sending or calling.
5. **Do not claim contact writes.** If the user asks you to create, update, delete, or export contacts, state that this Hermes installation does not expose those Inkbox contact tools.

## Access semantics

- Contact context is **filtered server-side** by per-identity grants. If Inkbox does not include a resolved contact marker, this identity may not have access or the sender may be unknown.
- Hermes contact read tools return only contacts visible to the configured identity.
- Grant management is handled by the `inkbox-identity-access` skill when the user asks to share contacts across Inkbox identities.

## What this skill does NOT cover

- Bulk vCard import — that's an admin flow, not exposed as an agent tool.
- Arbitrary workspace memory. Use the host's available memory/note tools only when they are actually registered.

## When you need more — raw Inkbox docs

If a lookup filter, contact field, or access semantics question isn't covered here, go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

Prefer fetching these over guessing field names or filter semantics.
