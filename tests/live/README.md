# Live test suite (`tests/live/`)

End-to-end tests that boot a **real Hermes gateway** running this plugin and have a
separate Inkbox identity message it for real, over email. They prove the agent is
reachable, replies, reasons, and uses its tools — not just that units pass. Run by
`.github/workflows/live-email.yml` as a two-leg matrix (one suite; per-test gating
picks what runs per leg). Never gates a merge; manual + weekly.

## Actors
- **AUT** — a real Hermes gateway + this plugin + a model (the agent under test).
- **Remote** — a second Inkbox identity (different org) that emails the AUT and
  polls for replies; no model, no gateway, just the harness.

## Mock leg — reachability (deterministic, free)
Model is a local mock (`mock_openai.py`), so replies are deterministic.
- **test_agent_emails_back** — remote emails the AUT; the reply is delivered back
  (tracked by thread_id), carries the mock marker, and has no error text. Proves the
  whole pipe: inbound -> routing -> agent loop -> tool send -> real delivery.

## Real leg — intelligence (real gpt-5.5 via OpenAI direct)
Model is real, so these prove the agent reasons + uses tools. Every expected value
is looked up live via the API keys — nothing hardcoded.
- **test_basic_reply** — answers a simple question with a real, non-error reply.
- **test_reports_own_identity** — reports its own handle/email/phone, which it must
  fetch via `inkbox_whoami`; verified against the AUT's real identity.
- **test_reports_sender_details** — asked "who am I?", reports the sender's
  name/phone/email from the contact card it sees; verified against the sender's
  real contact in the AUT's org.
- **test_aware_of_inkbox_tools** — lists its Inkbox tools; we assert it names the
  real tools from `plugin.yaml` — a non-LLM proof the tools are truly registered.

## Concurrency
Only one client may hold the AUT's Inkbox tunnel at a time, so every live workflow
shares the `inkbox-live-aut-tunnel` concurrency group and runs one at a time across
all triggers (PRs + the main schedule queue behind each other).

## Cleanup
`conftest.py` tears down after the suite (best-effort): deletes the `smoke-*` email
threads in both mailboxes and any contact the suite seeded. To **keep** the
artifacts (e.g. to inspect a run in the Inkbox console), dispatch the workflow with
`keep_artifacts: true` (or set `LIVE_KEEP_ARTIFACTS=1`).

## Covered today / gaps
**Covered (email):** reachability, the agent's self-knowledge, contact awareness,
tool awareness.
**Not yet:** SMS, iMessage, voice; cross-channel ("email me back via SMS");
outbound-initiated flows; multi-turn. Same harness — next scenarios to add.
