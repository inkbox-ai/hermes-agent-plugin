# tests/live/conftest.py
"""Shared guardrails for the live suite.

The live tests drive real SMS through the shared Inkbox 10DLC pool, whose
conversation-health rules (see servers ``conversation_health.py``) block a
sender once its window fills: the same body twice with no reply
(``duplicate_body``), 10 unanswered outbound (``unanswered_limit``), or 5
carrier spam-fails in a row (``carrier_spam_backoff``). Each rule keys off
the window *"since the recipient's last inbound reply"* and empties the
moment an inbound lands in that direction.

``duplicate_body`` is already handled everywhere by per-send body
diversification (each driver send carries a unique ref), and the tests
never provoke real carrier fails, so the only window that creeps up is
``unanswered_limit`` — and only in tests where the agent answers
out-of-band (voice "call me" → the agent *calls* back, never texts), so
nothing resets the opener's window.

Rather than poke an SMS before every test (which spams the real driver
phone), this autouse guardrail only resets when needed: it reads the
opener's current window and, if there's plenty of head-room, does nothing.
Only when the window is close to the cap does it land an inbound on the
opener to empty it — a cheap read per test, an actual SMS rarely.

"Opener" = whoever sends first in the test; the rule blocks the opener on
*their* window, and a window empties only on an inbound to that party, so
the reset lands on the opener. Penetrator opens by default (all current
tests); a test that opens the other way marks itself
``@pytest.mark.first_sender("aut")``.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")

# Server's unanswered-outbound cap (conversation_health.UNANSWERED_OUTBOUND_LIMIT).
UNANSWERED_LIMIT = 10
# Reset once the opener's window leaves fewer than this many free sends —
# comfortably more than any single test's opener-send count.
MIN_FREE_SLOTS = 4


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "first_sender(who): which side opens the conversation in this test "
        "('penetrator' default, or 'aut') — steers the pre-test window reset.",
    )


def _client(key: str):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


@pytest.fixture(scope="session")
def _reset_channel():
    """Session-cached reset endpoints, or None when the suite can't run.

    Returns ``(aut, aut_pid, aut_phone, remote, remote_pid, driver_phone)``:
    both SDK clients plus each side's phone-number id and E.164 number, so
    the reset can read/send in either direction. None off-CI (keys/numbers
    absent) makes the guardrail a no-op.
    """
    if not (REMOTE_KEY and AUT_KEY):
        return None
    try:
        aut = _client(AUT_KEY)
        remote = _client(REMOTE_KEY)
        aut_nums = aut.phone_numbers.list()
        remote_nums = remote.phone_numbers.list()
        if not (aut_nums and remote_nums):
            return None
        return (
            aut, str(aut_nums[0].id), aut_nums[0].number,
            remote, str(remote_nums[0].id), remote_nums[0].number,
        )
    except Exception:
        return None


def _window_count(client, pid: str, counterparty_number: str) -> int | None:
    """The opener's unanswered-outbound count in this conversation.

    Counts the opener's outbound to the counterparty since the opener's most
    recent inbound from them — the same "since last reply" window the server
    scores. Returns None if the history can't be read (treat as "unknown →
    reset to be safe").
    """
    tail = _digits(counterparty_number)[-10:]
    try:
        msgs = [
            m for m in client.texts.list(pid, limit=30)
            if _digits(getattr(m, "remote_phone_number", "") or "")[-10:] == tail
        ]
    except Exception:
        return None
    # Newest first, walk back until the last inbound; count outbound before it.
    msgs.sort(key=lambda m: str(getattr(m, "created_at", "")), reverse=True)
    count = 0
    for m in msgs:
        direction = (getattr(m, "direction", "") or "").lower()
        if direction == "inbound":
            break
        if direction == "outbound":
            count += 1
    return count


def _try_send(send_fn) -> bool:
    """Attempt one reset send; True if it landed, False on any block/error."""
    try:
        send_fn()
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _reset_conversation_health(request, _reset_channel):
    """Reset the opener's conversation window only when it's running low.

    See the module docstring for the who-opens / which-window logic.
    """
    if _reset_channel is None:
        yield
        return

    aut, aut_pid, aut_phone, remote, remote_pid, driver_phone = _reset_channel

    def smoke_to_pen():  # AUT → penetrator; empties the PENETRATOR's window
        aut.texts.send(aut_pid, to=driver_phone, text=_sync_body())

    def pen_to_smoke():  # penetrator → AUT; empties the AUT's window (wakes agent)
        remote.texts.send(remote_pid, to=aut_phone, text=_sync_body())

    marker = request.node.get_closest_marker("first_sender")
    opener = (marker.args[0] if marker and marker.args else "penetrator").lower()

    if opener == "aut":
        # AUT opens: guard the AUT's window; empty it with a penetrator→AUT poke.
        opener_client, opener_pid, counterparty = aut, aut_pid, driver_phone
        close, open_ = pen_to_smoke, smoke_to_pen
    else:
        # Penetrator opens: guard the driver's window; empty it with an AUT→driver poke.
        opener_client, opener_pid, counterparty = remote, remote_pid, aut_phone
        close, open_ = smoke_to_pen, pen_to_smoke

    window = _window_count(opener_client, opener_pid, counterparty)
    # Enough head-room → don't send anything (the common, spam-free path).
    if window is not None and UNANSWERED_LIMIT - window >= MIN_FREE_SLOTS:
        yield
        return

    # Window is low (or unknown) → land an inbound on the opener to empty it.
    if not _try_send(close):
        # Blocked → the counterparty's window is also full; drain both.
        _try_send(close)
        _try_send(open_)
        _try_send(close)

    yield


def _sync_body() -> str:
    # Unique + benign: never trips duplicate_body or the content filter.
    return f"[test-sync] conversation reset {uuid.uuid4().hex[:8]}"
