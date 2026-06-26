"""Live test: the agent emails back — and the reply is real, not an error.

A *remote* Inkbox identity emails the agent-under-test (AUT). The AUT's running
Hermes gateway routes it, "thinks" against a deterministic mock model (see
mock_openai.py — no real LLM, so this is repeatable and free), and emails a reply.

We assert three independent things so a broken setup can't pass:
  1. delivery  — a reply lands in the remote mailbox, tracked by thread_id;
  2. content   — the reply body carries the mock's ``REPLY_OK <nonce>`` marker and
                 contains NO error strings (this is what catches the agent emailing
                 back a model-auth 401 instead of a real reply);
  3. intent    — bonus: the in-gateway send-spy recorded the email send.

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

# Strings that mean the agent replied with a failure instead of a real answer.
ERROR_MARKERS = ("non-retryable error", "missing authentication", "http 401", "http 403", "traceback")

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY) or os.environ.get("LIVE_REAL_MODEL") == "1",
    reason="mock-model reachability test (needs both keys; skipped in real-model mode)",
)


def _mailbox(client) -> str:
    boxes = client.mailboxes.list()
    assert boxes, "identity has no mailbox"
    return boxes[0].email_address


def _spy_recorded_email_to(address: str) -> bool:
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

    nonce = f"smoke-{uuid.uuid4().hex[:8]}"
    subject = f"[{nonce}] are you there?"
    sent = remote.messages.send(
        remote_email,
        to=[aut_email],
        subject=subject,
        body_text="This is an automated reachability check — please reply to this email to confirm.",
    )
    thread_id = str(getattr(sent, "thread_id", "") or "")

    # Poll the remote mailbox for the AUT's reply — match on thread_id (preferred),
    # falling back to sender + nonce when the send didn't surface a thread id.
    def _is_reply(msg) -> bool:
        if thread_id and str(getattr(msg, "thread_id", "") or "") == thread_id:
            return True
        frm = (getattr(msg, "from_address", "") or "").lower()
        subj = getattr(msg, "subject", "") or ""
        return aut_email.lower() in frm and nonce in subj

    deadline = time.monotonic() + TIMEOUT_S
    reply = None
    while time.monotonic() < deadline and reply is None:
        for msg in remote.messages.list(remote_email, direction=MessageDirection.INBOUND):
            if _is_reply(msg):
                reply = msg
                break
        if reply is None:
            time.sleep(POLL_EVERY_S)

    # (1) delivery
    if reply is None:
        if _spy_recorded_email_to(remote_email):
            pytest.fail(
                f"Agent INTENDED to reply (spy saw a send to {remote_email}) but it did not "
                f"arrive within {TIMEOUT_S:.0f}s — delivery issue."
            )
        pytest.fail(f"No reply within {TIMEOUT_S:.0f}s and the agent never called email send.")

    # (2) content is a real reply, not an error fallback
    detail = remote.messages.get(remote_email, reply.id)
    body = ((getattr(detail, "body_text", "") or "") + " " + (getattr(reply, "subject", "") or "")).lower()
    bad = [m for m in ERROR_MARKERS if m in body]
    assert not bad, f"reply delivered but the body is an error, not a real answer: {bad}\n{body[:300]}"
    assert "reply_ok" in body, f"reply delivered but missing the mock marker REPLY_OK:\n{body[:300]}"
    assert nonce in body, f"reply did not echo the request nonce {nonce} — agent may not have read the inbound"

    # (3) intent (bonus; spy may not load in the gateway process)
    if SPY_FILE and not _spy_recorded_email_to(remote_email):
        print("note: reply delivered + verified, but the in-gateway send-spy recorded no send")
