<h1>Hermes Agent Inkbox Plugin</h1>

<img src="assets/hermes_with_iphone.png" alt="Hermes, now with a phone" width="200" align="left">

<p>
  <br><br>
  <b>Give your Hermes agent its own Inkbox identity:</b><br>
  a mailbox, iMessage, a phone number for calls and SMS, realtime phone calls, and an Inkbox tunnel.<br>
  Keep Hermes reachable from anywhere without forking Hermes.
</p>

<p>
  <code>Email</code> · <code>Calls</code> · <code>SMS / MMS</code> · <code>iMessage</code> · <code>Tunnel</code>
</p>

<br clear="left">

---

Status: gateway platform adapter, setup wizard, doctor checks, SMS/MMS batching, 1:1 and group SMS conversations, inbound email/SMS/iMessage/voice, OpenAI Realtime phone calls, post-call actions, SMS and iMessage conversation tools, and package-included skills are implemented.

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

Keep that process running. On startup the plugin opens an Inkbox tunnel, configures mail/text/iMessage webhook subscriptions and the incoming-call URL, and routes inbound email, SMS, iMessage, and calls into Hermes sessions.

To update an existing install:

```bash
hermes plugins update inkbox
hermes gateway restart
```

## Setup Wizard

`hermes inkbox setup` walks the active Hermes install through Inkbox configuration:

1. Installs or upgrades `inkbox>=0.5.0,<1.0.0` and `aiohttp>=3.9` in the Hermes Python environment when needed.
2. Authenticates to Inkbox, or starts self-signup if you do not have an API key yet.
3. Resolves or creates the Inkbox agent identity for this Hermes gateway.
4. Optionally provisions a local US phone number so SMS and voice are available.
5. Offers OpenAI Realtime for calls when a phone number exists, validates the OpenAI API key, and stores `INKBOX_REALTIME_*` settings only when validation succeeds.
6. Offers to enable iMessage for the agent (existing or freshly created), then walks you through connecting your iPhone: text the connect command to the Inkbox iMessage router, message the agent once, and receive a welcome reply confirming the channel.
7. Stores `INKBOX_API_KEY`, `INKBOX_IDENTITY`, `INKBOX_SIGNING_KEY`, and related settings in `~/.hermes/.env`.
8. Points the identity's mailbox, phone number, and iMessage events at the agent-owned Inkbox tunnel.
9. Prints the final mailbox/phone summary and next commands.

If setup provisions a new local phone number, it waits for an inbound SMS `START` to that number before finishing. Text `START` from every phone that should receive outbound SMS from the agent.

Inkbox reachability is controlled server-side with mailbox and phone contact rules in the Inkbox Console. The plugin sets `INKBOX_ALLOW_ALL_USERS=true` so anyone Inkbox lets through reaches Hermes; use the Inkbox Console for allow/block rules instead of maintaining a second local allowlist.

## SDK Install Note

The setup wizard installs dependencies into the Python environment that runs Hermes. That may be different from your shell's `pip`.

If the wizard prints a missing-SDK warning, use the exact command it prints. It will look like this:

```bash
/path/to/hermes/venv/bin/python3 -m pip install 'inkbox>=0.5.0,<1.0.0' 'aiohttp>=3.9'
```

When `uv` is available, the wizard prefers:

