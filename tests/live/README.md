# Live test suite (`tests/live/`)

End-to-end tests that boot a **real Hermes gateway** running this plugin and have a
separate Inkbox identity message it for real, over email. They prove the agent is
reachable, replies, reasons, and uses its tools — not just that units pass. Run by
`.github/workflows/live-channels.yml` as a two-leg matrix (one suite; per-test gating
picks what runs per leg). Never gates a merge; manual + weekly.

## Actors
- **AUT** — a real Hermes gateway + this plugin + a model (the agent under test).
- **Remote** — a second Inkbox identity (different org) that emails the AUT and
  polls for replies; no model, no gateway, just the harness.

## Mock leg — reachability (deterministic, free)
Model is a local mock (`mock_openai.py`), so replies are deterministic.
- **test_email_reachability** — remote emails the AUT; the reply is delivered back
  (tracked by thread_id), carries the mock marker, and has no error text. Proves the
  whole pipe: inbound -> routing -> agent loop -> tool send -> real delivery.
- **test_sms_reachability** — same, over **SMS** (agent-to-agent, no opt-in needed).

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

`tests/live/test_sms.py` mirrors all of the above over **SMS** (basic, own
identity, sender details, tools). Two Inkbox agents text each other with no START
opt-in — servers bypasses the missing-opt-in gate for inter-agent traffic. Prompts
ask for short replies to stay clear of carrier/spam filtering; questions never name
a tool, so the agent must choose the right tool itself.

`tests/live/test_cross_channel.py` tests **cross-channel** replies, correlated by a
6-char token: an email asks the agent to *text* the token (poll SMS for it), and an
SMS asks the agent to *email* the token (poll email for it). The agent must find the
sender's other-channel address from the contact card. iMessage/voice get added here.

## Concurrency
Only one client may hold the AUT's Inkbox tunnel at a time, so every live workflow
shares the `inkbox-live-aut-tunnel` concurrency group and runs one at a time across
all triggers (PRs + the main schedule queue behind each other).

## Artifacts (kept for debugging)
The suite does **not** delete anything from Inkbox — the test emails and any
contacts stay in the test identities so you can inspect a run in the console. The
workflow uses **no** GitHub environment/deployment, so nothing lingers on GitHub
either.

## Covered today / gaps
**Covered (email + SMS):** reachability, self-knowledge, contact awareness, tool
awareness, and **cross-channel** (email->SMS, SMS->email) — with the agent choosing
its tools and target channel autonomously.
**Not yet:** iMessage, voice; outbound-initiated flows; multi-turn. Same harness.
