# Changelog

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
