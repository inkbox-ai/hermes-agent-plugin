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
import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import InkboxAdapter


MAX = adapter_mod.OUTBOUND_FAILURE_MAX_ATTEMPTS


@pytest.mark.parametrize(
    ("error_code", "error_detail", "expected"),
    [
        ("40002", "Flagged by a SPAM filter; temporary condition", "retry"),
        ("message_blocked_spam_filter", "Markdown content rejected", "retry"),
        ("message_too_long", "Message content is too long", "retry"),
        ("carrier_unavailable", "Service temporarily unavailable", "retry"),
        ("recipient_opted_out", "Recipient opted out", "stop"),
        ("invalid_phone_number", "Invalid destination", "stop"),
        ("unknown", "Destination is unreachable", "stop"),
        ("content_rejected", "Unsafe or harmful content", "stop"),
        ("unknown", "Provider rejected the message", "conditional"),
    ],
)
def test_sms_delivery_failure_policy(error_code, error_detail, expected):
    assert (
        adapter_mod._sms_delivery_failure_policy(error_code, error_detail)
        == expected
    )


@pytest.mark.parametrize(
    ("attempt", "error_code", "error_detail", "required", "forbidden"),
    [
        (
            1,
            "40002",
            "Temporary spam filter rejection",
            "FIRST SAFE RETRY REQUIRED",
            "[SILENT]",
        ),
        (
            2,
            "40002",
            "Temporary spam filter rejection",
            "RETRY OPTIONAL",
            "FIRST SAFE RETRY REQUIRED",
        ),
        (
            1,
            "recipient_opted_out",
            "Recipient opted out",
            "DO NOT RETRY",
            "FIRST SAFE RETRY REQUIRED",
        ),
        (
            2,
            "invalid_phone_number",
            "Destination unreachable",
            "DO NOT RETRY",
            "RETRY OPTIONAL",
        ),
        (
            1,
            "unknown",
            "Provider rejected the message",
            "REVIEW BEFORE RETRY",
            "FIRST SAFE RETRY REQUIRED",
        ),
        (
            2,
            "unknown",
            "Provider rejected the message",
            "REVIEW BEFORE RETRY",
            "RETRY OPTIONAL",
        ),
    ],
)
def test_delivery_failure_instruction_uses_attempt_and_classification(
    attempt,
    error_code,
    error_detail,
    required,
    forbidden,
):
    instruction = adapter_mod._delivery_failure_reply_instruction(
        mode="sms",
        error_code=error_code,
        error_detail=error_detail,
        attempt=attempt,
    )
    assert required in instruction
    assert forbidden not in instruction
    if "DO NOT RETRY" in required or "REVIEW BEFORE RETRY" in required:
        assert "[SILENT]" in instruction
    if required == "RETRY OPTIONAL":
        assert "[SILENT]" in instruction


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

    def reply_all_email(self, message_id, **kwargs):
        return self.send_email(reply_to_message_id=message_id, **kwargs)

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
    adapter_mod._GLOBAL_OUTBOUND_CONTEXT.clear()
    adapter._outbound_context = adapter_mod._GLOBAL_OUTBOUND_CONTEXT
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
    assert "zero emoji" in event.text
    assert "«**Jane Doe** is on file.»" in event.text
    assert "SMS failure classification: FIRST SAFE RETRY REQUIRED" in event.text
    assert "MUST now send exactly one safe, materially rephrased SMS" in event.text
    assert "[SILENT]" not in event.text
    assert "If there is nothing sensible to send" not in event.text
    # The wake-up must land in the SMS conversation's session.
    assert event.source.chat_id == "contact-123"
    assert event.source.user_id == "contact-123"


def test_host_plain_text_fallback_does_not_wake_agent_twice():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))

    _send_sms(adapter)
    _send_sms(
        adapter,
        "(Response formatting failed, plain text:)\n\n**Jane Doe** is on file.",
    )

    assert len(adapter._enqueued) == 1
    assert "attempt=1/" in adapter._enqueued[0].text


