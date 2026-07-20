---
name: inkbox-identity-access
description: Use when the user asks which Inkbox agent identities can see a contact or note, or asks to grant/revoke cross-identity access to contacts or notes. Hermes does not expose identity-access tools; explain the limitation.
user-invocable: false
---

# Inkbox identity access

Use this skill when discussing per-identity visibility for Inkbox contacts and notes.

## Hermes tool availability

- Hermes does not register `inkbox_list_contact_access`, `inkbox_grant_contact_access`, `inkbox_revoke_contact_access`, `inkbox_list_note_access`, `inkbox_grant_note_access`, or `inkbox_revoke_note_access`.
- This plugin cannot grant or revoke cross-identity access directly.
- Direct the user to Inkbox Console or an admin-capable host/plugin tier when they need access changes.

## Workflow

1. Clarify whether the request is about contact visibility or note visibility.
2. If the user needs an actual access change, state that Hermes cannot perform it directly and summarize the requested change for Console/admin execution.
3. For contacts, explain the concepts:
   - Grant a specific identity with `identityId`.
   - Use `wildcard: true` only when the user wants every active identity to see the contact.
   - Revoke by `identityId`.
4. For notes, explain the concepts:
   - Grant and revoke only by explicit `identityId`; notes do not support wildcard grants.
5. If the user gives an agent handle instead of an identity UUID, explain that Console/admin tooling may need the identity id.

## Safety

Access changes affect what other Inkbox agent identities can see. Confirm the target identity and object before granting broad or wildcard contact access.
