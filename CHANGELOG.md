# Changelog

## 0.2.41 (unreleased)

- Wait for Slack voice-note transcripts, including Companion turns, the same way as MMS, iMessage, and WhatsApp, and register Slack transcription completions on the Slack subscription.

## 0.2.40 (unreleased)

- Add `INKBOX_DELIVERY_MODE=local`: open no tunnel, register no webhook URL, listen on `127.0.0.1` only, and accept event bodies up to 2 MiB with unchanged signature verification.
- Handle `inbound.received` events in local delivery mode. Each event is stored durably before it is acknowledged, survives restarts, and is deduplicated by `delivery_id`, so retries run once and replays run again. A valid Inkbox signature is always required. The `agent` handler runs one turn per source session, keyed by the source id, with the content marked untrusted and the body capped at 64 KiB; the event completes only when that turn succeeds. A turn that does not finish within 30 minutes of starting is interrupted and reported as failed, and a turn that never starts is dropped before it is reported. At most 100 events or 64 MiB of payloads wait locally; beyond that the delivery is refused with 503 so it is retried later. Base64 bodies that decode to UTF-8 are shown as text; other binary bodies are noted, not inlined. The `script` handler passes the event as JSON on stdin and starts an agent turn when it prints output; up to 64 KiB of stdout (marked `(truncated)` when cut) and the last 4 KiB of stderr are kept; a run cut off by a restart counts toward its 5 runs, and stopping the plugin kills a running script's process group. Scripts run up to 5 times (retries after 10 s, 30 s, 2 min, and 10 min, 120 s timeout each). An agent turn runs at most once: a failed or interrupted turn is reported, never repeated, and a failed turn on script output does not run the script again. Script and agent failures are reported to Inkbox with the `delivery_id`; the report is retried with backoff (at most an hour apart) while the plugin runs and again on every start until it is recorded. A 2xx, a 409 (stale delivery) or any other 4xx except 408 and 429 ends the retries. Queued events, running scripts and turns, and script retry waits count as activity for hosted agents; pending failure reports do not.
- Require Inkbox SDK 0.7.41; the setup wizard installs and checks for it.

## 0.2.39 (unreleased)

- Wait briefly for voice-note transcripts, read once on timeout, and retain original attachments when transcription is unavailable.
- Register transcription completion notifications automatically and label transcribed speech separately from typed messages.

## 0.2.37

### Changed
- Require Inkbox SDK 0.7.39 for durable messaging and remote tool handling.

## 0.2.36

- Report bounded dependency import diagnostics when adapter initialization fails, without logging exception messages, paths, or credentials.

## 0.2.35

- Make managed diagnostics use a recent runtime health report instead of terminal signing-key or ordinary webhook settings. Keep standalone checks and report unavailable runtime evidence.

## 0.2.34

- Accept empty MCP continuation input requests through the pinned SDK revision.

- Keep contact and terminal-reply MCP arguments within the advertised schema while retaining durable operation identity and retry receipts.

## 0.2.33

- Allow host-managed MCP registration while retaining local voice, media, Vault, and inbound-task helpers.
- Read durable reply receipts without constructing a remote tool client.

## 0.2.32

- Retry incomplete live-test call cleanup and verify calls have ended between tests.

## 0.2.31

- Deduplicate incoming message and call-completion retries by event ID even when each delivery has a new request ID.

## 0.2.30

- Reuse valid tunnel certificates across reconnects and renew mismatched or expired state with Inkbox SDK 0.7.38 or newer.
- Bound tunnel startup and reconcile cancelled attempts before reconnecting, including late listener cleanup.

## 0.2.29

- Make subprocess-based inbox tests independent of the caller's working directory.

## 0.2.28

- Report durable managed message and chat completion timestamps so supervisors can count inactivity after work finishes. Status reads do not extend the idle window.

## 0.2.27

- Forward an optional model selection to the native chat turn without changing the default for other conversations.

## 0.2.26