def test_host_plain_text_fallback_still_wakes_without_prior_failure():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))

    _send_sms(
        adapter,
        "(Response formatting failed, plain text:)\n\n**Jane Doe** is on file.",
    )

    assert len(adapter._enqueued) == 1


def test_sms_retry_budget_caps_total_sends():
    adapter = _sms_adapter(FakeIdentity(text_exc=SpamBlockError()))

    for _ in range(MAX + 1):
        _send_sms(adapter)

    # Failures 1 and 2 wake the agent (sends 2 and 3); failures 3+ stay quiet.
    assert len(adapter._enqueued) == MAX - 1
    assert f"attempt=1/{MAX}" in adapter._enqueued[0].text
    assert f"attempt=2/{MAX}" in adapter._enqueued[1].text
    assert "FIRST SAFE RETRY REQUIRED" in adapter._enqueued[0].text
    assert "[SILENT]" not in adapter._enqueued[0].text
    assert "RETRY OPTIONAL" in adapter._enqueued[1].text
    assert "[SILENT]" in adapter._enqueued[1].text


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
    assert "SMS failure classification: FIRST SAFE RETRY REQUIRED" in event.text
    assert "[SILENT]" not in event.text


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
        "stored_message_id": "stored-mail-1",
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
    assert "SMS failure classification: FIRST SAFE RETRY REQUIRED" in event.text
    assert "[SILENT]" not in event.text
    # Routed into the contact's session, thread-scoped to the conversation.
    assert event.source.chat_id == "contact-123"
    assert event.source.thread_id == "sms:conv-123"
    # Resend routing state is populated for a post-restart gateway.
    assert adapter._last_inbound_modality["contact-123"] == "sms"
    assert adapter._last_inbound_sms["contact-123"]["conversation_id"] == "conv-123"


