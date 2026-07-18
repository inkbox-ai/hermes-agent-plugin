---
name: inkbox-contact-lookup
description: Use when the user asks "who is X", "what's the email for Y", "find a contact named Z", "save this contact", or any question that needs organization-wide contact context.
user-invocable: false
---

# Inkbox contact lookup

Hermes is the Inkbox social-assistant tier. It receives contact context on inbound email, SMS, iMessage, and calls when Inkbox resolves the sender, and it can read or update organization-wide contacts.

## Hermes tool availability

- Use `inkbox_list_contacts` for name-based searches like "who is Alex?".
- Use `inkbox_lookup_contact` when you have an exact or partial email/phone filter.
- Use `inkbox_get_contact` to fetch a full contact by UUID after list/lookup returns one.
- Use `inkbox_create_contact` when the user asks you to save a new person or contact card.
- Use `inkbox_update_contact` when the user asks you to change an existing contact; look up the contact first if you do not already have its UUID.
- Use `inkbox_delete_contact` only after the target contact is explicit and confirmed.
- There is no vCard export/import or contact rule tool in Hermes. Contacts do not have per-identity access tools.
- If an inbound message includes a resolved contact marker, treat that marker as the source of truth for the current sender.
- If the user asks for broad contact administration, direct them to Inkbox Console or to a host/plugin that exposes the Inkbox power-assistant admin tools.

## Workflow

1. **Use resolved inbound context first.** If the message starts with an `[inkbox:...]` marker containing contact fields, use those fields and do not invent missing identity details.
2. **Look up named people.** If the user asks about a named person, call `inkbox_list_contacts` with the name before saying you do not know.
3. **Use literal addresses when supplied.** If the user gives an email address or phone number, use it directly with `inkbox_send_email`, `inkbox_send_sms`, `inkbox_send_imessage`, or `inkbox_place_call`; optionally call `inkbox_lookup_contact` if the user asks who it belongs to.
4. **Create contacts when asked.** If the user asks you to save someone new and provides at least one useful field, call `inkbox_create_contact`.
5. **Update contacts by UUID.** If the user asks you to edit a contact, resolve the contact with list/lookup/get first, then call `inkbox_update_contact` with only the fields that should change. Omitted fields remain unchanged.
6. **Delete cautiously.** If the user asks to delete a contact, confirm the exact target when there is any ambiguity, then call `inkbox_delete_contact` with the UUID.
7. **Ask when the target is ambiguous.** If lookup returns multiple plausible contacts, ask which contact the user means before sending, calling, updating, or deleting.

## Contact memory semantics

- Active contacts and generated contact facts are organization-wide.
- Contact `notes` are user-managed profile text. Generated facts are separate, source-grounded memory; do not copy or overwrite them through the `notes` field.
- Correspondence remains limited to the configured identity's authorized email, text, iMessage, and call history.
- The installed SDK does not expose unified contact correspondence or generated-fact reads, so Hermes does not register those tools. Do not reconstruct them with raw requests.

## What this skill does NOT cover

- Bulk vCard import — that's an admin flow, not exposed as an agent tool.
- vCard export — not exposed as an agent tool.
- Arbitrary workspace memory. Use the host's available memory/note tools only when they are actually registered.

## When you need more — raw Inkbox docs

If a lookup filter, contact field, or access semantics question isn't covered here, go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

Prefer fetching these over guessing field names or filter semantics.
