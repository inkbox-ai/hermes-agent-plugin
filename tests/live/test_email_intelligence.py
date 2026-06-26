"""Live intelligence suite over email — the agent's REAL brain + tools.

Runs against a real OpenAI model (``LIVE_REAL_MODEL=1``, real key) so it proves
the agent actually reasons and uses its Inkbox tools/data — not a mock. A remote
identity emails questions; we verify the replies against values looked up live
through the API keys (NO hardcoded expectations):

  * basic        — answers a simple question (sanity).
  * own identity — reports its own handle / email / phone (looked up via the AUT key).
  * sender       — reports who the sender is, from the contact card it can see
                   (looked up via the AUT key).
  * tools        — names its real Inkbox tools (derived from plugin.yaml).

Skipped unless both keys + LIVE_REAL_MODEL=1 are set.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
TIMEOUT_S = float(os.environ.get("LIVE_EMAIL_TIMEOUT", "150"))
POLL_EVERY_S = 5.0
ERROR_MARKERS = ("non-retryable error", "missing authentication", "http 401", "http 403", "traceback")

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY and os.environ.get("LIVE_REAL_MODEL") == "1"),
    reason="real-model intelligence suite: needs both keys + LIVE_REAL_MODEL=1",
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _mailbox(client) -> str:
    boxes = client.mailboxes.list()
    assert boxes, "identity has no mailbox"
    return boxes[0].email_address


def _first_phone(client) -> str | None:
    nums = client.phone_numbers.list()
    return nums[0].number if nums else None


def _client(key):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _ask(remote, aut_email: str, remote_email: str, question: str) -> str:
    """Email the agent a question; return the reply body (lowercased)."""
    from inkbox.mail.types import MessageDirection

    nonce = f"smoke-{uuid.uuid4().hex[:8]}"
    sent = remote.messages.send(remote_email, to=[aut_email], subject=f"[{nonce}] {question[:40]}", body_text=question)
    thread_id = str(getattr(sent, "thread_id", "") or "")

    def _is_reply(msg) -> bool:
        if thread_id and str(getattr(msg, "thread_id", "") or "") == thread_id:
            return True
        frm = (getattr(msg, "from_address", "") or "").lower()
        return aut_email.lower() in frm and nonce in (getattr(msg, "subject", "") or "")

    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        for msg in remote.messages.list(remote_email, direction=MessageDirection.INBOUND):
            if _is_reply(msg):
                body = getattr(remote.messages.get(remote_email, msg.id), "body_text", "") or ""
                bad = [m for m in ERROR_MARKERS if m in body.lower()]
                assert not bad, f"reply is an error, not a real answer: {bad}\n{body[:300]}"
                return body.lower()
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"no reply within {TIMEOUT_S:.0f}s to: {question!r}")


@pytest.fixture(scope="module")
def ctx():
    remote = _client(REMOTE_KEY)
    aut = _client(AUT_KEY)
    return {
        "remote": remote,
        "aut": aut,
        "remote_email": _mailbox(remote),
        "aut_email": _mailbox(aut),
    }


def test_basic_reply(ctx):
    body = _ask(ctx["remote"], ctx["aut_email"], ctx["remote_email"],
                "Please reply with a one-sentence acknowledgement that you received this email.")
    assert len(body.strip()) > 0, "empty reply"


def test_reports_own_identity(ctx):
    aut = ctx["aut"]
    handle = _mailbox(aut).split("@", 1)[0]
    aut_email = ctx["aut_email"]
    aut_phone = _first_phone(aut)
    assert aut_phone, "AUT identity has no phone number to report"

    body = _ask(ctx["remote"], aut_email, ctx["remote_email"],
                "What is your Inkbox identity? Include ALL known info: your handle, "
                "your display name, your email address, and your phone number.")
    assert handle in body, f"reply missing handle {handle!r}\n{body[:400]}"
    assert aut_email in body, f"reply missing email {aut_email!r}\n{body[:400]}"
    assert _digits(aut_phone)[-10:] in _digits(body), f"reply missing phone {aut_phone!r}\n{body[:400]}"


def test_reports_sender_details(ctx):
    """The agent must report who the sender is, from the contact card it can see."""
    aut, remote = ctx["aut"], ctx["remote"]
    remote_email = ctx["remote_email"]

    # Look up (or seed) the sender's contact in the AUT org — the card the agent sees.
    matches = aut.contacts.lookup(email=remote_email)
    if not matches:
        from inkbox.contacts.types import ContactEmail, ContactPhone
        rphone = _first_phone(remote)
        aut.contacts.create(
            given_name="Penny",
            family_name="Tester",
            emails=[ContactEmail(label="work", value=remote_email)],
            phones=[ContactPhone(label="mobile", value=rphone)] if rphone else None,
        )
        matches = aut.contacts.lookup(email=remote_email)
    assert matches, "could not establish a contact card for the sender"
    contact = matches[0]
    name = (getattr(contact, "preferred_name", None) or getattr(contact, "given_name", None) or "")
    emails = [e.value for e in getattr(contact, "emails", [])]
    phones = [p.value for p in getattr(contact, "phones", [])]

    body = _ask(ctx["remote"], ctx["aut_email"], remote_email,
                "Who am I to you? Reply with the name, phone number, and email address "
                "you have on file for me.")
    if name:
        assert name.lower() in body, f"reply missing sender name {name!r}\n{body[:400]}"
    assert any(e.lower() in body for e in emails), f"reply missing sender email {emails}\n{body[:400]}"
    if phones:
        assert any(_digits(p)[-10:] in _digits(body) for p in phones), \
            f"reply missing sender phone {phones}\n{body[:400]}"


def test_aware_of_inkbox_tools(ctx):
    """Non-LLM proof the agent is wired with real tools: it names them."""
    import yaml

    manifest = yaml.safe_load(Path(__file__).resolve().parents[2].joinpath("plugin.yaml").read_text())
    tool_names = [t for t in manifest.get("provides_tools", []) if isinstance(t, str)]
    assert tool_names, "plugin.yaml provides_tools is empty"

    body = _ask(ctx["remote"], ctx["aut_email"], ctx["remote_email"],
                "List the exact names of all the Inkbox tools you have access to, one per line.")
    hits = [t for t in tool_names if t.lower() in body]
    assert len(hits) >= 3, f"agent named only {hits} of its tools {tool_names}\n{body[:500]}"
