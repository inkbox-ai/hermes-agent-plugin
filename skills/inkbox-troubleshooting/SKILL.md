---
name: inkbox-troubleshooting
description: Use when an Inkbox tool or channel reports runtime/config errors such as "Inkbox plugin is not configured", "whoami failed", "Vault is locked", "recipient_not_opted_in", or phone/mailbox readiness failures. Helps recover from misconfiguration and tool errors, not first-time setup walkthroughs.
user-invocable: false
---

# Inkbox troubleshooting

Use this skill when an Inkbox tool fails, the user asks why Inkbox is not working, or a channel readiness problem blocks email, SMS, calls, contacts, notes, or vault access.

## Common errors

| Error | Fix |
|---|---|
| `Inkbox plugin is not configured` | Set `apiKey` + `identity` in config, or run this plugin's Inkbox setup command (`<host> inkbox setup`). |
| `whoami failed: 401 Unauthorized` | API key is wrong, revoked, or has a typo. Ask the user to mint or paste a fresh key. |
| `whoami: API key is not agent-scoped` | Outbound may work, but access-scoped reads can behave differently. Prefer an agent-scoped key bound to the configured identity. |
| `404` on `getIdentity` | The configured `identity` handle does not exist under this key or does not match the key's bound identity. |
| `sender_sms_pending` | The Inkbox phone number is still propagating to carriers. Retry later and verify `smsStatus`. |
| `recipient_not_opted_in` | Ask the recipient to text `START` to the agent's Inkbox number, then retry. |
| `recipient_opted_out` | The recipient texted `STOP`; they must text `START` again before SMS can be sent. |
| `Vault is locked` | Set `INKBOX_HERMES_VAULT_KEY` through the local gateway secret mechanism and restart; never paste it in chat. |

## Vault unlock pattern

Vault tools respect native tool policy. The plugin never persists the vault key. Metadata listing needs no plugin key; each decryption/TOTP operation reads `INKBOX_HERMES_VAULT_KEY` and uses fresh SDK reads. Migrate SDK-global Vault auto-unlock settings out of this gateway process: they can affect unrelated clients before the plugin handles a tool.

If vault access fails, do not ask for the vault key in chat. Tell the operator which env var needs to be set in this plugin's gateway process.

## Identity checks

Use `inkbox_whoami` when you need to confirm the active Inkbox identity, mailbox, phone number, SMS status, auth subtype, sending domain, or call routing state.

If `inkbox_whoami` fails, surface the exact error and suggest this plugin's Inkbox doctor command (`<host> inkbox doctor`) for a local diagnostic pass.

## When you need more

If a config field, error message, or setup flow here does not match what the user is seeing, go to the source:

- **https://inkbox.ai/llms.txt** — LLM-friendly index of Inkbox docs.
- **https://inkbox.ai/docs/all.md** — the full Inkbox documentation concatenated as one markdown file.

## Optional channels

Native iMessage replies require `INKBOX_IMESSAGE_THREADED_REPLIES=true` and SDK 0.7.13+. Slack requires `INKBOX_SLACK_ENABLED=true`; Slack Companion requires SDK 0.7.14+. A paused Slack subscription needs operator attention, not a duplicate subscription. Run `hermes inkbox doctor` for read-only checks. Never delete native/Companion checkpoints or resend an unknown-outcome delivery to force recovery.
