"""Outbound delivery-failure feedback loop.

Covers both failure surfaces on every channel:
  - synchronous send rejections (server content policy 422, opt-out 402,
    email send errors, local too-long guards) → agent woken with the error;
  - asynchronous delivery-failure webhooks (text.delivery_failed,
    imessage.delivery_failed, message.bounced / message.failed) → same.

And the budget mechanics: max OUTBOUND_FAILURE_MAX_ATTEMPTS sends per
logical reply shared across both surfaces, reset on inbound / delivered /
TTL, replay-deduped webhooks.
"""

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import InkboxAdapter


MAX = adapter_mod.OUTBOUND_FAILURE_MAX_ATTEMPTS


class SpamBlockError(Exception):
    """Shaped like the SDK error for the server's content-policy 422."""

    status_code = 422
    detail = {
        "error": "message_blocked_spam_filter",
        "rule": "markdown_artifacts",
        "text_message_id": "txt-blocked",
        "message": "Markdown formatting (headers/bold/code fences) reads as bot traffic in SMS.",
    }


class TransientError(Exception):
    """Shaped like a 503 the host gateway retries on its own."""

    status_code = 503
    detail = {"error": "carrier_unavailable", "message": "upstream temporarily unavailable"}


class OptOutError(Exception):
    """Shaped like the iMessage-line 402 for an opted-out recipient."""

    status_code = 402
    detail = {
        "error": "recipient_opted_out",
        "message": "Recipient has opted out of messages from this line.",
    }


class FakeText:
    id = "txt-1"
    delivery_status = "queued"
    conversation_id = "conv-123"


class FakeIdentity:
    def __init__(self, *, text_exc=None, imessage_exc=None, email_exc=None):
        self.sent_texts = []
        self.sent_imessages = []
        self.sent_emails = []
        self._text_exc = text_exc
        self._imessage_exc = imessage_exc
        self._email_exc = email_exc

    def send_text(self, **kwargs):
        if self._text_exc is not None:
            raise self._text_exc
        self.sent_texts.append(kwargs)
        return FakeText()

    def send_imessage(self, **kwargs):
        if self._imessage_exc is not None:
            raise self._imessage_exc
        self.sent_imessages.append(kwargs)
        return FakeText()

    def send_email(self, **kwargs):
        if self._email_exc is not None:
            raise self._email_exc
        self.sent_emails.append(kwargs)
        return types.SimpleNamespace(id="mail-1")


class FakeInkboxClient:
    def __init__(self, identity):
        self.identity = identity
        self.contacts = types.SimpleNamespace(get=lambda _contact_id: None)

    def get_identity(self, _handle):
        return self.identity


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )


@pytest.fixture(autouse=True)
def inline_to_thread(monkeypatch):
    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)


def _adapter(identity, *, contact=None):
    """Bare adapter with just the state the failure loop touches."""
    adapter = object.__new__(InkboxAdapter)
    adapter.platform = "inkbox"
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._seen_request_ids = {}
    adapter._inflight_request_ids = {}
    adapter._outbound_failure_state = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_sms = {}
    adapter._last_inbound_imessage = {}
    adapter._last_inbound_email = {}
    adapter._stop_imessage_typing = lambda *_a, **_k: None
    adapter._resolve_channel_overrides = lambda *_a, **_k: (None, None)

    async def _resolve_contact_full(**_kwargs):
        return contact

    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueued = []

    async def _capture(event):
        adapter._enqueued.append(event)

    adapter._enqueue = _capture

    async def _capture_sms_event(event):
        adapter._enqueued.append(event)

    adapter._enqueue_sms_text_event = _capture_sms_event
    return adapter


def _sms_adapter(identity):
    adapter = _adapter(identity)
    adapter._last_inbound_modality["contact-123"] = "sms"
    adapter._last_inbound_sms["contact-123"] = {
        "conversation_id": "conv-123",
        "remote_phone_number": "+15555550101",
        "text_id": "txt-in",
    }
    return adapter


def _send_sms(adapter, text="**Jane Doe** is on file."):
    return asyncio.run(adapter.send("contact-123", text, metadata={"mode": "sms"}))


