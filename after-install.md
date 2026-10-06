# Inkbox Plugin Installed

Run:

```bash
hermes inkbox setup
hermes inkbox doctor
hermes gateway run
```

The setup wizard stores Inkbox values in `~/.hermes/.env`. If the Inkbox SDK
is missing, it installs it into the Hermes Python environment, not your shell's
default `pip`. When `uv` is available, the wizard uses `uv pip install
--python ...` so the Hermes venv does not need `pip` preinstalled.


### Optional channel capabilities

Run `hermes inkbox setup` to configure Slack, then restart the gateway. Slack defaults off (`INKBOX_SLACK_ENABLED=false`). Native iMessage replies also default off; set `INKBOX_IMESSAGE_THREADED_REPLIES=true` only with Inkbox SDK 0.7.13+ (Slack Companion needs 0.7.14+). `hermes inkbox doctor` checks enabled capabilities without installing connections or sending messages.

Vault metadata tools work without a decryption key. Set `INKBOX_HERMES_VAULT_KEY` through the local host secret mechanism for secret/TOTP tools; never paste the key in chat. Remove SDK-global Vault auto-unlock configuration from this gateway process when migrating, so unrelated channel clients remain independent. Existing receipt/checkpoint files must survive upgrades and feature toggles.
