---
name: inkbox-notes-memory
description: Use when the user asks to save, remember, list, retrieve, update, or delete notes in Inkbox. This is for persistent Inkbox notes, not workspace-local memory.
user-invocable: false
---

# Inkbox notes memory

The Inkbox plugin exposes persistent notes scoped by the configured Inkbox identity. Use these tools when a user asks to save a note, remember free-form context in Inkbox, or retrieve prior Inkbox notes.

## Required tools

- `inkbox_list_notes` — list/search notes visible to this identity
- `inkbox_get_note` — fetch a full note by UUID
- `inkbox_create_note` — create a persistent Inkbox note

## Optional (allowlist needed)

- `inkbox_update_note` — update an existing note by UUID
- `inkbox_delete_note` — delete a note by UUID

## Workflow

1. **Use Inkbox notes for free-form memory.** When the user says "save a note", "remember this in Inkbox", or asks for durable non-contact context, call `inkbox_create_note`.

2. **Do not store contact details as notes.** If the user asks to save a person, phone number, email, address-book entry, or "my contact", use the contact workflow: lookup first, then create or update an Inkbox contact.

3. **Search before editing.** For "update the note about X", call `inkbox_list_notes` with a focused query, then `inkbox_get_note` if needed before using `inkbox_update_note`.

4. **Be explicit about optional tools.** If update/delete is not available, say that the note was found but the update/delete tool is not allowlisted.

## Access semantics

- Note reads are filtered server-side by the Inkbox identity's access grants.
- Notes are persistent Inkbox records. They are different from Hermes workspace notes/memory and should be used whenever the user specifically refers to Inkbox notes.

## When you need more - raw Inkbox docs

If a notes field, access rule, or error behavior is not covered here, use the raw docs:

- **https://inkbox.ai/llms.txt** - LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** - the full Inkbox documentation concatenated as one markdown file.
