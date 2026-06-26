"""Live cross-channel suite — the agent answers on a DIFFERENT channel.

Ask on one channel; the agent must figure out the sender's *other-channel* address
from the contact card and respond there. Each request carries a short token, and we
assert that token shows up on the other channel — proving the response is tied to
the request.

  * email -> SMS : email asks for a text; we poll SMS for the token.
  * SMS  -> email: SMS asks for an email; we poll email for the token.

More channels (iMessage, voice) get added here. Real-model only.
"""

from __future__ import annotations

import os
import re
import time
import uuid

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
REAL = os.environ.get("LIVE_REAL_MODEL") == "1"
TIMEOUT_S = float(os.environ.get("LIVE_XCHANNEL_TIMEOUT", "200"))
POLL_EVERY_S = 6.0

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY and REAL),
    reason="cross-channel suite: needs both keys + LIVE_REAL_MODEL=1",
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _client(key):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _token() -> str:
    return uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def xc():
    remote = _client(REMOTE_KEY)
    aut = _client(AUT_KEY)
    remote_email = remote.mailboxes.list()[0].email_address
    aut_email = aut.mailboxes.list()[0].email_address
    rnums = remote.phone_numbers.list()
    anums = aut.phone_numbers.list()
    assert rnums and anums, "both identities need a phone number for cross-channel"
    remote_phone, remote_pid = rnums[0].number, str(rnums[0].id)
    aut_phone = anums[0].number

    # The agent can only cross channels if the sender's card has BOTH an email and a
    # phone. Ensure it does (merge in whatever is missing; never clobber existing data).
    from inkbox.contacts.types import ContactEmail, ContactPhone
    matches = aut.contacts.lookup(email=remote_email)
    if not matches:
        aut.contacts.create(
            given_name="Penny", family_name="Tester",
            emails=[ContactEmail("work", remote_email)],
            phones=[ContactPhone("mobile", remote_phone)],
        )
    else:
        c = matches[0]
        emails = list(getattr(c, "emails", []))
        phones = list(getattr(c, "phones", []))
        changed = False
        if not any((e.value or "").lower() == remote_email.lower() for e in emails):
            emails.append(ContactEmail("work", remote_email))
            changed = True
        if not any(_digits(p.value)[-10:] == _digits(remote_phone)[-10:] for p in phones):
            phones.append(ContactPhone("mobile", remote_phone))
            changed = True
        if changed:
            aut.contacts.update(c.id, emails=emails, phones=phones)

    return {
        "remote": remote, "aut": aut,
        "remote_email": remote_email, "remote_pid": remote_pid,
        "aut_email": aut_email, "aut_phone": aut_phone,
    }


def test_email_request_gets_sms_response(xc):
    """Email asks the agent to TEXT a code; the code must arrive over SMS."""
    remote, remote_pid, aut_phone = xc["remote"], xc["remote_pid"], xc["aut_phone"]
    token = _token()
    tail = _digits(aut_phone)[-10:]

    def _sms_from_aut():
        return [m for m in remote.texts.list(remote_pid, limit=30)
                if (getattr(m, "direction", "") or "").lower() == "inbound"
                and _digits(getattr(m, "remote_phone_number", "") or "")[-10:] == tail]

    before = {m.id for m in _sms_from_aut()}
    remote.messages.send(
        xc["remote_email"], to=[xc["aut_email"]], subject=f"[{token}] text me please",
        body_text=f"Please send me a text message (SMS) that says: lalala {token}",
    )

    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        for m in _sms_from_aut():
            if m.id not in before and token in (getattr(m, "text", "") or "").lower():
                return  # cross-channel confirmed: email request -> SMS response with the token
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"agent did not send an SMS containing {token!r} within {TIMEOUT_S:.0f}s")


def test_sms_request_gets_email_response(xc):
    """SMS asks the agent to EMAIL a code; the code must arrive over email."""
    from inkbox.mail.types import MessageDirection

    remote, remote_email, aut_email = xc["remote"], xc["remote_email"], xc["aut_email"]
    token = _token()

    def _email_from_aut():
        return [m for m in remote.messages.list(remote_email, direction=MessageDirection.INBOUND)
                if aut_email.lower() in (getattr(m, "from_address", "") or "").lower()]

    before = {m.id for m in _email_from_aut()}
    remote.texts.send(xc["remote_pid"], to=xc["aut_phone"], text=f"Please email me the code {token}.")

    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        for m in _email_from_aut():
            if m.id in before:
                continue
            hay = (getattr(m, "subject", "") or "").lower()
            if token not in hay:
                body = getattr(remote.messages.get(remote_email, m.id), "body_text", "") or ""
                hay = body.lower()
            if token in hay:
                return  # cross-channel confirmed: SMS request -> email response with the token
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"agent did not send an email containing {token!r} within {TIMEOUT_S:.0f}s")