```bash
uv pip install --python /path/to/hermes/venv/bin/python3 'inkbox>=0.5.0,<1.0.0' 'aiohttp>=3.9'
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
INKBOX_BASE_URL=https://your-inkbox-api.example
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
3. Hermes `openai-api` credentials, including `credential_pool.openai-api`.
4. `OPENAI_API_KEY`.

The setup wizard offers Realtime after a phone number is available. When an OpenAI API key is available and realtime is enabled, the wizard validates Realtime access before saving the plugin-specific key. If no realtime API key is available, validation fails, or realtime is disabled, calls use Inkbox STT/TTS. Hermes/Codex OAuth is not used for GA Realtime calls.

Common realtime env vars:

```bash
export OPENAI_API_KEY="sk-..."
export INKBOX_REALTIME_MODEL="gpt-realtime-2"
export INKBOX_REALTIME_VOICE="cedar"
export INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS=true
```

Disable realtime:

```bash
export INKBOX_REALTIME_ENABLED=false
hermes gateway restart
```

Realtime calls receive the agent's Inkbox handle, mailbox, phone number, caller contact metadata, and outbound-call purpose before greeting. The realtime model has direct access to `consult_agent`, `register_post_call_action`, `edit_post_call_action`, `delete_post_call_action`, and `hang_up_call`.

When Realtime is enabled, the plugin preflights the OpenAI Realtime websocket before accepting the Inkbox call in raw-media mode. If that preflight fails, calls fall back to Inkbox STT/TTS by default. Set `INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS=false` to fail the call instead.

### Two calling lines

Calls — inbound and outbound — can run over either of two lines, and the agent picks the one that matches the channel it's talking on:

- **The dedicated phone number.** The agent's own number (the same line SMS uses). Outbound calls present this number; inbound calls to it ring the agent.
- **The shared Inkbox iMessage line.** The agent can also place and receive voice calls with a person it's connected to over iMessage, over the same shared line that person already messages. The underlying number is never surfaced — Inkbox resolves it from the iMessage connection — and it only works for people already connected over iMessage (an unknown caller is rejected; an outbound call with no connection is refused).

Inbound answering is configured once per identity (`auto_accept` → open the call bridge WebSocket), so a single setting governs both lines. Outbound, the agent sets `origination` on `inkbox_place_call` (`dedicated_number` / `shared_imessage_number`), or omits it when only one line is available.

## iMessage

iMessage works differently from SMS: the agent does not get its own iMessage number. People connect to the agent through the Inkbox iMessage router, and each connected person gets a dedicated thread with the agent.

1. Enable iMessage for the agent during `hermes inkbox setup` (or later by rerunning it). Enablement is stored on the Inkbox identity, not in local config.
2. From an iPhone, text the connect command (for example `connect @my-agent-handle`) to the Inkbox iMessage router number. The wizard prints both, and the agent can also share them via the `inkbox_imessage_triage_number` tool.
3. Inkbox texts back from the number assigned to that conversation. Send any first message there — the agent can only reply after you message it first (recipient-first; there is no cold outreach over iMessage).
4. The setup wizard waits for that first message and replies with a welcome confirming the channel. From then on, the gateway routes the thread into the same contact-keyed Hermes session as email/SMS/voice, and the agent replies over iMessage by default to whoever last reached it there.

If a person disconnects the agent, outbound sends to that conversation fail until they reconnect through the router and message the agent again. Conversation rows expose `assignment_status` (`active`/`released`) so the agent can see this, and `inkbox_list_imessage_assignments` lists who is currently connected. Outbound delivery transitions (`imessage.sent`, `imessage.delivered`) arrive as webhooks and are logged by the gateway without waking the agent; `imessage.delivery_failed` wakes the agent to fix and resend, matching the SMS lifecycle handling — where `text.delivery_unconfirmed` (carrier uncertainty, not a failure) is likewise logged without a wake.

Native attachments work in both outbound paths. In a normal channel reply, Hermes `MEDIA:/absolute/path` directives are securely validated, uploaded with the Inkbox SDK, and sent as iMessage media. For explicit `inkbox_send_imessage` calls, use `mediaPaths` for local files; use `mediaUrls` only for already-hosted public HTTP(S) URLs. iMessage supports one attachment of up to 10 MiB per message.

Once someone is connected over iMessage, the agent can also place and receive **voice calls** with them over that same shared line — see [Two calling lines](#two-calling-lines). This works even for an agent that has no dedicated phone number.

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
6. If iMessage is enabled, connect via the iMessage router, message the agent, and verify it replies in the same iMessage thread.
7. Call the agent phone number and ask for its handle, email, and phone.
8. Ask during a call for a post-call SMS or email follow-up, then verify it sends after hangup.

## Config Reference

| Env var | Required | Default | Description |
|---|---|---|---|
| `INKBOX_API_KEY` | yes | - | Agent-scoped Inkbox API key. Admin keys are accepted by setup so it can create or choose an identity. |
| `INKBOX_IDENTITY` | yes | - | Inkbox agent identity handle. |
| `INKBOX_SIGNING_KEY` | inbound | - | Webhook HMAC secret. Required for signed inbound email, SMS, iMessage, and calls. |
| `INKBOX_REQUIRE_SIGNATURE` | no | `true` | Refuse unsigned inbound Inkbox webhooks unless set to `false`. |
| `INKBOX_EXTERNAL_EVENTS_ENABLED` | no | `false` | Gates whether **unverified/unknown** webhooks reach the agent: a source with no registered provider, or an Inkbox-signed payload with no matching handler. Off by default. **Verified registered third-party providers** (e.g. a configured GitHub secret via `INKBOX_WEBHOOK_SECRET_GITHUB`) are always delivered regardless of this flag; unverified sources are handed to the agent with a directive forbidding irreversible action. |
| `INKBOX_BASE_URL` | no | SDK default | Override Inkbox API base URL. |
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
| `INKBOX_REALTIME_CONNECT_TIMEOUT_S` | no | `8` | Seconds to wait for OpenAI Realtime preflight before falling back or failing. |
| `INKBOX_REALTIME_CONSULT_TIMEOUT_S` | no | plugin default | Seconds the Realtime voice agent waits for a Hermes consult before continuing. |
| `INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS` | no | `true` | Fall back to Inkbox STT/TTS if OpenAI Realtime connect/auth fails before call accept. |

## Channel Overrides

Two optional blocks under the `inkbox:` platform config tailor the agent per
channel without editing `SOUL.md` or the bundled skills. Both are keyed by
**modality** (`email`, `sms`, `imessage`, `voice`) or by a specific **Inkbox
contact id**, with the contact id taking precedence.

- `channel_prompts` — an ephemeral system prompt injected on that channel's turns
  (e.g. an overview the agent should lead with, or a tone instruction).
- `channel_skill_bindings` — extra skills auto-loaded on a new session for that
  channel. These are **merged on top of** the built-in per-channel defaults, so
  the responder/troubleshooting skills are never dropped.

```yaml
inkbox:
  channel_prompts:
    imessage: "You are the Inkbox concierge. Give a one-line overview of Inkbox
      (email, phone, and identities for AI agents) and offer a quick live demo."
    voice: "Keep replies to one short spoken sentence."
  channel_skill_bindings:
    - id: imessage
      skills: ["inkbox:inkbox-outreach-sequence"]
    - id: voice
      skill: "inkbox:inkbox-outbound-calling"   # single-name shorthand