@pytest.mark.parametrize("call_state", ["none", "active", "closed", "hosted"])
@pytest.mark.parametrize("conversation_id", ["conv-123", ""])
def test_callback_only_sms_recovery_keeps_original_sink(call_state, conversation_id):
    """A real callback/queue/send path must not retry through voice or a newer SMS."""
    async def run():
        identity = FakeIdentity()
        adapter = _adapter(identity, contact={"id": "contact-123", "name": "Kim"})
        ws = AsyncMock()
        if call_state == "active":
            adapter._active_call_ws["contact-123"] = ws
        envelope = _delivery_failed_envelope(conversation_id=conversation_id)
        response = await adapter._on_text_lifecycle(envelope)
        assert response.status == 200
        event = adapter._enqueued[0]
        original_target = envelope["data"]["text_message"]["remote_phone_number"]

        # A later incoming channel/conversation must not change the captured
        # callback destination while the native worker is waiting to run.
        adapter._last_inbound_modality["contact-123"] = "email"
        if call_state == "closed":
            adapter._voice_recently_closed["contact-123"] = time.time()
            adapter._last_inbound_modality.clear()
        elif call_state == "hosted":
            adapter._hosted_post_call_active_chats = {"contact-123": 1}
        adapter._last_inbound_sms["contact-123"] = {
            "conversation_id": "other-conversation", "remote_phone_number": "+15555550999",
        }
        adapter._background_tasks = set()

        async def handle_message(queued):
            result = await adapter.send(
                str(queued.source.chat_id), "Safe SMS retry", reply_to=queued.message_id,
                metadata={"thread_id": queued.source.thread_id},
            )
            assert result.success

        adapter.handle_message = handle_message
        # Exercise the production ContextVar handoff, not a test-only explicit
        # mode passed directly to send(). No model task or prompt is replaced.
        task = await InkboxAdapter._enqueue(adapter, event)
        await task
        expected = {"conversation_id": conversation_id} if conversation_id else {"to": original_target}
        assert identity.sent_texts == [{**expected, "text": "Safe SMS retry"}]
        assert identity.sent_emails == []
        ws.send_str.assert_not_awaited()
        if call_state == "active":
            # The recovery route is task-local: ordinary live-call output still
            # uses its native socket after the SMS recovery finishes.
            result = await adapter.send("contact-123", "Legitimate voice reply")
            assert result.success
            assert ws.send_str.await_count == 2
            assert json.loads(ws.send_str.await_args_list[0].args[0])["delta"] == "Legitimate voice reply"
        elif call_state in {"closed", "hosted"}:
            result = await adapter.send("contact-123", "Unbound voice reflection")
            assert result.success
            assert result.message_id in {"suppressed-post-call-leak", "suppressed-hosted-post-call-text"}
            assert len(identity.sent_texts) == 1

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["imessage", "email"])
@pytest.mark.parametrize("active_call", [False, True])
def test_shared_missing_context_recovery_pins_channel_and_target(mode, active_call):
    async def run():
        identity = FakeIdentity()
        adapter = _adapter(identity)
        ws = AsyncMock()
        if active_call:
            adapter._active_call_ws["contact-123"] = ws
        adapter._last_inbound_email["contact-123"] = {
            "from_address": "original@example.test", "stored_message_id": "original-mail",
            "rfc_message_id": "original-rfc", "subject": "Original subject",
        }
        await adapter._note_outbound_delivery_failure(
            mode=mode, chat_id="contact-123", thread_id=None, conversation_id=None,
            target="original@example.test" if mode == "email" else "+15555550101",
            failed_body="Synthetic original", error_code="40002", error_detail="Temporary rejection",
            stage="delivery_failed",
        )
        event = adapter._enqueued[0]
        adapter._last_inbound_modality["contact-123"] = "sms"
        adapter._last_inbound_imessage["contact-123"] = {
            "conversation_id": "other-conversation", "remote_number": "+15555550999",
        }
        adapter._last_inbound_email["contact-123"] = {
            "from_address": "other@example.test", "stored_message_id": "other-mail",
        }
        adapter._background_tasks = set()

        async def handle_message(queued):
            result = await adapter.send("contact-123", "Safe recovery", reply_to=queued.message_id)
            assert result.success

        adapter.handle_message = handle_message
        task = await InkboxAdapter._enqueue(adapter, event)
        await task
        if mode == "imessage":
            assert identity.sent_imessages == [{"to": "+15555550101", "text": "Safe recovery"}]
            assert identity.sent_emails == []
        else:
            assert identity.sent_emails == [{"reply_to_message_id": "original-mail", "body_text": "Safe recovery"}]
            assert identity.sent_imessages == []
        assert identity.sent_texts == []
        ws.send_str.assert_not_awaited()

    asyncio.run(run())


