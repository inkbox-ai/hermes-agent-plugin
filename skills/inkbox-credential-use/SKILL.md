---
name: inkbox-credential-use
description: Use when the user asks the agent to "log into X", "get the API key for Y", "fetch the SSH key for Z", or "give me the TOTP code for service A". Hermes does not expose Inkbox vault tools; explain the limitation and do not invent credential access.
user-invocable: false
---

# Inkbox credential use

OpenClaw exposes Inkbox vault tools. Hermes social-tier does not register credential vault tools, so this plugin cannot list, fetch, or generate credentials/TOTP codes through Inkbox.

## Hermes tool availability

- There is no `inkbox_credentials_list`, `inkbox_credentials_get_login`, `inkbox_credentials_get_api_key`, `inkbox_credentials_get_ssh_key`, or `inkbox_totp_code` tool in Hermes.
- Do not claim that you checked the vault or retrieved a credential.
- If the user needs vault access, direct them to Inkbox Console or a host/plugin tier that exposes the Inkbox vault tools.

## Prerequisites

- The user must provide credentials through an approved host mechanism outside this Hermes Inkbox plugin.
- Do not ask the user to paste secrets into chat unless they explicitly choose that path and the current host policy allows it.

## Workflow

1. **State the limitation.** Say that this Hermes installation does not expose Inkbox vault tools.
2. **Offer a safe path.** Ask the user to configure the needed credential through the host's supported secret mechanism or Inkbox Console.
3. **Avoid plaintext echo.** If credentials arrive through another tool, use them for the requested action and do not repeat plaintext unless the user explicitly asked to see it.

## Hygiene

- **Don't fake vault access.** No Inkbox vault tool is available in Hermes.
- **Don't paste plaintext into chat.** When another approved credential source exists, use it without repeating secrets.
- **Don't store secrets in session memory.** Treat credentials as transient.

## Errors

| Error | Meaning |
|---|---|
| Missing credential tool | This Hermes plugin tier does not expose Inkbox vault tools. |
| User asks for a secret by name | Ask them to configure it through the host's supported secret path or Inkbox Console. |

## What this skill does NOT cover

- Creating, updating, or deleting secrets — there's no plugin tool for this in agent-scoped mode.
- Granting access to secrets — admin-only via the Inkbox Console.
- TOTP setup — initial TOTP config also happens in the Console.

## When you need more — raw Inkbox docs

If a payload shape, secret type, vault behavior, or TOTP detail isn't covered here, go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of every Inkbox doc page.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

Especially useful when checking the exact fields on `LoginPayload`, `APIKeyPayload`, `SSHKeyPayload`, `KeyPairPayload`, `OtherPayload`, or `TOTPCode` rather than guessing.
