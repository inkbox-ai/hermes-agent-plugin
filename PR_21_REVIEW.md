# PR 21 Review: Hermes Agent Plugin CI, Canary, and Live Email/SMS

Review date: 2026-06-26
Branch reviewed: `ci/host-contract-and-canary` at `9713655`
Base: `origin/main`

## Executive Decision

Core plugin implementation changes are small and appropriate. The email/SMS live-test coverage is meaningful, not filler. I would not merge this PR until the CI hardening items below are either fixed or explicitly accepted as policy, because the latest update makes live Inkbox/OpenAI checks run on same-repo PRs with real secrets.

Recommended state: merge after fixing Findings 1 and 2. Finding 3 is strongly recommended before this becomes the standard live pipeline.

## Findings

### 1. High: live workflow runs real Inkbox/OpenAI secrets on same-repo PR code

File: `.github/workflows/live-channels.yml`
Lines: 10-14, 40-48, 72-76, 141-154

The updated workflow runs on `pull_request` into `main` and executes both matrix legs, including `mode: real`, for same-repo branches. That means PR code can run with:

- `HERMES_INKBOX_API_KEY`
- `HERMES_INKBOX_SIGNING_KEY`
- `REMOTE_INKBOX_API_KEY`
- `OPENAI_API_KEY`

This is safe only if every same-repo branch author is fully trusted and branch protection prevents arbitrary unreviewed workflow/test changes from reaching CI with secrets. It also makes PR checks depend on live Inkbox delivery, tunnel availability, carrier behavior, and paid OpenAI calls.

Recommendation: split PR and unattended coverage. Run only a controlled PR smoke lane, and keep the real-model leg on `workflow_dispatch` or after merge. If real live PR checks are required, gate them with a protected GitHub Environment/manual approval or a trusted-runner policy, and document that same-repo PR authors can access these secrets through changed test code.

### 2. High: live suite still runs after a failed canary

File: `.github/workflows/live-channels.yml`
Lines: 20-25, 38-43

`workflow_run` triggers on canary completion, not canary success. The `live` job does not check `github.event.workflow_run.conclusion == 'success'`, so a failed host-interface canary still starts the live email/SMS suite, takes the shared Inkbox tunnel lock, sends real messages, and may burn OpenAI tokens.

Recommendation: add a job guard:

```yaml
if: >
  (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) &&
  (github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success')
```

That matches the stated intent that live runs only after a freshly passing canary.

### 3. Medium: live logs/artifacts can retain message content and contact PII

File: `.github/workflows/live-channels.yml`
Lines: 157-176
File: `tests/live/spy_path/sitecustomize.py`
Lines: 32-36

The workflow prints and uploads `gateway.log`, `send_spy.jsonl`, and Hermes logs. The spy writes outbound method kwargs up to 500 chars, which can include email addresses, phone numbers, subjects, and message bodies. This is useful for debugging, but it retains live-channel content in GitHub artifacts and logs.

Recommendation: upload only on failure, add `retention-days: 3` or similar, and redact phone/email/message body fields before printing. At minimum, avoid `cat "$SPY_FILE"` in the public job log and keep it as a short-retention artifact.

### 4. Medium: notification delivery can change CI result

Files: `.github/workflows/canary.yml`, `.github/workflows/live-channels.yml`
Lines: canary 39-44, live 182-191

The Google Chat `curl` steps are part of the workflow result. If the webhook secret is missing or the network has a transient error, the notification can fail an otherwise useful test run. `curl -s` also does not fail on HTTP 4xx/5xx, so it is neither strictly reliable nor clearly non-blocking.

Recommendation: decide whether notification failure should be fatal. For CI signal, make it non-blocking with a shell guard and `|| true`; for alert integrity, use `curl --fail --retry 3` and ensure the secret exists in every trigger context.

### 5. Medium: local full-suite behavior depends on whether real Hermes is installed

File: `tests/conftest.py`
Lines: 10-27

The test split is correct for CI: unit jobs run without real Hermes, contract jobs install real Hermes. Locally, after installing `hermes-agent` into the same venv, `uv run pytest -v` fails non-contract tests because the real `BasePlatformAdapter.build_source()` expects `self.platform`, while several tests instantiate adapters via `object.__new__`.

This does not break current CI because the jobs are isolated, but it is a cleanup concern for developer ergonomics.

Recommendation: make the real-host path explicit, for example with `HERMES_CONTRACT_REAL_HOST=1` or a pytest marker, so ordinary local unit tests always use the stub unless the contract lane is intentionally requested.

## Test Necessity

The added tests are mostly necessary for the first email/SMS version:

- `tests/contract/test_host_interface.py` catches host drift that would break plugin registration, adapter lifecycle, `MessageEvent`, `SendResult`, and base adapter helpers.
- `tests/live/test_email_reply.py` proves end-to-end email reachability with a deterministic model.
- `tests/live/test_sms.py` covers SMS reachability plus real-model identity/tool behavior.
- `tests/live/test_email_intelligence.py` verifies the real model can use identity/contact/tool context, not just echo a canned response.
- `tests/live/test_cross_channel.py` is valuable for the email/SMS first version because the product promise is one contact/session across channels.
- `tests/live/mock_openai.py` is simple and justified. It avoids real model cost for reachability.
- `tests/live/spy_path/sitecustomize.py` is useful, but its output should be treated as sensitive live telemetry.

No obvious garbage tests found. There is helper duplication across live tests, but it is acceptable at this stage; extracting a shared live harness can wait until iMessage/voice live coverage is added.

## Naming and Cleanliness

Names are mostly clear:

- `PR checks - lint/unit/contract`, `contract-pr`, `Canary - plugin vs Hermes main`, and `Live - agent channels (email + SMS)` communicate intent.
- `test_email_reachability` is better than a generic reply test name.
- The live workflow comments should be updated if PR behavior remains, because "mock - per-PR, free" is no longer accurate when the real matrix leg also runs on PRs.
- `send_spy.jsonl` is understandable, but "spy" may read as invasive in public CI artifacts. `send_intents.jsonl` would be a cleaner external-facing name.

Runtime implementation changes are clean:

- `adapter.py` only widens `connect()` to accept `is_reconnect` and future kwargs.
- `config.py` only includes `mailbox` and `phone_number` in object summaries.
- No `CLAUDE.md` or Claude instruction files were found in `hermes-agent-plugin` or the harness worktree.

## Verification Run

Commands run locally:

- `uv run ruff check .` - passed.
- `uv sync --extra test --python 3.12 && uv run pytest -v` - 99 passed, 13 skipped.
- `uv pip install "hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git@main" && uv run pytest tests/contract -v` - 12 passed against Hermes `main` commit `7d568293f97a8640467303e786583efc19cecd20`.
- `uv run --with pyyaml python ...` YAML parse check - all workflow YAML files parsed.

I did not run the live Inkbox/OpenAI workflow locally because it requires repository secrets and live external delivery.
