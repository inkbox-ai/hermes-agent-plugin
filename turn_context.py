"""Per-turn parent-route context shared between the adapter and the send tools.

The adapter stamps the current inbound turn's route into a contextvar just
before dispatching a turn; the send tools read it back so a spawned send can
record where the parent turn originated. Kept in its own tiny module so both
``adapter.py`` (the writer) and ``tools.py`` (the reader) can import it without
creating an import cycle between them.
"""

from __future__ import annotations

import contextvars
from typing import Dict, Optional

# The active turn's parent route, or None when no turn is in flight (CLI, tests,
# or any code path the adapter did not stamp). A route dict carries the keys
# ``sessionThreadId``, ``chatId``, ``threadId``, ``modality``, ``contactId``,
# ``messageId``, and ``replyTo``.
_CURRENT_TURN: contextvars.ContextVar[Optional[Dict]] = contextvars.ContextVar(
    "inkbox_current_turn", default=None
)


def set_current_turn(route: Dict) -> None:
    """Stamp the parent route for the turn about to be dispatched.

    Args:
        route (Dict): The parent turn's route descriptor.

    Returns:
        None
    """
    _CURRENT_TURN.set(route)


def get_current_turn() -> Optional[Dict]:
    """Read the current turn's parent route, if one was stamped.

    Returns:
        Optional[Dict]: The stamped route dict, or None when the contextvar is
        unset (e.g. CLI or test invocation) — callers degrade gracefully.
    """
    # LookupError guards the case where the contextvar was never set in this
    # context; treat it identically to the default of None.
    try:
        return _CURRENT_TURN.get()
    except LookupError:
        return None
