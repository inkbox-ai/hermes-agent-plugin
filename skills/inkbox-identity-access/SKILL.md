---
name: inkbox-identity-access
description: Use when the user asks which Inkbox agent identities can see a contact or note, or asks to grant/revoke cross-identity access to contacts or notes.
user-invocable: false
---

# Inkbox identity access

Use this skill when managing per-identity visibility for Inkbox contacts and notes.

## Optional tools

- `inkbox_list_contact_access`
- `inkbox_grant_contact_access`
- `inkbox_revoke_contact_access`
- `inkbox_list_note_access`
- `inkbox_grant_note_access`
- `inkbox_revoke_note_access`

## Workflow

1. Resolve the contact or note id first. Use lookup/list/get tools if the user names a person or note.
2. List current access before changing it when possible.
3. For contacts:
   - Grant a specific identity with `identityId`.
   - Use `wildcard: true` only when the user wants every active identity to see the contact.
   - Revoke by `identityId`.
4. For notes:
   - Grant and revoke only by explicit `identityId`; notes do not support wildcard grants.
5. If the user gives an agent handle instead of an identity UUID and no tool can resolve handles, explain that you need the identity id or a contact/note access listing that contains it.

## Safety

Access changes affect what other Inkbox agent identities can see. Confirm the target identity and object before granting broad or wildcard contact access.
