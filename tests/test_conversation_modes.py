"""Current-message wake gates, durable quiet context, and original reply routing."""
import asyncio
import types

import pytest

from tests import test_companion as harness
from tests.test_companion import event, idle, uid

factory = harness.factory
from inkbox_plugin.conversation import ConversationState, mentions, wakes
from inkbox_plugin.companion import message
from inkbox_plugin.adapter import InkboxAdapter
from gateway.platforms.base import MessageEvent, MessageType


@pytest.mark.parametrize("text,expected", [
    ("hey @AGENT!", True), ("@sample-agent, hi", True), ("hello agent", False),
    ("user@agent.com", False), ("https://example.com/@agent", False),
    ("@agents", False), ("@sample-agent-extra", False), ("(@agent)", True),
])
def test_mentions_are_whole_tokens(text, expected):
    assert mentions(text, "sample-agent") is expected


@pytest.mark.parametrize("channel", ["mail", "phone", "imessage"])
@pytest.mark.parametrize("access", ["direct", "sponsored", None, "future"])
@pytest.mark.parametrize("response", ["safe", "relaxed"])
@pytest.mark.parametrize("addressed", [False, True])
def test_companion_independent_current_message_gates(factory, channel, access, response, addressed):
    async def run():
        instance = factory(channel)
        instance.adapter.config.extra.update(group_reply_mode="mention", companion_response_mode=response)
        receipt = event(channel)
        item = message(receipt)
        item.update(sender_access=access, body_text="hi @agent" if addressed else "Hello everyone")
        await instance.receiver.accept(receipt)
        await idle(instance)
        expected = addressed and (response == "relaxed" or access == "direct")
        assert len(instance.inputs) == int(expected)
        if not expected:
            row = next(iter(instance.receiver.rows.values()))
            assert row["turns"][0]["state"] == "context_only"
            instance.identity.reply_all_email.assert_not_called()
            assert instance.adapter._active_sessions == {}
        await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize("recipients,cc,access,expected", [
    (["Agent <AGENT@example.com>"], [], "direct", True),
    (["someone@example.com"], ["agent@example.com"], "direct", False),
    (["agent@example.com"], [], "sponsored", False),
    ([], [], "direct", False),
])
def test_current_email_to_is_addressing_not_authority(factory, recipients, cc, access, expected):
    instance = factory()
    instance.adapter.config.extra["group_reply_mode"] = "mention"
    instance.adapter._identity_email_addresses = {"agent@example.com"}
    assert wakes(instance.adapter, "mail", {"sender_access": access, "to_addresses": recipients,
                                          "cc_addresses": cc, "body_text": "Hello"}) is expected


def test_quiet_live_first_restores_context_without_historical_wake(factory):
    async def run():
        first = factory()
        first.adapter.config.extra["group_reply_mode"] = "mention"
        incoming = event(phase="live", number=4)
        message(incoming).update(sender_access="sponsored", body_text="@agent sponsor said yes earlier")
        await first.receiver.accept(incoming)
        await idle(first)
        assert not first.inputs
        await first.receiver.close()
        second = factory(root=first.receiver.root)
        second.adapter.config.extra["group_reply_mode"] = "mention"
        await second.receiver.start()
        later = event(phase="live", number=5)
        message(later).update(body_text="@agent summarize", sender_access="direct")
        await second.receiver.accept(later)
        await idle(second)
        assert len(second.inputs) == 1
        assert "sponsor said yes earlier" in second.inputs[0].text
        assert "Hello group" in second.inputs[0].text
        second.resource.load_initialization.assert_not_called()
        second.resource.activation_messages.assert_not_called()
        await second.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["initialization", "live"])
@pytest.mark.parametrize("author,valid", [("Owner@Example.COM", True), ("other@example.com", False)])
def test_snapshot_receipt_author_matches_case_insensitively(factory, phase, author, valid):
    async def run():
        instance = factory()
        incoming = event(phase=phase)
        message(incoming)["from_address"] = author
        await instance.receiver.accept(incoming)
        await idle(instance)
        if valid:
            # A live receipt that is also the snapshot trigger is current, not historical.
            assert next(iter(instance.receiver.rows.values()))["state"] != "failed"
        else:
            assert not instance.inputs
            assert "author" in next(iter(instance.receiver.rows.values()))["error"]
        await instance.receiver.close()
    asyncio.run(run())


