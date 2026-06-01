---
name: inkbox-credential-use
description: Use when the user asks the agent to "log into X", "get the API key for Y", "fetch the SSH key for Z", or "give me the TOTP code for service A". Covers the credential vault path through the Inkbox plugin. Always list-then-get; never enumerate plaintext.
user-invocable: false
---

# Inkbox credential use

When an action requires plaintext credentials (a password, an API key, an SSH key, a TOTP code), use this skill to retrieve them through the Inkbox vault. The vault tools are **all optional** — the user must opt them into `tools.allow` before this skill can do anything.

## Required tools (all optional, must be allowlisted)

- `inkbox_credentials_list` — metadata only (id, name, secretType). Safe to call freely.
- `inkbox_credentials_get_login` — plaintext login (username + password + optional URL)
- `inkbox_credentials_get_api_key` — plaintext API key
- `inkbox_credentials_get_ssh_key` — plaintext SSH private key (+ public, fingerprint, passphrase)
- `inkbox_totp_code` — current TOTP code for a login that has TOTP configured

## Prerequisites

- The vault must be initialized in the Inkbox Console.
- The vault unlock key must be available in the `INKBOX_VAULT_KEY` env var (or a custom env var if `vault.keyEnvVar` is configured).
- The identity must have access grants to the secrets in question (admin-set via the Inkbox Console).

If `INKBOX_VAULT_KEY` is not set, the very first credential tool call returns "Vault is locked." Direct the user to export the env var in the shell launching Hermes, then retry.

## Workflow

1. **List first.** Call `inkbox_credentials_list` (optionally filter by `type`). Read the metadata to find the right `id` by `name`. Never guess UUIDs.

2. **Confirm with the user when stakes are high.** If the user said "log into AWS production," verify the matching secret's `name` and `description` look right before fetching plaintext. The agent should not silently pull production credentials.

3. **Fetch the typed plaintext.** Call the matching get tool:
   - Login → `inkbox_credentials_get_login`
   - API key → `inkbox_credentials_get_api_key`
   - SSH key → `inkbox_credentials_get_ssh_key`

   Each returns the payload as JSON. Use the fields directly in the action the user asked for; do not echo the plaintext back to the user unless they explicitly asked to see it.

4. **TOTP** is a separate flow. Call `inkbox_totp_code` with the login secret's UUID — returns `{ code, secondsRemaining }`. The code is valid for `secondsRemaining` seconds; if it's <5, regenerate before using.

## Hygiene

- **Don't list-then-dump.** `credentials_list` returns metadata only by design. Don't call all three `get_*` tools in a loop trying to enumerate the vault.
- **Don't paste plaintext into chat.** When the user asks "use the GitHub PAT to push," fetch it and use it — don't repeat it back to them.
- **Don't store outside this call.** The plugin caches the unlocked vault in-process; you don't need to (and shouldn't) keep plaintext in session memory.

## Errors

| Error | Meaning |
|---|---|
| `Vault is locked. Set the INKBOX_VAULT_KEY...` | Env var missing — direct the user to set it. |
| `404` on get_* | Wrong secret id, or this identity doesn't have access. |
| `TypeError` on get_login/get_api_key/get_ssh_key | Caller picked the wrong typed getter for the secret's type. Re-list to see `secretType`. |

## What this skill does NOT cover

- Creating, updating, or deleting secrets — there's no plugin tool for this in agent-scoped mode.
- Granting access to secrets — admin-only via the Inkbox Console.
- TOTP setup — initial TOTP config also happens in the Console.

## When you need more — raw Inkbox docs

If a payload shape, secret type, vault behavior, or TOTP detail isn't covered here, go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

Especially useful when checking the exact fields on `LoginPayload`, `APIKeyPayload`, `SSHKeyPayload`, `KeyPairPayload`, `OtherPayload`, or `TOTPCode` rather than guessing.
