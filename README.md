# Hermes Agent Inkbox Plugin

[Inkbox](https://inkbox.ai) platform plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It gives a Hermes agent its own Inkbox identity: mailbox, phone number, SMS/MMS, voice calls, contact rules, an Inkbox tunnel, realtime phone calls, and bundled Inkbox skills without forking Hermes.

Status: gateway platform adapter, setup wizard, doctor checks, SMS/MMS batching, 1:1 and group SMS conversations, inbound email/SMS/voice, OpenAI Realtime phone calls, post-call actions, SMS conversation tools, and package-included skills are implemented.

## Prerequisites

- An installed Hermes Agent.
- The recommended Hermes installer for macOS, Linux, or WSL2:

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc
hermes setup
```

- The recommended Hermes installer for Windows PowerShell:

```powershell
iex (irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1)
hermes setup
```

After setup, configure a model provider with `hermes setup` if the installer did not already walk you through it.

- An Inkbox account or API key. `hermes inkbox setup` can create a fresh agent identity through self-signup, or it can use an existing Inkbox API key.

## Quick Start

Install and enable the plugin:

```bash
hermes plugins install inkbox-ai/hermes-agent-plugin --enable
```

Configure Inkbox:

```bash
hermes inkbox setup
hermes inkbox doctor
```

Start the gateway:

```bash
hermes gateway run
```

Keep that process running. On startup the plugin opens an Inkbox tunnel, configures mail/text webhook subscriptions and the incoming-call URL, and routes inbound email, SMS, and calls into Hermes sessions.

To update an existing install:

```bash
hermes plugins update inkbox
hermes gateway restart
```

## Setup Wizard

`hermes inkbox setup` walks the active Hermes install through Inkbox configuration:

1. Installs or upgrades `inkbox>=0.4.6` and `aiohttp>=3.9` in the Hermes Python environment when needed.
2. Authenticates to Inkbox, or starts self-signup if you do not have an API key yet.
3. Resolves or creates the Inkbox agent identity for this Hermes gateway.
4. Optionally provisions a local US phone number so SMS and voice are available.
5. Offers OpenAI Realtime for calls when a phone number exists, validates the OpenAI API key, and stores `INKBOX_REALTIME_*` settings only when validation succeeds.
6. Stores `INKBOX_API_KEY`, `INKBOX_IDENTITY`, `INKBOX_SIGNING_KEY`, and related settings in `~/.hermes/.env`.
7. Points the identity's mailbox and phone number at the agent-owned Inkbox tunnel.
8. Prints the final mailbox/phone summary and next commands.

If setup provisions a new local phone number, it waits for an inbound SMS `START` to that number before finishing. Text `START` from every phone that should receive outbound SMS from the agent.

Inkbox reachability is controlled server-side with mailbox and phone contact rules in the Inkbox Console. The plugin sets `INKBOX_ALLOW_ALL_USERS=true` so anyone Inkbox lets through reaches Hermes; use the Inkbox Console for allow/block rules instead of maintaining a second local allowlist.

## SDK Install Note

The setup wizard installs dependencies into the Python environment that runs Hermes. That may be different from your shell's `pip`.

If the wizard prints a missing-SDK warning, use the exact command it prints. It will look like this:

```bash
/path/to/hermes/venv/bin/python3 -m pip install 'inkbox>=0.4.6' 'aiohttp>=3.9'
```

When `uv` is available, the wizard prefers:

```bash
uv pip install --python /path/to/hermes/venv/bin/python3 'inkbox>=0.4.6' 'aiohttp>=3.9'
```

Do not use plain `pip install inkbox aiohttp` unless the wizard tells you to; plain `pip` may point at pyenv, Homebrew, system Python, or another virtualenv.

## Manual Config

The setup wizard writes to `~/.hermes/.env`:

```bash
INKBOX_API_KEY=ApiKey_xxxxxxxxxxxx
INKBOX_IDENTITY=my-agent-handle
INKBOX_SIGNING_KEY=xxxxxxxxxxxx
INKBOX_ALLOW_ALL_USERS=true
```

Optional:

```bash
INKBOX_BASE_URL=https://inkbox.ai
INKBOX_PUBLIC_URL=https://your-public-hermes-host.example
INKBOX_TUNNEL_NAME=my-agent-handle
INKBOX_HOME_CHANNEL=contact-or-phone
INKBOX_ALLOWED_USERS=contact-or-phone,another-contact
INKBOX_REQUIRE_SIGNATURE=true
```

Without `INKBOX_PUBLIC_URL`, the adapter uses the Inkbox SDK tunnel.

## Realtime Calls

Calls auto-detect OpenAI Realtime credentials. The plugin checks, in order:

1. `platforms.inkbox.realtime.api_key` in Hermes config.
2. `INKBOX_REALTIME_API_KEY`.
3. `OPENAI_API_KEY`.

The setup wizard offers Realtime after a phone number is available. When an OpenAI API key is available and realtime is enabled, the wizard validates Realtime access before saving the plugin-specific key. If no realtime API key is available, validation fails, or realtime is disabled, calls use Inkbox STT/TTS. Hermes/Codex OAuth is not used for GA Realtime calls.

Common realtime env vars:

```bash
export OPENAI_API_KEY="sk-..."
export INKBOX_REALTIME_MODEL="gpt-realtime-2"
export INKBOX_REALTIME_VOICE="cedar"
```

Disable realtime:

```bash
export INKBOX_REALTIME_ENABLED=false
hermes gateway restart
```

Realtime calls receive the agent's Inkbox handle, mailbox, phone number, caller contact metadata, and outbound-call purpose before greeting. The realtime model has direct access to `hermes_agent_consult`, `register_post_call_action`, `edit_post_call_action`, `delete_post_call_action`, and `hang_up_call`.

## CLI

```bash
hermes inkbox setup
hermes inkbox doctor
hermes inkbox whoami
```

In a chat session:

```text
/inkbox doctor
/inkbox whoami
```

Useful Hermes commands while iterating:

```bash
hermes plugins list
hermes plugins update inkbox
hermes gateway run
hermes gateway restart
hermes config
hermes config edit
```

## Smoke Test

After the gateway starts:

1. Run `hermes inkbox doctor`.
2. Text `START` to the agent's Inkbox phone number from every phone the agent should text.
3. Send the agent an SMS and verify it replies in the same SMS thread.
4. Add the agent to a group SMS/MMS conversation and verify it stays silent for unrelated chatter, then replies in the same conversation when addressed.
5. Send the agent an email and verify it replies from its Inkbox mailbox.
6. Call the agent phone number and ask for its handle, email, and phone.
7. Ask during a call for a post-call SMS or email follow-up, then verify it sends after hangup.

## Config Reference

| Env var | Required | Default | Description |
|---|---|---|---|
| `INKBOX_API_KEY` | yes | - | Agent-scoped Inkbox API key. Admin keys are accepted by setup so it can create or choose an identity. |
| `INKBOX_IDENTITY` | yes | - | Inkbox agent identity handle. |
| `INKBOX_SIGNING_KEY` | inbound | - | Webhook HMAC secret. Required for signed inbound email, SMS, and calls. |
| `INKBOX_REQUIRE_SIGNATURE` | no | `true` | Refuse unsigned inbound webhooks unless set to `false`. |
| `INKBOX_BASE_URL` | no | `https://inkbox.ai` | Override Inkbox API base URL. |
| `INKBOX_PUBLIC_URL` | no | - | Public Hermes gateway URL. If omitted, the plugin opens an Inkbox tunnel. |
| `INKBOX_TUNNEL_NAME` | no | identity handle | Override Inkbox tunnel name. |
| `INKBOX_HOME_CHANNEL` | no | - | Default Inkbox chat/contact id for cron or notification delivery. |
| `INKBOX_ALLOWED_USERS` | no | - | Optional comma-separated local allowlist. Usually leave empty and use Inkbox contact rules. |
| `INKBOX_ALLOW_ALL_USERS` | no | `false` | Allow all senders admitted by Inkbox contact rules. Setup writes `true`. |
| `INKBOX_REALTIME_ENABLED` | no | `auto` | Use raw phone media with OpenAI Realtime when credentials exist. Set `false` to force Inkbox STT/TTS. |
| `INKBOX_REALTIME_API_KEY` | no | - | OpenAI API key used only for realtime calls. `OPENAI_API_KEY` is also accepted. |
| `OPENAI_API_KEY` | no | - | OpenAI API key used for realtime calls when `INKBOX_REALTIME_API_KEY` is absent. |
| `INKBOX_REALTIME_MODEL` | no | `gpt-realtime-2` | Realtime voice model. |
| `INKBOX_REALTIME_VOICE` | no | `cedar` | Realtime voice name. |

## Tools

Hermes direct tools:

- `inkbox_whoami`
- `inkbox_send_email`
- `inkbox_send_sms`
- `inkbox_list_text_conversations`
- `inkbox_get_text_conversation`
- `inkbox_list_texts`
- `inkbox_get_text`
- `inkbox_mark_text_read`
- `inkbox_mark_text_conversation_read`
- `inkbox_place_call`

Realtime-only call tools:

- `hermes_agent_consult`
- `register_post_call_action`
- `edit_post_call_action`
- `delete_post_call_action`
- `hang_up_call`

## Bundled Skills

The plugin registers all `skills/*/SKILL.md` files with Hermes.

| Skill | Trigger |
|---|---|
| `inkbox-troubleshooting` | Runtime/config errors, failed tools, readiness issues |
| `inkbox-email-triage` | Checking or replying to Inkbox email |
| `inkbox-sms-responder` | Sending, replying to, or triaging SMS |
| `inkbox-outbound-calling` | Placing calls to numbers or contacts |
| `inkbox-call-review` | Reviewing calls and transcripts |
| `inkbox-contact-lookup` | Resolving, creating, or updating contacts |
| `inkbox-contact-rules` | Managing mail/phone allow and block rules |
| `inkbox-identity-access` | Granting/revoking contact or note visibility |
| `inkbox-notes-memory` | Saving, retrieving, or updating Inkbox notes |
| `inkbox-credential-use` | Fetching vault credentials or TOTP codes |
| `inkbox-outreach-sequence` | Multi-step outreach over email/SMS |

## Development Commands

```bash
python -m pytest
python -m pytest tests/test_realtime_auth.py tests/test_realtime_bridge_parity.py
```

## Architecture Notes

- Agent-scoped: runtime should use an Inkbox agent-scoped API key.
- Tunnel-first inbound: with a signing key, gateway opens an Inkbox tunnel, creates mail/text webhook subscriptions, and wires the incoming-call URL.
- Voice: Inkbox STT/TTS fallback path and realtime raw-media path both route through the same call WebSocket.
- Post-call actions: realtime calls can register, edit, delete, and dispatch work for the main Hermes agent after hangup.
- Identity-aware calls: call prompts include agent handle/mailbox/phone/tunnel and known caller contact metadata.
