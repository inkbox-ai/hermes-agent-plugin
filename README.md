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

Status: gateway platform adapter, setup wizard, doctor checks, SMS/MMS batching, 1:1 and group SMS/iMessage conversations, inbound email/SMS/iMessage/voice, OpenAI Realtime phone calls, post-call actions, conversation tools, and package-included skills are implemented.

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

Keep that process running. On startup the plugin opens an Inkbox tunnel,
configures mail/text/iMessage webhook subscriptions and incoming-call handling,
and routes inbound email, SMS, iMessage, and calls into Hermes sessions.

To update an existing install:

```bash
hermes plugins update inkbox
hermes gateway restart
```

## Setup Wizard

`hermes inkbox setup` walks the active Hermes install through Inkbox configuration:

1. Installs or upgrades `inkbox>=0.5.8,<1.0.0` and `aiohttp>=3.9` in the Hermes Python environment when needed.
2. Authenticates to Inkbox, or starts self-signup if you do not have an API key yet.
3. Resolves or creates the Inkbox agent identity for this Hermes gateway.
4. Optionally provisions a local US phone number so SMS and voice are available.
5. Presents the **Phone call voice stack** choices: Inkbox Voice AI, OpenAI Realtime API, or Inkbox TTS/STT. Realtime is saved only after key validation succeeds.
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
/path/to/hermes/venv/bin/python3 -m pip install 'inkbox>=0.5.8,<1.0.0' 'aiohttp>=3.9'
```

When `uv` is available, the wizard prefers:

```bash
uv pip install --python /path/to/hermes/venv/bin/python3 'inkbox>=0.5.8,<1.0.0' 'aiohttp>=3.9'
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
INKBOX_CONTACT_MEMORIES_ENABLED=true
```

Without `INKBOX_PUBLIC_URL`, the adapter uses the Inkbox SDK tunnel.

Verified inbound events include generated memories for the matched sender or caller by default. They are added as background context and are never treated as instructions. Disable them with `INKBOX_CONTACT_MEMORIES_ENABLED=false`, or override the environment setting in Hermes config:

```yaml
platforms:
  inkbox:
    contact_memories_enabled: false
