---
name: inkbox-contact-lookup
description: Use when the user asks "who is X", "what's the email for Y", "find a contact named Z", "save this contact", or any question that needs to resolve, create, or update an Inkbox contact record. Also use as a prereq before sending email/SMS to a name (rather than a literal address).
user-invocable: false
---

# Inkbox contact lookup

The Inkbox plugin gives the agent access to contacts visible to the configured identity. Use this skill to resolve "who is X", find recipient details, or save contact details into Inkbox contacts.

## Required tools

- `inkbox_lookup_contact` — reverse-lookup by exactly one filter (email, phone, emailDomain, emailContains, phoneContains)
- `inkbox_get_contact` — fetch a full contact record by UUID
- `inkbox_list_contacts` — free-text search via `q`, with `order: "recent" | "name"`
- `inkbox_create_contact` — create an Inkbox address-book contact when the user asks to save a person/contact

## Optional (allowlist needed)

- `inkbox_update_contact` — update an existing contact after lookup/get confirms the target UUID
- `inkbox_export_contact_vcard` — vCard 4.0 string export

## Workflow

1. **Lookup-first.** If the user mentions an email or phone, try `inkbox_lookup_contact` first — it's the cheapest path. Pass exactly one filter:
   - `{ "email": "ada@example.com" }`
   - `{ "phone": "+15551234567" }`
   - `{ "emailDomain": "example.com" }`
   - `{ "emailContains": "ada" }`
   - `{ "phoneContains": "555" }`

2. **Fall back to free-text search.** For name-based queries ("find Ada Lovelace"), use `inkbox_list_contacts` with `q: "Ada"`. Match by `givenName` + `familyName` in the results.

3. **Save contact details in Inkbox.** If the user asks to save a contact, do not use workspace notes. First try `inkbox_lookup_contact` with any known phone/email. If nothing exists, call `inkbox_create_contact` with known name/email/phone fields and put loose context in the contact `notes` field.

4. **Update existing contacts when allowed.** If lookup/list returns the intended person and `inkbox_update_contact` is available, use it to add/correct phone, email, name, company, job title, or notes. If update is unavailable, tell the user the existing contact was found but update is not allowlisted.

5. **Pull the full record when you need it.** If lookup/list returns a contact and you need addresses, vCard fields, or all phone/email entries, call `inkbox_get_contact` with the `id`.

6. **Then act.** Use the resolved email or phone to call `inkbox_send_email`, `inkbox_send_sms`, etc.

## Access semantics

- Contact reads are **filtered server-side** by per-identity grants. If a contact doesn't appear in your list/lookup results, this identity doesn't have access — not necessarily that the contact doesn't exist.
- Contacts created through `inkbox_create_contact` are Inkbox address-book records, not workspace memories.
- Lookup may return zero results. Don't retry with the same filter; either widen (try `emailDomain` instead of `email`) or fall back to a free-text `list` query.
- Grant management is handled by the `inkbox-identity-access` skill when the user asks to share contacts across Inkbox identities.

## What this skill does NOT cover

- Bulk vCard import — that's an admin flow, not exposed as an agent tool.
- Arbitrary workspace memory. Use Inkbox notes for persistent notes and Inkbox contacts for address-book facts.

## When you need more — raw Inkbox docs

If a lookup filter, contact field, or access semantics question isn't covered here, go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

Prefer fetching these over guessing field names or filter semantics.