def _delivery_failed_envelope(text_id="txt-out-1", conversation_id="conv-123"):
    return {
        "id": f"evt-{text_id}",
        "event_type": "text.delivery_failed",
        "data": {
            "text_message": {
                "id": text_id,
                "direction": "outbound",
                "local_phone_number": "+15555550100",
                "remote_phone_number": "+15555550101",
                "conversation_id": conversation_id,
                "text": "Sorry Kim — the site isn't built yet.",
                "delivery_status": "delivery_failed",
                "error_code": "40002",
                "error_detail": (
                    "The message was flagged by a SPAM filter and was not "
                    "delivered. This is a temporary condition."
                ),
            },
        },
    }


# ── Synchronous send rejections ─────────────────────────────────────────


def test_sms_spam_block_wakes_agent_with_rule():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))

    result = _send_sms(adapter)

    assert result.success is False
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert event.text.startswith(
        f"[inkbox:delivery_failure channel=sms stage=send_rejected attempt=1/{MAX}"
    )
    assert "message_blocked_spam_filter rule=markdown_artifacts" in event.text
    assert "reads as bot traffic in SMS" in event.text
    assert "«**Jane Doe** is on file.»" in event.text
    assert "[SILENT]" in event.text
    # The wake-up must land in the SMS conversation's session.
    assert event.source.chat_id == "contact-123"
    assert event.source.user_id == "contact-123"


def test_sms_retry_budget_caps_total_sends():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))

    for _ in range(MAX + 1):
        _send_sms(adapter)

    # Failures 1 and 2 wake the agent (sends 2 and 3); failures 3+ stay quiet.
    assert len(adapter._enqueued) == MAX - 1
    assert f"attempt=1/{MAX}" in adapter._enqueued[0].text
    assert f"attempt=2/{MAX}" in adapter._enqueued[1].text


def test_transient_sms_error_does_not_wake_agent():
    adapter = _sms_adapter(FakeIdentity(text_exc=TransientError()))

    result = _send_sms(adapter)

    assert result.success is False
    assert result.retryable is True  # host gateway owns transient retries
    assert adapter._enqueued == []
    assert adapter._outbound_failure_state == {}


def test_successful_send_does_not_wake_or_count():
    adapter = _sms_adapter(FakeIdentity())

    result = _send_sms(adapter, "all good")

    assert result.success is True
    assert adapter._enqueued == []
    assert adapter._outbound_failure_state == {}


def test_sms_too_long_wakes_agent():
    adapter = _sms_adapter(FakeIdentity())

    result = _send_sms(adapter, "x" * (adapter_mod.SMS_MAX_LENGTH + 1))

    assert result.success is False
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert "channel=sms stage=send_rejected" in event.text
    assert "sms_too_long" in event.text


def test_imessage_opt_out_wakes_agent():
    adapter = _adapter(FakeIdentity(imessage_exc=OptOutError()))
    adapter._last_inbound_modality["contact-123"] = "imessage"
    adapter._last_inbound_imessage["contact-123"] = {
        "conversation_id": "imsg-conv-1",
        "remote_number": "+15555550101",
        "message_id": "imsg-in",
    }

    result = asyncio.run(
        adapter.send("contact-123", "hello again", metadata={"mode": "imessage"})
    )

    assert result.success is False
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert "channel=imessage stage=send_rejected" in event.text
    assert "recipient_opted_out" in event.text
    assert "opted out" in event.text


def test_email_send_failure_wakes_agent():
    adapter = _adapter(
        FakeIdentity(email_exc=Exception("550 mailbox unavailable")),
    )
    adapter._last_inbound_modality["contact-123"] = "email"
    adapter._last_inbound_email["contact-123"] = {
        "subject": "Project",
        "rfc_message_id": "<abc@mail>",
        "from_address": "kim@example.com",
    }

    result = asyncio.run(
        adapter.send("contact-123", "Here is the update.", metadata={"mode": "email"})
    )

    assert result.success is False
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert "channel=email stage=send_rejected" in event.text
    assert "550 mailbox unavailable" in event.text
    assert "to=kim@example.com" in event.text


# ── Asynchronous delivery-failure webhooks ──────────────────────────────


def test_carrier_delivery_failed_wakes_agent():
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123", "name": "Kim"})

    response = asyncio.run(adapter._on_text_lifecycle(_delivery_failed_envelope()))

    assert response.status == 200
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert "channel=sms stage=delivery_failed" in event.text
    assert f"attempt=1/{MAX}" in event.text
    assert "[40002]" in event.text
    assert "flagged by a SPAM filter" in event.text
    assert "Sorry Kim — the site isn't built yet." in event.text
    # Routed into the contact's session, thread-scoped to the conversation.
    assert event.source.chat_id == "contact-123"
    assert event.source.thread_id == "sms:conv-123"
    # Resend routing state is populated for a post-restart gateway.
    assert adapter._last_inbound_modality["contact-123"] == "sms"
    assert adapter._last_inbound_sms["contact-123"]["conversation_id"] == "conv-123"