```

## Phone Call Voice Stack

The setup wizard offers three ways to handle phone calls:

1. **Inkbox Voice AI** handles calls on behalf of the agent and notifies Hermes
   when each call ends. Setup also asks whether its tools should be
   contact-scoped or use YOLO mode.
2. **OpenAI Realtime API** uses your API key for low-latency conversations. The
   realtime agent can consult Hermes for complex tasks.
3. **Inkbox TTS/STT** routes transcripts and spoken replies through Hermes with
   higher conversational latency and no OpenAI API key.

`INKBOX_VOICE_STACK` is the canonical selection. Existing installations without
that setting retain their current Realtime auto-detection behavior.

In Voice AI mode, `inkbox_place_call` turns `purpose`, `opening_message`, and
`context` into Inkbox Voice AI's task brief. The runtime omits a per-call
authority override, so the call inherits the authority approved during setup.
When any Voice AI call ends—including unanswered and failed calls—Inkbox sends
one signed `call.ended` event. Hermes receives the outcome, transcript, and open
post-call actions in a single suppressed-text turn and can complete remaining
work with its normal tools.

### OpenAI Realtime credentials

Calls auto-detect OpenAI Realtime credentials. The plugin checks, in order:

1. `platforms.inkbox.realtime.api_key` in Hermes config.
2. `INKBOX_REALTIME_API_KEY`.
3. Hermes `openai-api` credentials, including `credential_pool.openai-api`.
4. `OPENAI_API_KEY`.

When OpenAI Realtime is selected, the wizard validates Realtime access before
saving the plugin-specific key. Failed validation returns to the three voice
stack choices without changing the active selection. Hermes/Codex OAuth is not
used for GA Realtime calls.

Common realtime env vars:

```bash
export OPENAI_API_KEY="sk-..."
export INKBOX_REALTIME_MODEL="gpt-realtime-2"
export INKBOX_REALTIME_VOICE="cedar"
export INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS=true
```

Switch from Realtime to Inkbox TTS/STT:

```bash
export INKBOX_VOICE_STACK=inkbox_tts_stt
hermes gateway restart
```

`INKBOX_REALTIME_ENABLED` is a legacy compatibility toggle used only when
`INKBOX_VOICE_STACK` is absent.

Realtime calls receive the agent's Inkbox handle, mailbox, phone number, caller contact metadata, and outbound-call purpose before greeting. The realtime model has direct access to `consult_agent`, `register_post_call_action`, `edit_post_call_action`, `delete_post_call_action`, and `hang_up_call`.

When Realtime is enabled, the plugin preflights the OpenAI Realtime websocket before accepting the Inkbox call in raw-media mode. If that preflight fails, calls fall back to Inkbox STT/TTS by default. Set `INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS=false` to fail the call instead.

### Two calling lines

Calls — inbound and outbound — can run over either of two lines, and the agent picks the one that matches the channel it's talking on:

- **The dedicated phone number.** The agent's own number (the same line SMS uses). Outbound calls present this number; inbound calls to it ring the agent.
- **The shared Inkbox iMessage line.** The agent can also place and receive voice calls with a person it's connected to over iMessage, over the same shared line that person already messages. The underlying number is never surfaced — Inkbox resolves it from the iMessage connection — and it only works for people already connected over iMessage (an unknown caller is rejected; an outbound call with no connection is refused).

Inbound answering is configured once per identity, so one voice-stack selection
governs both lines. OpenAI Realtime and Inkbox TTS/STT open the call bridge
WebSocket; Inkbox Voice AI handles call media without the local bridge. Outbound,
the agent sets `origination` on `inkbox_place_call` (`dedicated_number` /
`shared_imessage_number`), or omits it when only one line is available.

## iMessage

iMessage supports shared, dedicated inbound, and dedicated outbound lines. Shared and dedicated inbound lines are recipient-first. A dedicated outbound line can initiate 1:1 conversations and groups of 2–8 distinct recipients.

1. Enable iMessage for the agent during `hermes inkbox setup` (or later by rerunning it). Enablement is stored on the Inkbox identity, not in local config.
2. From an iPhone, text the connect command (for example `connect @my-agent-handle`) to the Inkbox iMessage router number. The wizard prints both, and the agent can also share them via the `inkbox_imessage_triage_number` tool.
3. On shared service, Inkbox texts back from the number assigned to that conversation. Send any first message there. Dedicated inbound lines likewise require the recipient to message first; dedicated outbound lines may initiate conversations.
4. The setup wizard waits for that first message and replies with a welcome confirming the channel. From then on, a 1:1 thread joins the same contact-keyed Hermes session as email/SMS/voice, while each group gets its own shared conversation session. The agent replies over iMessage by default to the thread that woke it.

If a person disconnects the agent, outbound sends to that conversation fail until they reconnect through the router and message the agent again. Conversation rows expose `assignment_status` (`active`/`released`) so the agent can see this, and `inkbox_list_imessage_assignments` lists who is currently connected. Outbound delivery transitions (`imessage.sent`, `imessage.delivered`) arrive as webhooks and are logged by the gateway without waking the agent; `imessage.delivery_failed` wakes the agent to fix and resend, matching the SMS lifecycle handling — where `text.delivery_unconfirmed` (carrier uncertainty, not a failure) is likewise logged without a wake.

Native attachments work in both outbound paths. In a normal channel reply, Hermes `MEDIA:/absolute/path` directives are securely validated, uploaded with the Inkbox SDK, and sent as iMessage media. For explicit `inkbox_send_imessage` calls, use `mediaPaths` for local files; use `mediaUrls` only for already-hosted public HTTP(S) URLs. iMessage supports one attachment of up to 10 MiB per message.

Group iMessage uses the same conversation-first behavior as group SMS. `inkbox_list_imessage_conversations` includes groups by default, `inkbox_get_imessage_conversation` returns their history, and `inkbox_send_imessage` replies with `conversationId`. To start a group, pass 2–8 distinct E.164 recipients in `to`; the plugin verifies that the identity has a dedicated outbound iMessage line first. Inbound group messages share a conversation session, include sender and participant context, and only trigger a visible reply when the agent is addressed or expected to act. Typing indicators and read receipts remain 1:1-only.

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

## Docker Test Environment

The repository includes a manual-testing image with Hermes and the Inkbox SDK
preinstalled. The plugin source is staged from the exact local Docker build
context, while the public Inkbox SDK dependency is pinned in the Dockerfile.
The plugin is not installed or configured in the image.

Build and start it:

```bash
docker build --tag hermes-inkbox-dev .
docker volume create hermes-inkbox-dev-data
docker run --detach \
  --name hermes-inkbox-dev \
  --volume hermes-inkbox-dev-data:/opt/data \
  hermes-inkbox-dev