def ordinary_event(number, author, text):
    item = {"id": str(number), "conversation_id": "group-1", "sender_phone_number": author, "text": text}
    return MessageEvent(text="Group data: " + text, message_type=MessageType.TEXT,
                        source=types.SimpleNamespace(chat_id="sms:group-1", chat_type="group", thread_id="sms:group-1",
                                                     user_id=author, user_id_alt=author),
                        message_id=str(number), raw_message={"event_type": "text.received", "data": {"text_message": item}})


def ordinary_adapter(tmp_path):
    adapter = object.__new__(InkboxAdapter)
    adapter.config = types.SimpleNamespace(extra={"group_reply_mode": "mention"})
    adapter._identity_handle = "sample-agent"
    adapter._conversation_journal = ConversationState(tmp_path)
    adapter._pending_conversation_control = lambda source: False
    adapter._background_tasks = set()
    return adapter


def test_ordinary_quiet_context_survives_restart_and_cannot_change_active_route(tmp_path):
    first = ordinary_adapter(tmp_path)
    quiet = ordinary_event(1, "+15555550101", "Dinner at eight")
    assert not first._prepare_conversation_event(quiet)
    second = ordinary_adapter(tmp_path)
    wake = ordinary_event(2, "+15555550102", "@agent what time?")
    assert second._prepare_conversation_event(wake)
    assert "Dinner at eight" in wake.text
    assert wake.metadata["inkbox_reply_route"]["author"] == "+15555550102"
    assert wake.metadata["inkbox_reply_route"]["conversation_id"] == "group-1"
    late = ordinary_event(3, "+15555550101", "Later context")
    assert not second._prepare_conversation_event(late)
    assert wake.metadata["inkbox_reply_route"]["message_id"] == "2"
    second._conversation_journal.complete("sms:group-1", "2")
    assert [item["id"] for item in second._conversation_journal.row("sms:group-1")["quiet"]] == ["3"]


def test_ordinary_controls_and_only_asked_sender_bypass_mentions(tmp_path):
    adapter = ordinary_adapter(tmp_path)
    adapter._pending_conversation_control = lambda source: True
    adapter._conversation_journal.row("sms:group-1")["active_author"] = "+15555550101"
    other = ordinary_event(1, "+15555550102", "allow")
    assert not adapter._prepare_conversation_event(other)
    asked = ordinary_event(2, "+15555550101", "allow")
    assert adapter._prepare_conversation_event(asked)
    assert asked.text == "allow"
    adapter._pending_conversation_control = lambda source: False
    stop = ordinary_event(3, "+15555550102", "/stop")
    assert adapter._prepare_conversation_event(stop)
    assert stop.text == "/stop"


def test_submission_startup_failure_is_retryable_without_new_session(factory):
    async def run():
        instance = factory()
        instance.receiver.retry_delay = 0.01
        enqueue = instance.adapter._enqueue
        calls = []
        async def fail_once(incoming):
            calls.append(incoming.source.chat_id)
            if len(calls) == 1:
                raise ConnectionError("Host unavailable before submission")
            return await enqueue(incoming)
        instance.adapter._enqueue = fail_once
        await instance.receiver.accept(event())
        await idle(instance)
        assert len(instance.inputs) == 1
        assert len(calls) == 2 and calls[0] == calls[1]
        assert instance.resource.load_initialization.call_count == 1
        await instance.receiver.close()
    asyncio.run(run())