def test_carrier_delivery_failed_replay_is_deduped():
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123", "name": "Kim"})
    envelope = _delivery_failed_envelope()

    first = asyncio.run(adapter._on_text_lifecycle(envelope))
    second = asyncio.run(adapter._on_text_lifecycle(envelope))

    assert first.status == 200
    assert second.text == "duplicate"
    assert len(adapter._enqueued) == 1


def test_carrier_delivery_unconfirmed_does_not_wake():
    # text.delivery_unconfirmed is carrier *uncertainty*, not a failure —
    # the message usually landed. Waking the agent here would resend a
    # message the recipient likely already has. Ack + log only, and the
    # retry budget stays untouched.
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123", "name": "Kim"})
    envelope = _delivery_failed_envelope(text_id="txt-unconfirmed")
    envelope["event_type"] = "text.delivery_unconfirmed"
    envelope["data"]["text_message"]["delivery_status"] = "delivery_unconfirmed"
    envelope["data"]["text_message"]["error_code"] = None
    envelope["data"]["text_message"]["error_detail"] = None

    response = asyncio.run(adapter._on_text_lifecycle(envelope))

    assert response.status == 200
    assert adapter._enqueued == []
    assert adapter._outbound_failure_state == {}


def test_delivery_unconfirmed_stays_subscribed():
    # Still subscribed — the uncertainty lands in the gateway log even
    # though it never wakes the agent.
    assert "text.delivery_unconfirmed" in adapter_mod._DESIRED_TEXT_EVENTS


def test_non_failure_lifecycle_events_do_not_wake():
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123"})
    sent = _delivery_failed_envelope()
    sent["event_type"] = "text.sent"
    inbound_fail = _delivery_failed_envelope(text_id="txt-out-2")
    inbound_fail["data"]["text_message"]["direction"] = "inbound"

    asyncio.run(adapter._on_text_lifecycle(sent))
    asyncio.run(adapter._on_text_lifecycle(inbound_fail))

    assert adapter._enqueued == []
    assert adapter._outbound_failure_state == {}


def test_group_delivery_failed_reads_recipient_row():
    adapter = _adapter(FakeIdentity(), contact=None)
    envelope = _delivery_failed_envelope()
    msg = envelope["data"]["text_message"]
    msg["remote_phone_number"] = None
    msg["error_code"] = None
    msg["error_detail"] = None
    msg["recipients"] = [
        {
            "recipient_phone_number": "+15555550101",
            "delivery_status": "delivery_failed",
            "error_code": "40002",
            "error_detail": "Flagged by a SPAM filter.",
        },
    ]
    envelope["data"]["recipient_phone_number"] = "+15555550101"

    asyncio.run(adapter._on_text_lifecycle(envelope))

    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert "[40002]" in event.text
    assert "Flagged by a SPAM filter." in event.text


def test_imessage_delivery_failed_wakes_agent():
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123", "name": "Kim"})

    response = asyncio.run(adapter._on_imessage_lifecycle({
        "id": "evt-imsg-1",
        "event_type": "imessage.delivery_failed",
        "data": {
            "message": {
                "id": "imsg-out-1",
                "direction": "outbound",
                "remote_number": "+15555550101",
                "conversation_id": "imsg-conv-1",
                "content": "See you at 5!",
                "status": "delivery_failed",
                "error_code": "OPTED_OUT",
                "error_detail": "Recipient has opted out.",
            },
        },
    }))

    assert response.status == 200
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert "channel=imessage stage=delivery_failed" in event.text
    assert "[OPTED_OUT]" in event.text
    assert "See you at 5!" in event.text
    assert event.source.thread_id == "imessage:imsg-conv-1"
    assert adapter._last_inbound_imessage["contact-123"]["conversation_id"] == "imsg-conv-1"


