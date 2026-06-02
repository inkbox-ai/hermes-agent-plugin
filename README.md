# Hermes Agent Inkbox Plugin

Private Inkbox plugin for Hermes Agent. This packages the Inkbox fork behavior as an additive Hermes plugin instead of requiring a fork of `hermes-agent`.

It adds:

- Inkbox gateway platform adapter for email, SMS/MMS, and live voice calls.
- Inkbox tunnel/webhook registration on gateway startup.
- OpenAI Realtime voice bridge with `hermes_agent_consult` and post-call actions.
- CLI and slash command diagnostics: `hermes inkbox ...` and `/inkbox`.
- Minimal Inkbox tools: `inkbox_whoami`, `inkbox_send_email`, `inkbox_send_sms`, `inkbox_place_call`.
- Bundled Inkbox skills adapted from the OpenClaw plugin.

## Install Locally

```bash
hermes plugins install inkbox-ai/hermes-agent-plugin --enable
hermes inkbox setup
hermes inkbox doctor
hermes gateway run
```

`hermes inkbox setup` installs or upgrades `inkbox>=0.4.6` and `aiohttp>=3.9`
inside the Hermes Python environment when needed. It prefers `uv pip install
--python ...` when `uv` is available, so the Hermes venv does not need to have
`pip` preinstalled. Do not use a plain `pip` command unless the wizard prints
it; plain `pip` may point at pyenv, Homebrew, or another Python instead of
Hermes.

For local development before pushing, clone or symlink this directory to:

```text
~/.hermes/plugins/inkbox
```

Then enable it:

```bash
hermes plugins enable inkbox
```

## Required Configuration

The setup wizard writes these into `~/.hermes/.env`:

```bash
INKBOX_API_KEY=ApiKey_...
INKBOX_IDENTITY=your-agent-handle
INKBOX_SIGNING_KEY=whsec_...
INKBOX_ALLOW_ALL_USERS=true
```

Optional:

```bash
INKBOX_PUBLIC_URL=https://your-public-hermes-host.example
INKBOX_TUNNEL_NAME=your-agent-handle
INKBOX_HOME_CHANNEL=contact-or-phone
OPENAI_API_KEY=sk-...
INKBOX_REALTIME_ENABLED=auto
INKBOX_REALTIME_MODEL=gpt-realtime-2
INKBOX_REALTIME_VOICE=cedar
```

Realtime calls use `realtime.api_key`, `INKBOX_REALTIME_API_KEY`, or
`OPENAI_API_KEY` directly when present. If no API key is configured, the plugin
falls back to Hermes' Codex OAuth token and exchanges it for an ephemeral
OpenAI Realtime client secret.

Without `INKBOX_PUBLIC_URL`, the adapter uses the Inkbox SDK tunnel.

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

## Notes

This plugin intentionally avoids editing Hermes core files. It uses Hermes plugin conventions:

- `plugin.yaml` with `kind: platform`
- `register(ctx)` in `__init__.py`
- `ctx.register_platform(...)`
- `ctx.register_tool(...)`
- `ctx.register_cli_command(...)`
- `ctx.register_command(...)`
- `ctx.register_skill(...)`

On Hermes forks that already include a built-in Inkbox adapter, plugin platform registration is last-writer-wins in Hermes' platform registry. This repo is intended to replace the need for that fork on upstream Hermes.
