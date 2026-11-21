"""Companion Slack turns resolve voice notes like the other Companion channels."""
import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from inkbox_plugin import transcription
from inkbox_plugin.transcription import VoiceNoteGate, message_of
from tests import test_companion as base_harness
from tests.test_companion import idle, uid
from tests.test_companion_transcription import ObservedGate
from tests import test_slack_companion as slack_harness
from tests.test_slack_companion import incoming

factory = base_harness.factory
companion_host = slack_harness.slack_host
SPEECH = "A recorded update."
PENDING = "[Voice note 1: transcription pending; use the original attachment if needed and do not guess its contents]"
LABEL = "[Voice note 1 — automatically transcribed]\n" + SPEECH


def voice(number=3, *, phase="initialization", status="pending", message_id=True, **kwargs):
    value = incoming(number, phase=phase, **kwargs)
    if message_id:
        value["data"]["message_id"] = uid(number) if message_id is True else message_id
    value["data"]["event"]["files"] = [{
        "id": "FVOICE1", "name": "voice.m4a", "mimetype": "audio/mp4", "size": 2048,
        "transcription": {"status": status, "text": SPEECH if status == "completed" else None, "error_code": None},
    }]
    return value


def completion_for(value, status="completed"):
    completed = deepcopy(value)
    completed.pop("companion")
    completed["event_type"] = "message.transcription_finished"
    completed["data"]["channel"] = "slack"
    completed["data"]["event"]["files"][0]["transcription"] = {
        "status": status, "text": SPEECH if status == "completed" else None, "error_code": None}
    return completed


def trigger_text(host, text):
    """The server appends the trigger's voice-note results to its history entry."""
    trigger = next(entry for entry in host.snapshot["entries"] if entry["is_trigger"])
    trigger["text"] = text
    host.snapshot["text"] = json.dumps(host.snapshot["entries"])


async def stop(host):
    await host.receiver.close()
    await host.adapter._slack_activity.close()
    await host.adapter._voice_note_receiver().close()


def test_initialization_waits_for_slack_completion_and_refetches_once(companion_host, monkeypatch):
    async def run():
        host = companion_host
        gate = host.adapter._voice_notes = ObservedGate()
        read = AsyncMock()
        monkeypatch.setattr(transcription, "poll_message", read)
        value = voice()
        trigger_text(host, "<@UBOT> question 3\n\n" + PENDING)
        await host.receiver.accept(value)
        await asyncio.wait_for(gate.started.wait(), timeout=1)
        assert not host.inputs and host.resource.load_initialization.call_count == 1
        trigger_text(host, "<@UBOT> question 3\n\n" + LABEL)
        completion = completion_for(value)
        response = await host.adapter._route_inkbox_event(completion["event_type"], completion)
        assert response.status == 200
        await idle(host)
        assert len(host.inputs) == 1 and host.resource.load_initialization.call_count == 2
        text = host.inputs[0].text
        assert text.count(SPEECH) == 1 and text.count("Voice note 1") == 1
        assert "transcription pending" not in text and "transcript unavailable" not in text
        turn = next(iter(host.receiver.rows.values()))["turns"][0]
        assert turn["voice_note_resolution"]["snapshot_refreshed"] is True
        assert turn["voice_note_resolution"]["polled"] is False
        assert turn["envelope"]["data"]["event"]["files"][0]["transcription"]["text"] == SPEECH
        # A repeated completion is retained but never starts another turn.
        await host.adapter._route_inkbox_event(completion["event_type"], completion)
        await idle(host)
        assert len(host.inputs) == 1
        read.assert_not_called()
        await stop(host)
    asyncio.run(run())


def test_live_slack_turn_reads_once_on_timeout_and_labels_the_transcript(companion_host, monkeypatch):
    async def run():
        host = companion_host
        await host.receiver.accept(incoming())
        await idle(host)
        assert len(host.inputs) == 1
        host.adapter._voice_notes = VoiceNoteGate(wait_seconds=0)
        live = voice(4, phase="live", thread="1234567800.000001")
        read = AsyncMock(return_value=message_of(completion_for(live), "slack"))
        monkeypatch.setattr(transcription, "poll_message", read)
        await host.receiver.accept(live)
        await idle(host)
        assert len(host.inputs) == 2
        text = host.inputs[1].text
        assert LABEL in text and text.count("Voice note 1") == 1
        assert "<@UBOT> question 4" in text
        read.assert_awaited_once()
        channel, item = read.await_args.args[1:]
        assert channel == "slack" and item["id"] == uid(4) and item["connection_id"] == uid(40)
        await host.receiver.accept(live)
        await idle(host)
        assert len(host.inputs) == 2
        read.assert_awaited_once()
        await stop(host)
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["failed_completion", "failed_read"])
def test_live_slack_failure_delivers_original_attachment(companion_host, monkeypatch, outcome):
    async def run():
        host = companion_host
        await host.receiver.accept(incoming())
        await idle(host)
        live = voice(4, phase="live", thread="1234567800.000001")
        read = AsyncMock(side_effect=OSError("read failed"))
        monkeypatch.setattr(transcription, "poll_message", read)
        if outcome == "failed_completion":
            host.adapter._voice_note_receiver().complete("slack", message_of(completion_for(live, "failed"), "slack"))
        else:
            host.adapter._voice_notes = VoiceNoteGate(wait_seconds=0)
        await host.receiver.accept(live)
        await idle(host)
        assert len(host.inputs) == 2
        text = host.inputs[1].text
        assert text.count("[Voice note 1: transcript unavailable; process the original attachment as usual]") == 1
        assert "voice.m4a" in text and SPEECH not in text
        assert read.await_count == (0 if outcome == "failed_completion" else 1)
        await stop(host)
    asyncio.run(run())


@pytest.mark.parametrize("message_id", [False, uid(99)])
def test_slack_companion_without_matching_message_id_is_not_held(companion_host, monkeypatch, message_id):
    async def run():
        host = companion_host
        read = AsyncMock()
        monkeypatch.setattr(transcription, "poll_message", read)
        await host.receiver.accept(voice(message_id=message_id))
        await idle(host)
        assert len(host.inputs) == 1
        assert host.adapter._voice_note_receiver().entries == {}
        turn = next(iter(host.receiver.rows.values()))["turns"][0]
        assert "voice_note_resolution" not in turn
        assert turn["envelope"]["data"]["event"]["files"][0]["transcription"]["status"] == "pending"
        read.assert_not_called()
        await stop(host)
    asyncio.run(run())
