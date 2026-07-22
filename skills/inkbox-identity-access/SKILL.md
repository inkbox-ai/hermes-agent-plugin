---
name: inkbox-identity-access
description: Use when the user asks which Inkbox agent identities can see a contact or note, or asks to grant/revoke cross-identity note access. Contacts are organization-wide; Hermes does not expose note-access tools.
user-invocable: false
---

# Inkbox identity access

Use this skill when explaining organization-wide contact visibility or per-identity note access.

## Hermes tool availability

- Hermes does not register `inkbox_list_note_access`, `inkbox_grant_note_access`, or `inkbox_revoke_note_access`.
- Contacts do not have per-identity access controls.
- Direct the user to Inkbox Console or a host with note-access tools when they need note access changes.

## Workflow

1. Clarify whether the request is about contact visibility or note visibility.
2. For contacts, explain that every identity in the organization can see them and access cannot be granted or revoked per identity.
3. For notes, explain that grants use an explicit `identityId` and that Hermes cannot change them directly.
4. Summarize the requested note access change for execution in Inkbox Console or a host with note-access tools.
5. If the user gives an agent handle instead of an identity UUID, explain that note access tooling may need the identity id.

## Safety

Note access changes affect what other Inkbox agent identities can see. Confirm the target identity and note before requesting a change.
