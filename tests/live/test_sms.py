"""Live SMS suite — the same questions as the email suite, over real SMS.

SMS differs from email: the remote must opt in (agent-to-agent SMS skips the
START opt-in — servers bypasses it for inter-agent traffic), and outbound SMS is
subject to carrier + spam filtering — so prompts ask for SHORT replies and avoid
spammy content.

  * mock leg → reachability (deterministic ``REPLY_OK`` from the mock model).
  * real leg → intelligence (gpt-5.5): basic, own identity, sender, tools.

Skipped unless both keys are set. Replies are matched by *new* inbound message id
from the AUT's number (robust to clock skew).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
REAL = os.environ.get("LIVE_REAL_MODEL") == "1"
TIMEOUT_S = float(os.environ.get("LIVE_SMS_TIMEOUT", "180"))
POLL_EVERY_S = 6.0
ERROR_MARKERS = ("non-retryable error", "missing authentication", "http 401", "http 403", "traceback")

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY),
    reason="live SMS suite: needs REMOTE_INKBOX_API_KEY + HERMES_INKBOX_API_KEY",
)
real_only = pytest.mark.skipif(not REAL, reason="intelligence runs in the real-model leg")
mock_only = pytest.mark.skipif(REAL, reason="reachability runs in the mock-model leg")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _client(key):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _phone(client):
    nums = client.phone_numbers.list()
    assert nums, "identity has no phone number"
    return nums[0].number, str(nums[0].id)


@pytest.fixture(scope="module")
def sms():
    remote = _client(REMOTE_KEY)
    aut = _client(AUT_KEY)
    aut_phone, _aut_pid = _phone(aut)
    _remote_phone, remote_pid = _phone(remote)
    # No opt-in/START needed: servers bypasses the missing-opt-in gate for
    # inter-agent traffic (recipient is a Telnyx-owned Inkbox number) — see
    # send_text_service.py. Only an explicit STOP/opt-out would block.
    return {"remote": remote, "aut": aut, "aut_phone": aut_phone, "remote_pid": remote_pid}


def _ask_sms(sms, text: str) -> str:
    """Text the agent; return the reply body (lowercased), matched by new message id."""
    remote, aut_phone, pid = sms["remote"], sms["aut_phone"], sms["remote_pid"]
    tail = _digits(aut_phone)[-10:]

    def _inbound_from_aut():
        out = []
        for m in remote.texts.list(pid, limit=30):
            if (getattr(m, "direction", "") or "").lower() == "inbound" \
                    and _digits(getattr(m, "remote_phone_number", "") or "")[-10:] == tail:
                out.append(m)
        return out

    before = {m.id for m in _inbound_from_aut()}
    remote.texts.send(pid, to=aut_phone, text=text)

    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        for m in _inbound_from_aut():
            if m.id not in before:
                body = getattr(m, "text", "") or ""
                bad = [x for x in ERROR_MARKERS if x in body.lower()]
                assert not bad, f"SMS reply is an error, not a real answer: {bad}\n{body[:200]}"
                return body.lower()
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"no SMS reply within {TIMEOUT_S:.0f}s to: {text!r}")


@mock_only
def test_sms_reachability(sms):
    body = _ask_sms(sms, "ping")
    assert "reply_ok" in body, f"mock reachability: missing REPLY_OK marker\n{body[:200]}"


@real_only
def test_sms_basic_reply(sms):
    body = _ask_sms(sms, "Please reply OK to confirm you got this text.")
    assert len(body.strip()) > 0, "empty reply"


@real_only
def test_sms_reports_own_identity(sms):
    aut_email = sms["aut"].mailboxes.list()[0].email_address
    body = _ask_sms(sms, "Reply with just your Inkbox email address and phone number — short.")
    assert aut_email in body, f"reply missing email {aut_email!r}\n{body[:200]}"


@real_only
def test_sms_reports_sender_details(sms):
    aut, remote = sms["aut"], sms["remote"]
    remote_email = remote.mailboxes.list()[0].email_address
    matches = aut.contacts.lookup(email=remote_email)
    if not matches:
        pytest.skip("no contact card for the sender to report")
    name = (getattr(matches[0], "preferred_name", None) or getattr(matches[0], "given_name", None) or "")
    body = _ask_sms(sms, "Who am I to you? Tell me what you have on file about me.")
    if name:
        assert name.lower() in body, f"reply missing sender name {name!r}\n{body[:200]}"


@real_only
def test_sms_aware_of_inkbox_tools(sms):
    import yaml

    manifest = yaml.safe_load(Path(__file__).resolve().parents[2].joinpath("plugin.yaml").read_text())
    tool_names = [t for t in manifest.get("provides_tools", []) if isinstance(t, str)]
    body = _ask_sms(sms, "Name three of your Inkbox tools (exact names).")
    hits = [t for t in tool_names if t.lower() in body]
    assert len(hits) >= 2, f"agent named only {hits} of its tools\n{body[:300]}"
