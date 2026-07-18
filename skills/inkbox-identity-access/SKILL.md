---
name: inkbox-identity-access
description: Use when the user asks which Inkbox agent identities can see a note, or asks to grant/revoke note access. Contacts are organization-wide and do not have access grants; Hermes does not expose note-access tools.
user-invocable: false
---

# Inkbox identity access

Use this skill when discussing per-identity visibility for Inkbox notes. Contacts and generated contact facts are organization-wide and cannot be shared or restricted per identity.

## Hermes tool availability

- Hermes does not register `inkbox_list_note_access`, `inkbox_grant_note_access`, or `inkbox_revoke_note_access`.
- This plugin cannot grant or revoke note access directly.
- Direct the user to Inkbox Console or an admin-capable host/plugin tier when they need note access changes.

## Workflow

1. If the request concerns a contact, explain that active contacts and generated contact facts are already organization-wide.
2. If the request concerns a note, state that Hermes cannot change note access directly and summarize the requested change for Console/admin execution.
3. Note grants use an explicit `identityId`; notes do not support wildcard grants.
4. If the user gives an agent handle instead of an identity UUID, explain that Console/admin tooling may need the identity id.

## Safety

Note access changes affect what other Inkbox agent identities can see. Confirm the target identity and note before requesting a change.