- Preserve cancellation and expired interactions across pending responses, bound oversized chat output, and recover confirmed completed work without replay.
- Report known model-access failures with actionable codes.

## 0.2.25

- Add supervised direct chat with streamed text, tool activity, approvals, questions, cancellation, and downloadable file references.
- Retain control receipts across reconnects and expose bounded conversation observations without rerunning work.

## 0.2.24

- Refresh execution ownership for retried turns and replies; leave supervised post-call recovery to durable admission.
- Keep stopped uncertain work isolated from unrelated activity and configuration changes.
- Add durable, bounded webhook acceptance and retain uncertain outcomes without automatic replay.
- Preserve all message receipts through debounce batching, serialize conversation admission, and separate completed receipts from pending capacity.
- Limit hosted startup work to authorized deliveries and support explicit quarantine without replay.
- Track asynchronous replies through completion and expose supervised activity and admission fences so configuration changes wait for active work.
- Preserve standalone gateway behavior.
- Support durable inbox ownership on Windows as well as Unix.
- Mask generated webhook secrets in workflow output.
- Replace raw live-test log dumps and uploads with content-free diagnostic counts.

## 0.2.23

- Show authorized inline-reply parent authors, text excerpts and attachment metadata upfront, including retained context-only messages; temporary lookup failures and unavailable parents stay explicit without blocking the current message.
- Require published Inkbox SDK `>=0.7.29,<1.0.0` for the complete WhatsApp group and Companion interfaces.
- Preserve URLs when displaying native mentions, bound contact-name labels, and keep conflicting mention tokens explicitly unresolved.

- Support existing inbound-admitted WhatsApp groups with isolated sessions, media, reactions and explicit quoted replies; group names remain labels, with no group creation.
- Render verified native mentions in direct chats and groups while preserving source text, current-message attention and contact-memory boundaries.
- Extend existing Companion delivery, scoped authorization, Safe/Relaxed behavior and durable recovery to WhatsApp groups.
- Preserve the default contextual Automatic reply mode; native agent mentions also work when Mention required is selected.

## 0.2.22

- Restore delivery-failure follow-ups through Hermes v0.22.5 while retaining their original destinations and retry limits.
- Accept inbound messages when the API origin comes from SDK defaults or configuration, without requiring an explicit URL override.
- Make SMS, Slack, WhatsApp, and Vault tool visibility follow their available capabilities while retaining useful discovery, history, and recovery tools.
- Add `/inkbox refresh` and `inkbox_refresh_capabilities` to recheck capabilities and refresh live host tool catalogs without a gateway restart.
- Run scheduled full-stack CI once daily at noon Pacific time, following daylight saving time.
- Pin real-host CI checkouts and installation to Hermes v0.22.5.
- Support Hermes v0.22.5 event admission, routing restoration, and cancelled-delivery cleanup while preserving queued-message recovery.
- Add email listing, message/thread reads, attachment downloads, forwarding, call history/transcript reads, hangup, and read-only message-send recovery tools.
- Add email attachments and inline images, retain email/SMS/iMessage request keys for recovery, and expose explicit voicemail actions and messages.
- Preserve speaker phone-number attribution and connection history in hosted post-call context.
- Require published Inkbox SDK `>=0.7.27,<1.0.0`, replacing the temporary Git source override and source-candidate CI installs.

## 0.2.21

- Recognize case-equivalent WhatsApp conversation IDs as source replies, preserving automatic quotes and preventing duplicate final responses without changing retained request payloads.

- WhatsApp supports direct messages only: one E.164 string or an existing direct conversation. Reject recipient arrays and unsupported group events, and quarantine retained group intents without replay. Preserve quoted replies, media, reactions, threads and recovery keys.
- Add chronological quote-chain reads and whole-message named/custom reaction tools with explicit stable mutation keys and versioned receipt/removal semantics. Authorized peer reactions admit lightweight context once per revision; self echoes and inaccessible or unresolved targets never wake model work.
- Raise the current WhatsApp SDK floor to 0.7.26 and share the SDK's direct-message/reply request fixture matrix. Existing iMessage, Slack, media and native turn lifecycles remain in place.

