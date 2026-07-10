# tests/live/conftest.py
"""Shared guardrails for the live suite.

The live tests drive real SMS through the shared Inkbox 10DLC pool, which
the server protects with conversation-health rules (see servers
``conversation_health.py``): the same body sent twice with no reply
(``duplicate_body``), 10 unanswered outbound (``unanswered_limit``), or 5
carrier spam-fails in a row (``carrier_spam_backoff``) all block the next
send. Every one of those rules keys off the window *"since the recipient's
last inbound reply"* and **resets the moment an inbound lands** in that
direction.

Which window has to be clean depends on who sends FIRST in the test — the
rule blocks the sender based on *their* window, so the party that opens the
conversation is the one that must start clean. To empty a party's window we
land an inbound to them (the rule counts "outbound since their last
inbound"), so the reset must *end* on a send to whoever the test opens with.

Most tests open with the penetrator (driver → AUT), so the reset ends on an
AUT → penetrator poke. Tests that open the other way mark themselves
``@pytest.mark.first_sender("aut")`` and the reset flips to end on a
penetrator → AUT poke.

Reset strategy (per test): try the closing poke alone. If it lands, the
opener's window is already clean — done, no agent woken (the closing poke
is a direct SDK send, not an inbound the gateway routes to the model). If
it's blocked, the *other* window is full and is blocking the send; run the
full exchange — try-close, try-open (this clears the other window), try-close
again — which drains both. The open poke is the only step that can wake the
agent, and it only fires in that fallback case.
"""

from __future__ import annotations

import os
import uuid

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "first_sender(who): which side opens the conversation in this test "
        "('penetrator' default, or 'aut') — steers the pre-test window reset.",
    )


def _client(key: str):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


@pytest.fixture(scope="session")
def _reset_channel():
    """Session-cached reset endpoints, or None when the suite can't run.

    Returns ``(aut, aut_pid, aut_phone, remote, remote_pid, driver_phone)``:
    both SDK clients plus each side's phone-number id and E.164 number, so
    the reset can send in either direction. None off-CI (keys/numbers
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


def _try_send(send_fn) -> bool:
    """Attempt one reset send; True if it landed, False on any block/error."""
    try:
        send_fn()
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _reset_conversation_health(request, _reset_channel):
    """Give each test a clean conversation-health slate for its opener.

    See the module docstring for the who-sends-first / which-window logic.
    """
    if _reset_channel is None:
        yield
        return

    aut, aut_pid, aut_phone, remote, remote_pid, driver_phone = _reset_channel

    def _sync_body() -> str:
        # Unique + benign: never trips duplicate_body or the content filter.
        return f"[test-sync] conversation reset {uuid.uuid4().hex[:8]}"

    def smoke_to_pen():  # AUT -> penetrator; empties the PENETRATOR's window
        aut.texts.send(aut_pid, to=driver_phone, text=_sync_body())

    def pen_to_smoke():  # penetrator -> AUT; empties the AUT's window (wakes agent)
        remote.texts.send(remote_pid, to=aut_phone, text=_sync_body())

    marker = request.node.get_closest_marker("first_sender")
    opener = (marker.args[0] if marker and marker.args else "penetrator").lower()

    # Land the LAST reset send on the opener so THEIR window is empty when the
    # test's first real send goes out.
    if opener == "aut":
        close, open_ = pen_to_smoke, smoke_to_pen
    else:
        close, open_ = smoke_to_pen, pen_to_smoke

    if not _try_send(close):
        # Closing poke was blocked → the other window is full. Drain both:
        # try-close, try-open (clears the other window), try-close again.
        _try_send(close)
        _try_send(open_)
        _try_send(close)

    yield