def test_mail_bounce_wakes_agent_and_failed_is_deduped():
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123", "name": "Kim"})
    envelope = {
        "id": "evt-mail-1",
        "event_type": "message.bounced",
        "data": {
            "message": {
                "id": "mail-out-1",
                "mailbox_id": "mb-1",
                "thread_id": "thread-1",
                "message_id": "<out-1@inkboxmail.com>",
                "from_address": "agent@inkboxmail.com",
                "to_addresses": ["kim@example.com"],
                "subject": "Your website",
                "snippet": "Here is the plan for the build.",
                "direction": "outbound",
                "status": "bounced",
            },
        },
    }

    first = asyncio.run(adapter._on_mail_delivery_failure(envelope))
    failed = dict(envelope, event_type="message.failed")
    second = asyncio.run(adapter._on_mail_delivery_failure(failed))

    assert first.status == 200
    assert second.text == "duplicate"
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert "channel=email stage=bounced" in event.text
    assert "kim@example.com" in event.text
    assert "Here is the plan for the build." in event.text
    assert event.source.thread_id == "email:thread-1"
    # Resend threading state for a post-restart gateway.
    assert adapter._last_inbound_email["contact-123"]["from_address"] == "kim@example.com"
    assert adapter._last_inbound_email["contact-123"]["rfc_message_id"] == "<out-1@inkboxmail.com>"


def test_mail_inbound_direction_never_wakes():
    adapter = _adapter(FakeIdentity())
    envelope = {
        "event_type": "message.bounced",
        "data": {
            "message": {
                "id": "mail-in-1",
                "direction": "inbound",
                "to_addresses": ["agent@inkboxmail.com"],
            },
        },
    }

    asyncio.run(adapter._on_mail_delivery_failure(envelope))

    assert adapter._enqueued == []


def test_mail_failure_events_are_subscribed():
    assert "message.bounced" in adapter_mod._DESIRED_MAIL_EVENTS
    assert "message.failed" in adapter_mod._DESIRED_MAIL_EVENTS
    assert "message.received" in adapter_mod._DESIRED_MAIL_EVENTS


# ── Budget mechanics across surfaces ────────────────────────────────────


def test_sync_and_webhook_failures_share_one_budget():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))

    _send_sms(adapter)  # failure 1 (sync, keyed by conv + number + chat)
    asyncio.run(adapter._on_text_lifecycle(_delivery_failed_envelope()))  # failure 2

    assert len(adapter._enqueued) == 2
    assert f"attempt=1/{MAX}" in adapter._enqueued[0].text
    assert f"attempt=2/{MAX}" in adapter._enqueued[1].text


def test_inbound_sms_resets_budget():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))
    async def _resolve_contact_full(**_kwargs):
        return {"id": "contact-123", "name": "Kim"}
    adapter._resolve_contact_full = _resolve_contact_full

    _send_sms(adapter)
    _send_sms(adapter)
    inbound = asyncio.run(adapter._on_text_received({
        "event_type": "text.received",
        "data": {
            "text_message": {
                "id": "txt-in-2",
                "direction": "inbound",
                "remote_phone_number": "+15555550101",
                "local_phone_number": "+15555550100",
                "conversation_id": "conv-123",
                "text": "Any update?",
            },
        },
    }))
    _send_sms(adapter)

    assert inbound.status == 200
    # 2 failure wakes + 1 inbound turn + 1 fresh failure wake back at 1/MAX.
    failure_events = [e for e in adapter._enqueued if "delivery_failure" in e.text]
    assert len(failure_events) == 3
    assert f"attempt=1/{MAX}" in failure_events[2].text


def test_delivered_receipt_resets_budget():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))

    _send_sms(adapter)
    _send_sms(adapter)
    delivered = _delivery_failed_envelope(text_id="txt-ok")
    delivered["event_type"] = "text.delivered"
    delivered["data"]["text_message"]["delivery_status"] = "delivered"
    asyncio.run(adapter._on_text_lifecycle(delivered))
    _send_sms(adapter)

    assert len(adapter._enqueued) == 3
    assert f"attempt=1/{MAX}" in adapter._enqueued[2].text


def test_budget_expires_after_ttl():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))

    _send_sms(adapter)
    # Age every counter entry past the TTL.
    for entry in adapter._outbound_failure_state.values():
        entry["at"] = time.time() - adapter_mod.OUTBOUND_FAILURE_STATE_TTL_SECONDS - 1
    _send_sms(adapter)

    assert len(adapter._enqueued) == 2
    assert f"attempt=1/{MAX}" in adapter._enqueued[1].text
