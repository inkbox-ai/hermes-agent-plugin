# Changelog

## 0.2.16

### Added

- Companion mode initializes a separate group conversation with complete available history in one Hermes input, then processes live messages in order.
- Durable receipt and submission checkpoints recover work after restarts and pause uncertain host outcomes.
- Email replies retain their stored reply-all parent. The email tool accepts `reply_to_message_id` for reply-all without recipient overrides.

### Changed

- Requires Inkbox SDK `>=0.7.3,<1.0.0`.
- Plugin version reporting follows the installed plugin manifest.
- Companion replies recheck host permissions; shutdown preserves uncertain turns and waits for in-flight sends before releasing ownership.
- Companion delivery failures remain in scoped checkpoints for operator review without automatic recovery turns.
