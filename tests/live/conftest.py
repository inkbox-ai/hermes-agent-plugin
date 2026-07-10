# tests/live/conftest.py
"""Shared guardrails for the live suite.

The live tests drive real SMS through the shared Inkbox 10DLC pool, which
the server protects with spam/rate rules (see servers
``conversation_health.py`` + ``send_text_service.py``). A chatty suite or
repeated CI runs can trip them, and the worst offender is the per-number
24-hour recipient cap (``sender_rate_limited``, 429): once the AUT hits
it, it physically cannot send SMS replies, so every reply-dependent test
would otherwise wait out its full timeout and cascade red for ~15 minutes.

This autouse guardrail gives each test a clean-slate precondition: if the
gateway already logged the 24h cap, skip immediately with a loud reason
instead of marching through timeouts. Combined with the in-test rate
checks and the per-send body diversification, the suite degrades to
honest skips ("live-infra capacity, not a plugin failure") rather than a
false red.

The DURABLE fix is server-side and out of scope for this repo: raise the
CI org's ``phone.sms_per_number_per_24h`` quota (per-org override) or put
the live-test numbers on a dedicated 10DLC campaign, which exempts them
from every shared-pool rule.
"""

from __future__ import annotations

import os

import pytest

GATEWAY_LOG = os.environ.get("GATEWAY_LOG", "")

# The server error code that means "this number is out of 24h SMS quota".
_RATE_LIMIT_MARKER = "sender_rate_limited"


def _gateway_log_text() -> str:
    """Whole gateway log, or '' when it isn't wired / readable."""
    if not GATEWAY_LOG or not os.path.exists(GATEWAY_LOG):
        return ""
    try:
        with open(GATEWAY_LOG, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def aut_sms_rate_limited() -> bool:
    """True if the gateway has logged the AUT hitting its 24h SMS cap."""
    return _RATE_LIMIT_MARKER in _gateway_log_text()


_RATE_LIMIT_SKIP_REASON = (
    "AUT outbound SMS is rate-limited (429 sender_rate_limited) — the shared "
    "live-test number hit its 24h send cap. Live-infra capacity, not a "
    "transport/plugin failure. Durable fix: raise the CI org's "
    "phone.sms_per_number_per_24h quota (per-org override) or move the "
    "live-test numbers to a dedicated 10DLC campaign; or let the 24h window "
    "age out."
)


@pytest.fixture(autouse=True)
def _sms_quota_guardrail():
    """Skip a live test upfront when the AUT has already exhausted its SMS cap.

    Once one test trips ``sender_rate_limited``, the cap holds for the rest
    of the 24h window — so every subsequent test skips instantly here rather
    than waiting out a 180s reply timeout and cascading. A clean-slate
    precondition check, not a timeout march.
    """
    if aut_sms_rate_limited():
        pytest.skip(_RATE_LIMIT_SKIP_REASON)
    yield