docker exec --interactive --tty --user hermes hermes-inkbox-dev bash
```

Then, inside the container:

```bash
hermes setup
hermes plugins install "file://${INKBOX_PLUGIN_SOURCE}" --enable
hermes inkbox setup
hermes inkbox doctor
hermes gateway run
```

On a reused Docker volume, skip `hermes setup` when a model provider is already
configured.

Hermes state and credentials persist in the `hermes-inkbox-dev-data` Docker
volume when the container is restarted or recreated.

## Smoke Test

After the gateway starts:

1. Run `hermes inkbox doctor`.
2. Text `START` to the agent's Inkbox phone number from every phone the agent should text.
3. Send the agent an SMS and verify it replies in the same SMS thread.
4. Add the agent to a group SMS/MMS conversation and verify it stays silent for unrelated chatter, then replies in the same conversation when addressed.
5. Send the agent an email and verify it replies from its Inkbox mailbox.
6. If iMessage is enabled, connect via the iMessage router, message the agent, and verify it replies in the same iMessage thread. For a dedicated outbound line, also start a group and verify addressed replies stay in that conversation while unrelated chatter receives no response.
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
| `INKBOX_CONTACT_MEMORIES_ENABLED` | no | `true` | Include generated memories for the matched sender or caller as background context. `platforms.inkbox.contact_memories_enabled` takes precedence. |
| `INKBOX_VOICE_STACK` | no | legacy migration | Phone call stack: `inkbox_voice_ai`, `openai_realtime`, or `inkbox_tts_stt`. |
| `INKBOX_VOICE_AI_AUTHORITY_MODE` | Voice AI | `contact_scoped` | Voice AI tool authority: `contact_scoped` or `yolo`. |
| `INKBOX_VOICEMAIL_DETECTION` | no | `enabled` | Outbound call voicemail detection: `enabled` or `disabled`. Live CI sets `disabled`; ordinary calls keep `enabled`. |
| `INKBOX_REALTIME_ENABLED` | no | `auto` | Legacy Realtime toggle for installations without `INKBOX_VOICE_STACK`. |
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
- `inkbox_a2a_call`
- `inkbox_a2a_check`
- `inkbox_a2a_reply`
- `inkbox_a2a_complete`
- `inkbox_a2a_ask_caller`
- `inkbox_a2a_fail`
- `inkbox_list_a2a_tasks`
- `inkbox_list_a2a_messages`
- `inkbox_list_a2a_sent_tasks`
- `inkbox_get_a2a_sent_task`
- `inkbox_lookup_contact`
- `inkbox_list_contacts`
- `inkbox_get_contact`
- `inkbox_create_contact`
- `inkbox_update_contact`
- `inkbox_delete_contact`

Inbound A2A tasks use isolated context sessions and a durable task registry.
The three A2A outcome tools are accepted only during a verified inbound A2A
turn. Outbound delegation tools can create tasks, wait for worker state changes,
and answer requests for more input. The history tools support direction,
participant, lifecycle, context, keyword, timestamp, and cursor filters. The
sent-task tools remain available as outbound-only compatibility aliases. The
plugin requires Inkbox SDK 0.5.8 or newer.

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
| `inkbox-contact-lookup` | Resolving, creating, or updating organization-wide contacts |
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
- Tunnel-first inbound: with a signing key, the gateway opens an Inkbox tunnel,
  creates mail/text subscriptions, and keeps an identity-owned `call.ended`
  subscription; iMessage events share that identity subscription when enabled.
- Voice: Inkbox TTS/STT and OpenAI Realtime use the local media WebSocket.
  Inkbox Voice AI handles media remotely and reports completion by webhook.
- Post-call actions: Realtime and Voice AI calls dispatch one reconciled
  post-call turn for the main Hermes agent after hangup.
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
