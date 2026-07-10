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

Most SMS tests self-reset — the agent answers each question, and that
inbound clears the driver's window. The trap is the tests where the agent
replies out-of-band (voice "call me" → the agent *calls* back, never texts;
a spam-blocked reply that never lands): the driver keeps texting a number
that never texts back, so the driver's window grows across tests and runs
until it trips.

This autouse guardrail gives each test a clean slate by resetting that
window directly: before each test the AUT pokes the driver over the API
(a real inbound to the driver's conversation with the AUT), which empties
the driver's conversation-health window. It's a direct SDK send from the
AUT's number, not an inbound to the agent, so it doesn't wake the model.
"""

from __future__ import annotations

import os
import uuid

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")


def _client(key: str):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


@pytest.fixture(scope="session")
def _reset_channel():
    """(aut_client, aut_phone_number_id, driver_phone_number) for resets, or None.

    Built once per session. Returns None when the live keys/numbers aren't
    available so the reset guardrail is a no-op off-CI.
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
        return aut, str(aut_nums[0].id), remote_nums[0].number
    except Exception:
        return None


@pytest.fixture(autouse=True)
def _reset_conversation_health(_reset_channel):
    """Empty the driver's conversation-health window before each test.

    The AUT sends the driver a fresh (unique-body) SMS directly over the
    API. That inbound resets the driver→AUT window server-side, so the
    test's own driver sends start from a clean slate — no duplicate_body /
    unanswered_limit carried over from a prior test or run. A reset that
    can't send (keys absent, transient error) is swallowed: the guardrail
    must never fail a test, only ever help it.
    """
    if _reset_channel is not None:
        aut, aut_pid, driver_phone = _reset_channel
        try:
            aut.texts.send(
                aut_pid,
                to=driver_phone,
                text=f"[test-sync] conversation reset {uuid.uuid4().hex[:8]}",
            )
        except Exception:
            pass  # best-effort — never let a reset failure fail the test
    yield