- Add approved, dedicated one-to-one WhatsApp with signed reception, native final replies, scoped approvals, and durable duplicate/uncertain-send protection.
- Add ten WhatsApp tools for messages, conversations, owned media, local mark-read, readiness, and send lookup. Preserve media IDs and refresh incoming attachment URLs at dispatch.
- Require SDK 0.7.24 and exercise the exact public SDK candidate in current-feature and native-install checks while retaining historical compatibility lanes.

- Add `inkbox_slack_download_file_preview` to save available Slack image previews locally for vision inspection, without requiring a public image URL.

- Upload local Slack images, documents, video, and audio through source-bound native/Companion reply routes, with durable duplicate and uncertain-outcome protection. Remote image URLs remain links.
- Add `inkbox_slack_upload_file` and `inkbox_slack_get_operation`, preserving operation status and file IDs. Raise the base SDK floor to 0.7.15.
- Resume queued work after a confirmed Stop fence, including safe legacy pre-admission cancellations, while keeping uncertain deliveries quarantined.
- Supply Slack-native formatting guidance for native and Companion replies, and show native task-card progress where supported with a source-bound in-place message fallback alongside Working/Stop controls.
- Prefer typed SDK task-stream methods when available while preserving compatibility with older supported SDKs and uncertain-delivery handling.
- Preserve confirmed progress outcomes across reconnects; describe unconfirmed disconnect cleanup as paused rather than claiming the run was stopped.

## 0.2.20

- Restore tunnels with Inkbox SDK 0.7.16 and native platform certificate verifiers.
- Keep TLS compatibility checks valid for newer SDKs that handle certificate-store inspection themselves.

## 0.2.19

- Add opt-in Slack tools, guided workspace installation, signed channel reception, channel-wide Companion, exact-thread controls, and inline/thread-specific activity.
- Add opt-in native iMessage reply/thread APIs with durable serial follow-ups, source-bound text/media/tool replies, saved-answer recovery, and duplicate/uncertain-send protection.
- Add access-scoped Vault metadata, secret decryption, and TOTP tools using a local Hermes Vault key.
- Preserve pending permission scopes while fencing superseded workers; keep uncertain legacy work quarantined unless native ownership can be conclusively resolved.
- Keep native approval prompts visible while filtering operational chatter, and quarantine interrupted media/tool sends without replaying saved answers or spinning the recovery queue.
- Raise the base Inkbox SDK floor to 0.7.11; native iMessage requires 0.7.13 and Slack Companion requires 0.7.14.
- Keep tunnel TLS compatible with native platform certificate verifiers while preserving trusted roots and hostname validation.

## 0.2.18

### Fixed

- A2A progress updates continue after a turn hands work to background subagents, and stop once that work and any follow-up turn finish or the task settles. Callers no longer see a long silence on tasks that are still running.

## 0.2.17

### Added

- Companion initialization loads complete available history with durable receipt, turn, and reply checkpoints.
- Independent Safe/Relaxed sender modes and Auto/Mention group modes, configurable in setup.
- Context-only messages persist without waking or interrupting the agent; current email To addressing also satisfies Companion mention mode.

### Fixed

- SMS and iMessage group sessions are shared across participants and remain separate from private conversations.
- Approval answers and commands use current raw text and the prompted sender rather than history or unrelated participants.
- Automatic email replies preserve To, Cc, and threading using the stored reply-all parent.
- Delayed replies keep their original destinations. Mixed-case email authors match across initialization, approvals, and controls.
- Startup failures before submission can retry; completed model responses are saved before delivery, while uncertain submissions and sends remain paused.

### Changed

- Requires the published Inkbox SDK `>=0.7.6,<1.0.0`; no unreleased source pin.
- Plugin version reporting follows the installed plugin manifest.
- Scoped Companion delivery failures remain available for inspection without automatic recovery turns.
