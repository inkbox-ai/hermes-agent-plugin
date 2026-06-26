"""Live test: the agent emails back when emailed.

A *remote* Inkbox identity emails the agent-under-test (AUT) and waits for the
AUT's running Hermes gateway to route it, reason with a real model, and reply by
email. Two signals:

  * delivery (primary) — the reply actually lands in the remote mailbox;
  * intent (safety net) — the send-spy (``sitecustomize_spy``) recorded that the
    AUT called the email-send method. If delivery is slow but intent is present,
    the failure says so instead of a bare timeout.

Skipped unless both API keys are present, so it never runs in the offline suite.
Requires the AUT gateway to already be running (the workflow starts it).
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
SPY_FILE = os.environ.get("INKBOX_SPY_FILE")
TIMEOUT_S = float(os.environ.get("LIVE_EMAIL_TIMEOUT", "120"))
POLL_EVERY_S = 5.0

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY),
    reason="needs REMOTE_INKBOX_API_KEY + HERMES_INKBOX_API_KEY (live two-identity test)",
)


def _mailbox(client) -> str:
    boxes = client.mailboxes.list()
    assert boxes, "identity has no mailbox"
    return boxes[0].email_address


def _spy_recorded_email_to(address: str) -> bool:
    """True if the send-spy recorded an email send addressed to *address*."""
    if not SPY_FILE or not os.path.exists(SPY_FILE):
        return False
    address = address.lower()
    for line in open(SPY_FILE, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "Messages" in rec.get("method", "") and address in json.dumps(rec.get("kwargs", {})).lower():
            return True
    return False


def test_agent_emails_back():
    from inkbox import Inkbox
    from inkbox.mail.types import MessageDirection

    remote = Inkbox(api_key=REMOTE_KEY, base_url=BASE_URL)
    aut = Inkbox(api_key=AUT_KEY, base_url=BASE_URL)

    remote_email = _mailbox(remote)
    aut_email = _mailbox(aut)
    assert remote_email.lower() != aut_email.lower(), "remote and AUT must be different identities"

    nonce = uuid.uuid4().hex[:8]
    subject = f"[smoke-{nonce}] are you there?"
    remote.messages.send(
        remote_email,
        to=[aut_email],
        subject=subject,
        body_text="This is an automated reachability check — please reply to this email to confirm.",
    )

    # Poll the remote mailbox for the AUT's reply (newest first).
    deadline = time.monotonic() + TIMEOUT_S
    reply = None
    while time.monotonic() < deadline and reply is None:
        for msg in remote.messages.list(remote_email, direction=MessageDirection.INBOUND):
            frm = (getattr(msg, "from_address", "") or "").lower()
            subj = getattr(msg, "subject", "") or ""
            if aut_email.lower() in frm and nonce in subj:
                reply = msg
                break
        if reply is None:
            time.sleep(POLL_EVERY_S)

    if reply is None:
        if _spy_recorded_email_to(remote_email):
            pytest.fail(
                f"Agent INTENDED to reply (spy saw an email send to {remote_email}) "
                f"but the reply did not arrive within {TIMEOUT_S:.0f}s — delivery issue."
            )
        pytest.fail(
            f"No reply within {TIMEOUT_S:.0f}s and the agent never called email send — "
            f"it did not reason its way to replying."
        )

    # Delivered. If the spy was wired up, confirm the intent matched too.
    if SPY_FILE:
        assert _spy_recorded_email_to(remote_email), "reply arrived but spy recorded no matching send"
