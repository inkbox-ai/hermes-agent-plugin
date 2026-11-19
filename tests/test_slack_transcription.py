"""Slack voice notes use the shared bounded wait, single read, fallback, and transcript label."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from inkbox_plugin import transcription as transcription_module
from inkbox_plugin import voice_note_ingress as ingress_module
from inkbox_plugin.transcription import (
    VoiceNoteGate, completion_channel, message_of, poll_message, voice_note_context, with_message,
)
from inkbox_plugin.voice_note_ingress import VoiceNoteIngress
from tests import test_companion as base_harness
from tests.test_channel_ingress import slack_event, slack_host
from tests.test_companion import uid
from tests.test_native_turns import settle, started
from tests.test_voice_note_ingress import Request, close, receiver

factory = base_harness.factory
MESSAGE_ID = "00000000-0000-4000-8000-000000000007"
SPEECH = "Move the review to Thursday."


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


def voice_event(status="pending", *, text=None, error_code=None, message_id=MESSAGE_ID):
    event = slack_event()
    if message_id is not None:
        event["data"]["message_id"] = message_id
    event["data"]["event"]["files"] = [{
        "id": "FVOICE1", "name": "voice.m4a", "mimetype": "audio/mp4", "size": 2048,
        "transcription": {"status": status, "text": text, "error_code": error_code},
    }]
    return event


def completion(status="completed", *, text=SPEECH, error_code=None):
    event = voice_event(status, text=text, error_code=error_code)
    event["id"] = "evt_slack_completion"
    event["event_type"] = "message.transcription_finished"
    event["data"]["channel"] = "slack"
    return event


def archived(status="completed", *, text="Polled speech"):
    return {"messages": [{
        "id": MESSAGE_ID, "connection_id": uid(40), "conversation_id": "C123", "text": "<@UBOT> question 1",
        "files": [{"id": "FVOICE1", "name": "voice.m4a", "transcription": {"status": status, "text": text, "error_code": None}}],
    }], "next_cursor": None}


def slack_receiver(tmp_path, monkeypatch, *, wait_seconds=None, read=None):
    adapter, ingress, provider, rows, delivered = receiver(tmp_path, monkeypatch)
    adapter._slack_enabled = True
    if wait_seconds is not None:
        adapter._voice_notes = VoiceNoteGate(wait_seconds=wait_seconds)
    if read is not None:
        adapter._inkbox = SimpleNamespace(_api_http=SimpleNamespace(get=read))
    return adapter, ingress, provider, rows, delivered


def files(envelope):
    return envelope["data"]["event"]["files"]


def test_slack_completion_matches_its_original_by_retained_message_id():
    original = message_of(voice_event(), "slack")
    assert original["id"] == MESSAGE_ID and original["connection_id"] == uid(40)
    assert original["media"][0]["transcription"]["status"] == "pending"
    assert completion_channel(completion()) == "slack"
    finished = message_of(completion(), "slack")
    assert VoiceNoteIngress.key("slack", original["id"]) == VoiceNoteIngress.key("slack", finished["id"])
    unrelated = completion()
    unrelated["data"]["message_id"] = None
    assert completion_channel(unrelated) is None
    resolved = with_message(voice_event(), "slack", finished)
    assert files(resolved)[0]["transcription"]["text"] == SPEECH
    assert resolved["data"]["event"]["text"] == voice_event()["data"]["event"]["text"]
    assert files(voice_event())[0]["transcription"]["status"] == "pending"


def test_slack_original_waits_for_completion_and_wakes_once(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, _, rows, delivered = slack_receiver(tmp_path, monkeypatch)
        read = AsyncMock()
        monkeypatch.setattr(ingress_module, "poll_message", read)
        ingress.start()
        assert (await asyncio.wait_for(adapter._handle_webhook(Request(voice_event())), 1)).status == 202
        assert not rows
        assert ingress.db.execute("SELECT state FROM deliveries").fetchone()[0] == "pending"
        assert (await adapter._handle_webhook(Request(completion()))).status == 202
        await asyncio.wait_for(delivered.wait(), 1)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "slack.mention_received"
        assert rows[0]["data"]["event"]["text"] == "<@UBOT> question 1"
        assert files(rows[0])[0]["name"] == "voice.m4a"
        assert files(rows[0])[0]["transcription"] == {"status": "completed", "text": SPEECH, "error_code": None}
        assert ingress.db.execute("SELECT state FROM deliveries").fetchone()[0] == "completed"
        # A redelivered original or a repeated completion never wakes the agent again.
        assert (await adapter._handle_webhook(Request(voice_event()))).status == 202
        assert (await adapter._handle_webhook(Request(completion()))).status == 202
        await asyncio.sleep(0)
        assert len(rows) == 1
        read.assert_not_called()
        await close(adapter, ingress)
    asyncio.run(scenario())


def test_slack_completion_before_the_original_needs_no_read(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, _, rows, delivered = slack_receiver(tmp_path, monkeypatch)
        read = AsyncMock()
        monkeypatch.setattr(ingress_module, "poll_message", read)
        ingress.start()
        await adapter._handle_webhook(Request(completion()))
        assert not rows and not ingress.tasks
        await adapter._handle_webhook(Request(voice_event()))
        await asyncio.wait_for(delivered.wait(), 1)
        assert len(rows) == 1
        assert files(rows[0])[0]["transcription"]["text"] == SPEECH
        read.assert_not_called()
        await close(adapter, ingress)
    asyncio.run(scenario())


def test_slack_timeout_reads_the_retained_message_once(tmp_path, monkeypatch):
    async def scenario():
        read = Mock(return_value=archived())
        adapter, ingress, _, rows, delivered = slack_receiver(tmp_path, monkeypatch, wait_seconds=0, read=read)
        ingress.start()
        await adapter._handle_webhook(Request(voice_event()))
        await asyncio.wait_for(delivered.wait(), 1)
        read.assert_called_once_with(f"/slack/connections/{uid(40)}/archive/messages",
                                     params={"message_id": MESSAGE_ID}, timeout=8)
        assert len(rows) == 1
        assert files(rows[0])[0]["name"] == "voice.m4a"
        assert files(rows[0])[0]["transcription"]["text"] == "Polled speech"
        await adapter._handle_webhook(Request(completion()))
        await asyncio.sleep(0)
        assert len(rows) == 1
        read.assert_called_once()
        await close(adapter, ingress)
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["failed_completion", "failed_read", "missing_message"])
def test_slack_failure_or_unavailable_transcript_delivers_the_original(tmp_path, monkeypatch, outcome):
    async def scenario():
        read = Mock(side_effect=OSError("read failed")) if outcome == "failed_read" else Mock(
            return_value={"messages": [], "next_cursor": None})
        wait = None if outcome == "failed_completion" else 0
        adapter, ingress, _, rows, delivered = slack_receiver(tmp_path, monkeypatch, wait_seconds=wait, read=read)
        ingress.start()
        await adapter._handle_webhook(Request(voice_event()))
        if outcome == "failed_completion":
            await adapter._handle_webhook(Request(completion("failed", text=None, error_code="transcription_failed")))
        await asyncio.wait_for(delivered.wait(), 1)
        assert len(rows) == 1
        assert rows[0]["data"]["event"]["text"] == "<@UBOT> question 1"
        assert files(rows[0])[0]["name"] == "voice.m4a"
        expected = "failed" if outcome == "failed_completion" else "pending"
        assert files(rows[0])[0]["transcription"]["status"] == expected
        assert read.call_count == (0 if outcome == "failed_completion" else 1)
        context = voice_note_context(message_of(rows[0], "slack"))
        assert context == "[Voice note 1: transcript unavailable; process the original attachment as usual]"
        await close(adapter, ingress)
    asyncio.run(scenario())


def test_slack_poll_reads_one_retained_message_by_id():
    async def scenario():
        get = Mock(return_value=archived())
        adapter = SimpleNamespace(_inkbox=SimpleNamespace(_api_http=SimpleNamespace(get=get)))
        result = await poll_message(adapter, "slack", message_of(voice_event(), "slack"))
        get.assert_called_once_with(f"/slack/connections/{uid(40)}/archive/messages",
                                    params={"message_id": MESSAGE_ID}, timeout=8)
        assert result["id"] == MESSAGE_ID and result["conversation_id"] == "C123"
        assert result["media"][0]["transcription"]["text"] == "Polled speech"
        get.return_value = {"messages": [{**archived()["messages"][0], "id": uid(9)}]}
        with pytest.raises(LookupError):
            await poll_message(adapter, "slack", message_of(voice_event(), "slack"))
    asyncio.run(scenario())


def test_slack_events_without_pending_audio_or_message_id_are_not_held(tmp_path, monkeypatch):
    async def scenario():
        gate = VoiceNoteGate()
        read = AsyncMock()
        for event in (voice_event("completed", text=SPEECH), voice_event(message_id=None), slack_event()):
            item = message_of(event, "slack")
            assert await asyncio.wait_for(gate.resolve("slack", item, read), 0.1) is item
        assert gate.entries == {}
        read.assert_not_called()
        await gate.close()
        adapter, ingress, _, rows, delivered = slack_receiver(tmp_path, monkeypatch)
        ingress.start()
        await adapter._handle_webhook(Request(voice_event(message_id=None)))
        await asyncio.wait_for(delivered.wait(), 1)
        assert len(rows) == 1
        assert ingress.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
        await close(adapter, ingress)
    asyncio.run(scenario())


def test_slack_completion_route_does_not_dispatch_an_incoming_message():
    from inkbox_plugin.adapter import InkboxAdapter

    async def scenario():
        adapter = object.__new__(InkboxAdapter)
        adapter._on_slack_event = AsyncMock()
        response = await adapter._route_inkbox_event("message.transcription_finished", completion())
        assert response.status == 200
        adapter._on_slack_event.assert_not_called()
        assert adapter._voice_notes.entries[("slack", MESSAGE_ID)]["result"]["media"][0]["transcription"]["text"] == SPEECH
        await adapter._voice_notes.close()
    asyncio.run(scenario())


def test_slack_transcript_is_labelled_separately_from_the_typed_message(factory, tmp_path):
    async def run():
        value = await slack_host(factory, tmp_path)
        response = await value.adapter._on_slack_event(voice_event("completed", text=SPEECH))
        assert response.status == 202
        await started(value)
        text = value.inputs[0].text
        label = "\n[Voice note 1 — automatically transcribed]\n" + SPEECH
        assert "<@UBOT> question 1\nAttachment references (not downloaded): " in text
        assert label in text
        assert text.index("Attachment references") < text.index(label)
        assert text.count("[Voice note") == 1
        await settle(value.queue)
        await value.queue.close()
        await value.adapter._slack_activity.close()
        await value.adapter._voice_note_receiver().close()
    asyncio.run(run())


def test_slack_handler_uses_a_retained_completion_and_wakes_once(factory, tmp_path, monkeypatch):
    async def run():
        value = await slack_host(factory, tmp_path)
        read = AsyncMock(side_effect=AssertionError("completion was already retained"))
        monkeypatch.setattr(transcription_module, "poll_message", read)
        value.adapter._voice_note_receiver().complete("slack", message_of(completion(), "slack"))
        response = await asyncio.wait_for(value.adapter._on_slack_event(voice_event()), 2)
        assert response.status == 202
        await started(value)
        assert "[Voice note 1 — automatically transcribed]\n" + SPEECH in value.inputs[0].text
        assert (await value.adapter._on_slack_event(voice_event())).status == 200
        await settle(value.queue)
        assert len(value.inputs) == 1
        read.assert_not_called()
        await value.queue.close()
        await value.adapter._slack_activity.close()
        await value.adapter._voice_note_receiver().close()
    asyncio.run(run())


def test_slack_diagnostics_identify_unconfirmed_originals(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, _, _, _ = slack_receiver(tmp_path, monkeypatch)
        path = ingress_module.inbox_path(tmp_path, adapter._base_url, adapter._identity_id)
        ingress.accept(voice_event())
        ingress.db.execute("UPDATE deliveries SET state='unknown'")
        assert ingress_module.unconfirmed_ids(path) == [f"slack:{MESSAGE_ID}"]
        await close(adapter, ingress)
    asyncio.run(scenario())


def test_slack_original_redelivered_under_another_category_keeps_one_receipt(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, _, rows, delivered = slack_receiver(tmp_path, monkeypatch)
        ingress.start()
        channel = voice_event()
        channel["event_type"] = "slack.channel_message_received"
        assert (await adapter._handle_webhook(Request(channel))).status == 202
        # The same message may come back as a mention; it is a duplicate, not a conflict.
        assert (await adapter._handle_webhook(Request(voice_event()))).status == 202
        assert ingress.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
        await adapter._handle_webhook(Request(completion()))
        await asyncio.wait_for(delivered.wait(), 1)
        await asyncio.sleep(0)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "slack.channel_message_received"
        assert files(rows[0])[0]["transcription"]["text"] == SPEECH
        await close(adapter, ingress)
    asyncio.run(scenario())


def test_slack_transient_connection_read_is_retryable_before_admission(factory, tmp_path):
    async def run():
        value = await slack_host(factory, tmp_path)
        connections = value.adapter._inkbox.slack.list_connections
        connections.side_effect = [ConnectionError("reset"), connections.return_value]
        refused = await value.adapter._on_slack_event(voice_event("completed", text=SPEECH))
        assert refused.status == 503 and refused.get("native_receiver_starting")
        assert not value.inputs and not value.queue.rows
        assert (await value.adapter._on_slack_event(voice_event("completed", text=SPEECH))).status == 202
        await started(value)
        assert len(value.inputs) == 1
        await settle(value.queue)
        connections.side_effect = PermissionError("denied")
        with pytest.raises(PermissionError):
            await value.adapter._on_slack_event(voice_event("completed", text=SPEECH))
        await value.queue.close()
        await value.adapter._slack_activity.close()
        await value.adapter._voice_note_receiver().close()
    asyncio.run(run())
