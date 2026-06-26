"""Live intelligence test: the agent uses its REAL brain to answer correctly.

Unlike the mock-model reachability test, this runs against a real OpenAI model
(``LIVE_REAL_MODEL=1``, real key) so it proves the agent actually *reasons* — not
just that the pipe is connected. The remote identity emails a question whose
answer the system prompt can't pre-bake; a correct, non-error reply means the
real model received the message, thought, and replied through the full pipeline.

Skipped unless the live keys AND ``LIVE_REAL_MODEL=1`` are set, so it never runs
in the offline suite or the cheap mock lane.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
TIMEOUT_S = float(os.environ.get("LIVE_EMAIL_TIMEOUT", "150"))
POLL_EVERY_S = 5.0

ERROR_MARKERS = ("non-retryable error", "missing authentication", "http 401", "http 403", "traceback")

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY and os.environ.get("LIVE_REAL_MODEL") == "1"),
    reason="real-model intelligence test: needs both keys + LIVE_REAL_MODEL=1",
)


def _mailbox(client) -> str:
    boxes = client.mailboxes.list()
    assert boxes, "identity has no mailbox"
    return boxes[0].email_address


def test_agent_reasons_and_replies():
    from inkbox import Inkbox
    from inkbox.mail.types import MessageDirection

    remote = Inkbox(api_key=REMOTE_KEY, base_url=BASE_URL)
    aut = Inkbox(api_key=AUT_KEY, base_url=BASE_URL)
    remote_email = _mailbox(remote)
    aut_email = _mailbox(aut)

    nonce = f"smoke-{uuid.uuid4().hex[:8]}"
    # A question the agent must actually compute — answer is 42.
    sent = remote.messages.send(
        remote_email,
        to=[aut_email],
        subject=f"[{nonce}] quick question",
        body_text="Please reply with ONLY the single number equal to 6 multiplied by 7. Nothing else.",
    )
    thread_id = str(getattr(sent, "thread_id", "") or "")

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

    assert reply is not None, f"no reply within {TIMEOUT_S:.0f}s — agent did not answer"

    detail = remote.messages.get(remote_email, reply.id)
    body = (getattr(detail, "body_text", "") or "")
    low = body.lower()
    bad = [m for m in ERROR_MARKERS if m in low]
    assert not bad, f"reply is an error, not a real answer: {bad}\n{body[:300]}"
    # The real model must have computed 6*7. Allow it to phrase around the number.
    assert "42" in body, f"agent did not answer 42 (real reasoning failed):\n{body[:300]}"