def test_saved_model_result_is_sent_after_restart_without_rerunning(factory):
    async def run():
        first = factory()
        await first.receiver.accept(event())
        await idle(first)
        row = next(iter(first.receiver.rows.values()))
        turn = row["turns"][0]
        turn.update(state="submitted", result_ready=True, result="Saved model output")
        row["state"] = "ready"
        first.receiver._save(row)
        await first.receiver.close()
        second = factory(root=first.receiver.root)
        await second.receiver.start()
        await idle(second)
        assert not second.inputs
        second.identity.reply_all_email.assert_called_once_with(uid(3), body_text="Saved model output")
        assert next(iter(second.receiver.rows.values()))["turns"][0]["state"] == "completed"
        await second.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize("author,access,text,accepted", [
    ("OWNER@EXAMPLE.COM", "direct", "@agent allow", True),
    ("owner@example.com", "direct", "allow", False),
    ("owner@example.com", "sponsored", "@agent allow", False),
    ("other@example.com", "direct", "@agent allow", False),
    ("owner@example.com", None, "@agent /stop", False),
])
def test_companion_approval_requires_current_gates_and_prompted_author(factory, author, access, text, accepted):
    async def run():
        instance = factory()
        instance.adapter.config.extra["group_reply_mode"] = "mention"
        instance.gate.clear()
        start = event()
        message(start)["body_text"] = "@agent start"
        await instance.receiver.accept(start)
        await harness.wait_inputs(instance, 1)
        instance.adapter._pending_conversation_control = lambda source: True
        received = []
        async def enqueue(incoming):
            received.append(incoming)
            return asyncio.create_task(asyncio.sleep(0))
        instance.adapter._enqueue = enqueue
        answer = event(phase="live", number=4)
        message(answer).update(from_address=author, sender_access=access, body_text=text)
        await instance.receiver.accept(answer)
        assert len(received) == int(accepted)
        if accepted:
            assert received[0].text == "allow"
            assert received[0].allow_gateway_control is True
            assert received[0].message_id == instance.inputs[0].message_id
        await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize("chat_type", ["dm", "group"])
def test_ordinary_email_reply_all_is_not_mention_gated(tmp_path, chat_type):
    adapter = ordinary_adapter(tmp_path)
    incoming = ordinary_event(1, "sender@example.com", "An ordinary email without a mention")
    incoming.source.chat_type = chat_type
    incoming.raw_message = {"event_type": "message.received", "data": {"message": {
        "id": "stored-message", "from_address": "sender@example.com", "body_text": "Hello",
        "to_addresses": ["other@example.com"], "cc_addresses": ["agent@example.com"],
    }}}
    assert adapter._prepare_conversation_event(incoming)
    assert incoming.metadata["inkbox_reply_route"]["stored_message_id"] == "stored-message"


def test_failed_ordinary_wake_releases_buffered_context(tmp_path):
    adapter = ordinary_adapter(tmp_path)
    assert not adapter._prepare_conversation_event(ordinary_event(1, "+15555550101", "Useful earlier context"))
    first = ordinary_event(2, "+15555550102", "@agent summarize")
    assert adapter._prepare_conversation_event(first)
    # This is the durable reservation released by a failed processing outcome.
    adapter._conversation_journal.release(first.source.chat_id, first.message_id)
    second = ordinary_event(3, "+15555550102", "@agent try again")
    assert adapter._prepare_conversation_event(second)
    assert "Useful earlier context" in second.text


def test_control_ack_cannot_checkpoint_or_complete_running_model(factory):
    async def run():
        instance = factory()
        instance.gate.clear()
        await instance.receiver.accept(event())
        await harness.wait_inputs(instance, 1)
        model_event = instance.inputs[0]
        row = next(iter(instance.receiver.rows.values()))
        turn = row["turns"][0]
        control = MessageEvent(text="allow", message_type=MessageType.TEXT, source=model_event.source,
                               message_id=model_event.message_id, raw_message={
                                   **model_event.raw_message, "_inkbox_companion_control": True,
                               })
        instance.receiver.capture_result(control, "Approval recorded")
        assert instance.receiver.processing(control, "success")
        assert not turn.get("result_ready")
        assert turn["state"] == "submitted"
        assert not instance.receiver.completions[turn["id"]].done()
        instance.gate.set()
        await idle(instance)
        await instance.receiver.close()
    asyncio.run(run())


def test_saved_result_is_not_confused_with_prior_control_ack(factory):
    async def run():
        from inkbox_plugin.companion import digest
        first = factory()
        await first.receiver.accept(event())
        await idle(first)
        row = next(iter(first.receiver.rows.values()))
        turn = row["turns"][0]
        turn.update(state="submitted", result_ready=True, result="The actual answer",
                    delivery={"state": "sent", "fingerprint": digest("Approval recorded"), "message_id": "prior-ack"})
        row["state"] = "ready"
        first.receiver._save(row)
        await first.receiver.close()
        second = factory(root=first.receiver.root)
        await second.receiver.start()
        await idle(second)
        assert not second.inputs
        second.identity.reply_all_email.assert_called_once_with(uid(3), body_text="The actual answer")
        await second.receiver.close()
    asyncio.run(run())