```

Built-in defaults that always load (before merge): `inkbox:inkbox-troubleshooting`
on every channel, plus `inkbox:inkbox-imessage-responder` on iMessage and
`inkbox:inkbox-call-review` on realtime call wrap-up. Skill names use the
qualified `inkbox:<skill>` form.

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
- `inkbox_imessage_triage_number`
- `inkbox_send_imessage`
- `inkbox_list_imessage_assignments`
- `inkbox_list_imessage_conversations`
- `inkbox_get_imessage_conversation`
- `inkbox_send_imessage_reaction`
- `inkbox_mark_imessage_conversation_read`
- `inkbox_place_call`

Realtime-only call tools:

- `consult_agent`
- `register_post_call_action`
- `edit_post_call_action`
- `delete_post_call_action`
- `hang_up_call`

## Bundled Skills

The plugin registers all `skills/*/SKILL.md` files with Hermes.

| Skill | Trigger |
|---|---|
| `inkbox-troubleshooting` | Runtime/config errors, failed tools, readiness issues |
| `inkbox-email-triage` | Current inbound email and explicit outbound/reply sends |
| `inkbox-sms-responder` | Sending, replying to, or triaging SMS |
| `inkbox-imessage-responder` | Sending, replying to, or triaging iMessage |
| `inkbox-outbound-calling` | Placing calls to numbers or contacts |
| `inkbox-call-review` | Current-call/post-call context; historical call reads are not exposed in Hermes |
| `inkbox-contact-lookup` | Using resolved inbound contact context; contact CRUD tools are not exposed in Hermes |
| `inkbox-contact-rules` | Explaining server-side contact rules; rule edit tools are not exposed in Hermes |
| `inkbox-identity-access` | Explaining identity access; grant/revoke tools are not exposed in Hermes |
| `inkbox-notes-memory` | Explaining note limitations; Inkbox note tools are not exposed in Hermes |
| `inkbox-credential-use` | Explaining vault limitations; Inkbox vault tools are not exposed in Hermes |
| `inkbox-outreach-sequence` | Multi-step outreach over email/SMS |

## Development Commands

```bash
python -m pytest
python -m pytest tests/test_realtime_auth.py tests/test_realtime_bridge_parity.py
```

## Architecture Notes

- Agent-scoped: runtime should use an Inkbox agent-scoped API key.
- Tunnel-first inbound: with a signing key, gateway opens an Inkbox tunnel, creates mail/text webhook subscriptions (plus an identity-owned iMessage subscription when enabled), and wires the incoming-call URL.
- Voice: Inkbox STT/TTS fallback path and realtime raw-media path both route through the same call WebSocket.
- Post-call actions: realtime calls can register, edit, delete, and dispatch work for the main Hermes agent after hangup.
- Identity-aware calls: call prompts include agent handle/mailbox/phone/tunnel and known caller contact metadata.

## Recommended Configuration

The plugin runs out of the box, but a few Hermes overrides noticeably improve the
experience for an Inkbox agent. Apply them in `~/.hermes/.env` (or via
`hermes config set`) and `hermes gateway restart`.

**Decide how outbound content is redacted.** Hermes ships a redactor that masks
secrets — API keys, tokens — *and* E.164 phone numbers in the agent's outbound
content by default (`HERMES_REDACT_SECRETS=true`), rewriting `+19255550123` as
`+192****0123`. For a communications agent whose own number is meant to be
shared, that masking can get in the way; remove the Hermes layer with:

```bash
HERMES_REDACT_SECRETS=false
```

Equivalently, `hermes config set security.redact_secrets false`. Note this only
disables *Hermes'* masking — the model may still abbreviate or mask a number on
its own when composing a formal reply, so don't rely on this alone to guarantee
full digits. Leave redaction on if the agent handles third-party secrets you do
not want echoed into messages or logs.

**Use OpenAI Realtime for voice.** Inkbox STT/TTS is the zero-config fallback,
but realtime calls are noticeably more natural. Provide a key and let the
plugin auto-enable it:

```bash
OPENAI_API_KEY=sk-...
INKBOX_REALTIME_ENABLED=true
```

See [Realtime Calls](#realtime-calls) for the full credential resolution order
and voice/model overrides.

**Admit everyone Inkbox already vetted.** The setup wizard writes this, but if
you configured by hand, let Inkbox's contact rules be the gate rather than a
local allowlist:

```bash
INKBOX_ALLOW_ALL_USERS=true
```