def test_callback_only_email_recovery_uses_authoritative_stored_message():
    async def run():
        identity = FakeIdentity()
        adapter = _adapter(identity, contact={"id": "contact-123", "name": "Kim"})
        adapter._active_call_ws["contact-123"] = AsyncMock()
        response = await adapter._on_mail_delivery_failure({
            "id": "bounce-event", "event_type": "message.bounced",
            "data": {"message": {
                "id": "failed-stored-mail", "message_id": "<failed-rfc@example.test>",
                "to_addresses": ["original@example.test"], "direction": "outbound",
                "thread_id": "original-mail-thread", "status": "bounced", "subject": "Original",
            }},
        })
        assert response.status == 200
        event = adapter._enqueued[0]
        adapter._last_inbound_email["contact-123"] = {
            "from_address": "other@example.test", "stored_message_id": "other-mail",
        }
        adapter._background_tasks = set()

        async def handle_message(queued):
            result = await adapter.send("contact-123", "Safe retry", reply_to=queued.message_id)
            assert result.success

        adapter.handle_message = handle_message
        task = await InkboxAdapter._enqueue(adapter, event)
        await task
        assert identity.sent_emails == [{"reply_to_message_id": "failed-stored-mail", "body_text": "Safe retry"}]
        adapter._active_call_ws["contact-123"].send_str.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("retained_author", [None, "original-author"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_recovery_route_does_not_take_another_turns_group_context(retained_author, outcome):
    async def run():
        adapter = _adapter(FakeIdentity())
        journal = adapter._conversation_state()
        journal.row("contact-123")["active_author"] = "current-author"
        journal.quiet("contact-123", "quiet-input", "Unrelated quiet context")
        journal.consume("contact-123", "other-turn")
        before_quiet = json.loads(json.dumps(journal.row("contact-123")["quiet"]))
        original = {"author": retained_author} if retained_author else None
        await adapter._note_outbound_delivery_failure(
            mode="sms", chat_id="contact-123", thread_id="sms:conv-123", conversation_id="conv-123",
            target="+15555550101", failed_body="Synthetic", error_code="40002",
            error_detail="Temporary rejection", stage="delivery_failed", original_route=original,
        )
        event = adapter._enqueued[0]
        token = adapter_mod.reply_route.set(None)
        try:
            await adapter.on_processing_start(event)
            assert adapter_mod.reply_route.get()["mode"] == "sms"
            assert journal.row("contact-123")["active_author"] == (retained_author or "current-author")
            await adapter.on_processing_complete(event, outcome)
        finally:
            adapter_mod.reply_route.reset(token)
        assert journal.row("contact-123")["quiet"] == before_quiet
        assert journal.row("contact-123")["active_author"] == (retained_author or "current-author")

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["sms", "imessage"])
def test_synchronous_overlength_recovery_captures_original_conversation(mode):
    async def run():
        identity = FakeIdentity()
        adapter = _adapter(identity)
        adapter._active_call_ws["contact-123"] = AsyncMock()
        route = {"chat_id": "contact-123", "mode": mode, "author": "original-author",
                 "thread_id": f"{mode}:original-conversation", "conversation_id": "original-conversation"}
        maximum = adapter_mod.SMS_MAX_LENGTH if mode == "sms" else adapter_mod.IMESSAGE_MAX_LENGTH
        token = adapter_mod.reply_route.set(route)
        try:
            result = await adapter.send("contact-123", "x" * (maximum + 1))
        finally:
            adapter_mod.reply_route.reset(token)
        assert not result.success
        assert identity.sent_texts == identity.sent_imessages == []
        event = adapter._enqueued[0]
        adapter._last_inbound_sms["contact-123"] = {"conversation_id": "new-conversation"}
        adapter._last_inbound_imessage["contact-123"] = {"conversation_id": "new-conversation"}
        adapter._background_tasks = set()

        async def handle_message(queued):
            result = await adapter.send("contact-123", "Short retry", reply_to=queued.message_id)
            assert result.success

        adapter.handle_message = handle_message
        task = await InkboxAdapter._enqueue(adapter, event)
        await task
        sends = identity.sent_texts if mode == "sms" else identity.sent_imessages
        assert sends == [{"conversation_id": "original-conversation", "text": "Short retry"}]
        adapter._active_call_ws["contact-123"].send_str.assert_not_awaited()

    asyncio.run(run())


def test_synchronous_email_rejection_keeps_original_route_over_newer_stash():
    async def run():
        identity = FakeIdentity(email_exc=RuntimeError("Temporary send rejection"))
        adapter = _adapter(identity)
        route = {"chat_id": "contact-123", "mode": "email", "author": "original-author",
                 "to_email": "original@example.test", "stored_message_id": "original-mail",
                 "thread_id": "email:original-thread"}
        adapter._last_inbound_email["contact-123"] = {
            "stored_message_id": "newer-mail", "from_address": "newer@example.test",
        }
        token = adapter_mod.reply_route.set(route)
        try:
            result = await adapter.send("contact-123", "First reply")
        finally:
            adapter_mod.reply_route.reset(token)
        assert not result.success
        identity._email_exc = None
        event = adapter._enqueued[0]
        adapter._active_call_ws["contact-123"] = AsyncMock()
        adapter._background_tasks = set()

        async def handle_message(queued):
            result = await adapter.send("contact-123", "Safe retry", reply_to=queued.message_id)
            assert result.success

        adapter.handle_message = handle_message
        task = await InkboxAdapter._enqueue(adapter, event)
        await task
        assert identity.sent_emails == [{"reply_to_message_id": "original-mail", "body_text": "Safe retry"}]
        assert event.metadata["inkbox_reply_route"]["author"] == "original-author"
        adapter._active_call_ws["contact-123"].send_str.assert_not_awaited()

    asyncio.run(run())


def test_nested_callback_sms_length_rejection_keeps_target_and_budget():
    async def run():
        identity = FakeIdentity()
        adapter = _adapter(identity, contact={"id": "contact-123", "name": "Kim"})
        await adapter._on_text_lifecycle(_delivery_failed_envelope(conversation_id=""))
        first = adapter._enqueued[0]
        target = first.metadata["inkbox_reply_route"]["inkbox_recovery_target"]["target"]
        adapter._last_inbound_sms["contact-123"] = {
            "conversation_id": "newer-conversation", "remote_phone_number": "+15555550999",
        }
        adapter._background_tasks = set()

        async def too_long(event):
            result = await adapter.send("contact-123", "x" * (adapter_mod.SMS_MAX_LENGTH + 1), reply_to=event.message_id)
            assert not result.success

        adapter.handle_message = too_long
        await (await InkboxAdapter._enqueue(adapter, first))
        second = adapter._enqueued[1]
        assert f"attempt=2/{MAX}" in second.text

        async def corrected(event):
            result = await adapter.send("contact-123", "Short retry", reply_to=event.message_id)
            assert result.success

        adapter.handle_message = corrected
        await (await InkboxAdapter._enqueue(adapter, second))
        assert identity.sent_texts == [{"to": target, "text": "Short retry"}]
        assert all("newer-conversation" not in key and "+15555550999" not in key
                   for key in adapter._outbound_failure_state)

    asyncio.run(run())


def test_repeated_callback_email_rejection_keeps_attempted_stored_message():
    async def run():
        identity = FakeIdentity(email_exc=RuntimeError("Synthetic temporary rejection"))
        adapter = _adapter(identity, contact={"id": "contact-123", "name": "Kim"})
        await adapter._on_mail_delivery_failure({
            "event_type": "message.bounced", "data": {"message": {
                "id": "original-stored", "to_addresses": ["original@example.test"],
                "direction": "outbound", "status": "bounced", "subject": "Original subject",
            }},
        })
        first = adapter._enqueued[0]
        adapter._last_inbound_email["contact-123"] = {
            "stored_message_id": "newer-stored", "from_address": "newer@example.test", "subject": "Newer subject",
        }
        adapter._background_tasks = set()

        async def rejected(event):
            result = await adapter.send("contact-123", "First recovery", reply_to=event.message_id)
            assert not result.success

        adapter.handle_message = rejected
        await (await InkboxAdapter._enqueue(adapter, first))
        second = adapter._enqueued[1]
        assert f"attempt=2/{MAX}" in second.text
        assert second.metadata["inkbox_reply_route"]["subject"] == "Re: Original subject"
        identity._email_exc = None

        async def corrected(event):
            result = await adapter.send("contact-123", "Second recovery", reply_to=event.message_id)
            assert result.success

        adapter.handle_message = corrected
        await (await InkboxAdapter._enqueue(adapter, second))
        assert identity.sent_emails == [{"reply_to_message_id": "original-stored", "body_text": "Second recovery"}]

    asyncio.run(run())


def test_rejected_cold_email_never_acquires_a_newer_stored_reply_target():
    async def run():
        identity = FakeIdentity()
        adapter = _adapter(identity)
        attempts = []

        def reject_cold_send(**kwargs):
            attempts.append(kwargs)
            # Another inbound message arrives while the original SDK call is
            # in flight. Its stored ID is not authority for this cold send.
            adapter._last_inbound_email["contact-123"] = {
                "stored_message_id": "unrelated-newer-mail", "from_address": "newer@example.test",
                "subject": "Unrelated newer subject", "rfc_message_id": "unrelated-rfc",
            }
            raise RuntimeError("Synthetic send rejection")

        identity.send_email = reject_cold_send
        result = await adapter.send("contact-123", "Original cold email", metadata={
            "mode": "email", "to_email": "original@example.test", "subject": "Original subject",
        })
        assert not result.success
        assert attempts == [{"to": ["original@example.test"], "subject": "Original subject",
                             "body_text": "Original cold email"}]
        recovery = adapter._enqueued[0]
        route = recovery.metadata["inkbox_reply_route"]
        assert route["inkbox_recovery_target"]["email_context"]["stored_message_id"] is None
        assert route["subject"] == "Original subject"
        adapter._background_tasks = set()

        async def handle_message(queued):
            retry = await adapter.send("contact-123", "Cold email retry", reply_to=queued.message_id)
            assert not retry.success
            assert retry.error == "Email reply requires its stored message ID"

        adapter.handle_message = handle_message
        await (await InkboxAdapter._enqueue(adapter, recovery))
        assert len(attempts) == 1
        assert identity.sent_emails == []

    asyncio.run(run())


def test_terminal_sms_delivery_failure_requires_silent_and_no_resend():
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123", "name": "Kim"})
    envelope = _delivery_failed_envelope()
    message = envelope["data"]["text_message"]
    message["error_code"] = "invalid_phone_number"
    message["error_detail"] = "The destination is invalid or unreachable."

    response = asyncio.run(adapter._on_text_lifecycle(envelope))

    assert response.status == 200
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert "SMS failure classification: DO NOT RETRY" in event.text
    assert "Do not resend this message; reply exactly [SILENT]" in event.text
    assert "RETRY REQUIRED" not in event.text


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


@pytest.mark.parametrize("event_type", ["message.bounced", "message.failed", "text.delivery_failed", "imessage.delivery_failed"])
def test_gateway_mode_off_preserves_delivery_failure_recovery(monkeypatch, event_type):
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123", "name": "Kim"})
    adapter._companion = None
    adapter._require_signature = True
    adapter._signing_key = "synthetic-signing-key"
    provider = types.SimpleNamespace(name="inkbox", verify=Mock(return_value=True))
    monkeypatch.setattr(adapter_mod, "match_provider", lambda _headers: provider)
    failed = {
        "id": "failed-message", "direction": "outbound", "status": "delivery_failed",
        "thread_id": "mail-thread", "conversation_id": "phone-conversation",
        "to_addresses": ["kim@example.com"], "remote_phone_number": "+15555550101", "remote_number": "+15555550101",
        "snippet": "Ordinary reply", "text": "Ordinary reply", "content": "Ordinary reply",
    }
    envelope = {"event_type": event_type, "data": {"text_message" if event_type.startswith("text.") else "message": failed}}
    request = types.SimpleNamespace(read=AsyncMock(return_value=json.dumps(envelope).encode()),
                                    headers={}, url="https://example.com/webhook")
    assert asyncio.run(adapter._handle_webhook(request)).status == 200
    assert len(adapter._enqueued) == 1
    assert adapter._enqueued[0].source.chat_id == "contact-123"
    assert "Ordinary reply" in adapter._enqueued[0].text


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


def test_sms_send_to_webhook_correlation_flow():
    contact = {
        "id": "contact-123",
        "name": "Kim",
        "phones": [types.SimpleNamespace(value="+15555550101", is_primary=True)],
    }
    adapter = _adapter(FakeIdentity(), contact=contact)
    adapter._inkbox.contacts.get = lambda _cid: types.SimpleNamespace(**contact)

    # 1. Send SMS via production path
    result = asyncio.run(adapter.send(
        chat_id="contact-123",
        content="Production path SMS content",
        metadata={"mode": "sms"},
    ))
    assert result.success is True
    msg_id = result.message_id
    assert msg_id == "txt-1"

    # Verify context is populated automatically
    assert msg_id in adapter._outbound_context
    ctx = adapter._outbound_context[msg_id]
    assert ctx["channel"] == "sms"
    assert ctx["chat_id"] == "contact-123"
    assert ctx["body_snippet"] == "Production path SMS content"

    # 2. Receive incomplete webhook failure (direction is omitted, no remote number)
    envelope = {
        "id": "evt-text-1",
        "event_type": "text.delivery_failed",
        "data": {
            "text_message": {
                "id": msg_id,
                "conversation_id": "conv-123",
                "text": "Incomplete webhook text",
                "delivery_status": "delivery_failed",
                "error_code": "40002",
                "error_detail": "Flagged by spam",
            },
        },
    }

    response = asyncio.run(adapter._on_text_lifecycle(envelope))
    assert response.status == 200

    # Verify the agent was woken in the correct contact session with the original text snippet
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert event.source.chat_id == "contact-123"
    assert "Production path SMS content" in event.text
    assert "Incomplete webhook text" not in event.text

    # Verify terminal cleanup: context is removed after failure
    assert msg_id not in adapter._outbound_context


def test_outbound_context_correlation_unresolved_no_wake():
    adapter = _adapter(FakeIdentity(), contact=None)
    # The webhook fails to resolve to any contact (contact=None), and no context exists.
    # Therefore, no usable thread/session can be resolved.
    envelope = _delivery_failed_envelope(text_id="txt-unknown", conversation_id="")
    envelope["data"]["text_message"]["remote_phone_number"] = ""
    envelope["data"]["text_message"]["recipients"] = []

    response = asyncio.run(adapter._on_text_lifecycle(envelope))
    assert response.status == 200
    assert adapter._enqueued == []  # Not woken


def test_imessage_send_to_webhook_correlation_flow():
    contact = {
        "id": "contact-123",
        "name": "Kim",
        "phones": [types.SimpleNamespace(value="+15555550101", is_primary=True)],
    }
    adapter = _adapter(FakeIdentity(), contact=contact)
    adapter._inkbox.contacts.get = lambda _cid: types.SimpleNamespace(**contact)

    result = asyncio.run(adapter.send(
        chat_id="contact-123",
        content="Production path iMessage content",
        metadata={"mode": "imessage"},
    ))
    assert result.success is True
    msg_id = result.message_id
    assert msg_id == "txt-1"

    # 2. Receive incomplete webhook failure
    envelope = {
        "id": "evt-imsg-1",
        "event_type": "imessage.delivery_failed",
        "data": {
            "message": {
                "id": msg_id,
                "direction": "outbound",
                "content": "Webhook content",
                "status": "delivery_failed",
                "error_code": "OPTED_OUT",
            },
        },
    }

    response = asyncio.run(adapter._on_imessage_lifecycle(envelope))
    assert response.status == 200
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert event.source.chat_id == "contact-123"
    assert "Production path iMessage content" in event.text
    assert msg_id not in adapter._outbound_context


def test_imessage_media_send_to_webhook_correlation_flow():
    contact = {
        "id": "contact-123",
        "name": "Kim",
        "phones": [types.SimpleNamespace(value="+15555550101", is_primary=True)],
    }
    adapter = _adapter(FakeIdentity(), contact=contact)
    adapter._inkbox.contacts.get = lambda _cid: types.SimpleNamespace(**contact)

    # send_image routes to _send_imessage_media when mode is imessage and it's a media route
    result = asyncio.run(adapter.send_image(
        chat_id="contact-123",
        image_url="https://example.com/chart.png",
        caption="Production path iMessage media caption",
        metadata={"mode": "imessage"},
    ))
    assert result.success is True
    msg_id = result.message_id
    assert msg_id == "txt-1"

    # 2. Receive incomplete webhook failure
    envelope = {
        "id": "evt-imsg-media",
        "event_type": "imessage.delivery_failed",
        "data": {
            "message": {
                "id": msg_id,
                "direction": "outbound",
                "status": "delivery_failed",
                "error_code": "OPTED_OUT",
            },
        },
    }

    response = asyncio.run(adapter._on_imessage_lifecycle(envelope))
    assert response.status == 200
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert event.source.chat_id == "contact-123"
    assert "Production path iMessage media caption" in event.text
    assert msg_id not in adapter._outbound_context


def test_email_send_to_webhook_correlation_flow():
    contact = {
        "id": "contact-123",
        "name": "Kim",
        "emails": [types.SimpleNamespace(value="kim@example.com", is_primary=True)],
    }
    adapter = _adapter(FakeIdentity(), contact=contact)
    adapter._inkbox.contacts.get = lambda _cid: types.SimpleNamespace(**contact)

    result = asyncio.run(adapter.send(
        chat_id="contact-123",
        content="Production path Email content",
        metadata={
            "mode": "email",
            "thread_id": "inkbox-thread-uuid-1",
            "stored_message_id": "stored-mail-1",
        },
        reply_to="rfc-msg-id-1",
    ))
    assert result.success is True
    msg_id = result.message_id
    assert msg_id == "mail-1"

    # Verify thread_id and rfc_message_id are stored separately in context
    assert msg_id in adapter._outbound_context
    ctx = adapter._outbound_context[msg_id]
    assert ctx["email_thread_id"] == "inkbox-thread-uuid-1"
    assert ctx["email_rfc_message_id"] == "rfc-msg-id-1"
    assert ctx["email_subject"] == "(no subject)"

    # 2. Receive incomplete webhook failure (missing recipient to verify context lookup bypass)
    envelope = {
        "id": "evt-mail-bounce-1",
        "event_type": "message.bounced",
        "data": {
            "message": {
                "id": msg_id,
                "thread_id": "wrong-thread",
                "message_id": "<wrong-rfc@inkboxmail.com>",
                "from_address": "agent@inkboxmail.com",
                "to_addresses": [],  # missing recipient!
                "subject": "Different Subject",
                "direction": "outbound",
                "status": "bounced",
            },
        },
    }

    response = asyncio.run(adapter._on_mail_delivery_failure(envelope))
    assert response.status == 200
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert event.source.chat_id == "contact-123"
    assert event.source.thread_id == "email:inkbox-thread-uuid-1"  # routes to original thread_id
    assert "Production path Email content" in event.text

    # Verify thread-specific routing state was updated correctly
    assert adapter._last_inbound_email["contact-123"]["rfc_message_id"] == "rfc-msg-id-1"
    assert adapter._last_inbound_email["contact-123"]["subject"] == "(no subject)"

    # Verify terminal cleanup
    assert msg_id not in adapter._outbound_context


def test_inline_failure_counts_once_without_retry_wakeup(monkeypatch, tmp_path):
    from inkbox_plugin import send_outcome
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(FakeIdentity(), contact={"id": "contact-123", "name": "Kim"})
    envelope = _delivery_failed_envelope()
    send_outcome._mark(envelope["data"]["text_message"]["id"], "inline")
    asyncio.run(adapter._on_text_lifecycle(envelope))
    asyncio.run(adapter._on_text_lifecycle(envelope))
    assert not adapter._enqueued
    assert max(row["attempts"] for row in adapter._outbound_failure_state.values()) == 1
