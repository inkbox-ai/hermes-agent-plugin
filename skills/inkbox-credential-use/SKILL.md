---
name: inkbox-credential-use
description: Use when the user requests an accessible credential or current 2FA code, or asks the agent to log into a service using Inkbox Vault.
user-invocable: false
---

# Inkbox credentials

Use `inkbox_list_vault_secrets` to find authorized metadata, then `inkbox_get_vault_secret` with the selected `secret_id` for the requested operation. Login results omit TOTP seeds. Use `inkbox_get_totp_code` for a current 2FA code and validity window; refetch after expiry rather than storing the code.

Decryption requires `INKBOX_HERMES_VAULT_KEY` in the local gateway process. If locked, explain that local configuration and restart are needed. Never ask the user to send the key through chat or pass it as a tool argument. Metadata listing does not require this plugin key. Do not infer access from an old result: fetch current credentials only when needed, and honor revoked/deleted-secret errors.

Use credentials transiently for the authorized task; do not echo plaintext unless the user specifically requested the value, and never save credentials, keys, TOTP seeds, or current codes in session memory. Do not claim to have retrieved a credential if the tool failed.

The plugin has no tools for creating/editing secrets, granting access, or configuring initial TOTP seeds. Those actions belong in Inkbox Console. A secret name alone is not a secret ID; list metadata rather than inventing an ID.
