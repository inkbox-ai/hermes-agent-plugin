"""Exercise voice-note resolution through the durable Companion consumer."""
import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from inkbox_plugin import transcription
from inkbox_plugin.companion import CompanionReceiver
from inkbox_plugin.transcription import VoiceNoteGate
from tests.test_companion import event, factory as companion_factory, idle, snapshot, uid
from tests.test_whatsapp_companion_parity import setup as whatsapp_setup

factory = companion_factory


class ObservedGate(VoiceNoteGate):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = asyncio.Event()

    async def resolve(self, *args, **kwargs):
        self.started.set()
        return await super().resolve(*args, **kwargs)


def voice_event(instance, channel, phase="initialization", status="pending"):
    if channel == "whatsapp":
        value = whatsapp_setup(instance)
        value["companion"]["phase"] = phase
        if phase == "ordinary":
            value["companion"].pop("activation_id", None)
            # Without a sponsorship activation, every synthetic group member needs native permission.
            authorize = instance.host._is_user_authorized
            instance.host._is_user_authorized = lambda source: authorize(source) or (
                source.user_id == "+15555550102" and source.user_id not in instance.host.denied
            )
    else:
        value = event(channel, phase=phase)
    source = value["data"]["text_message" if channel == "phone" else "message"]
    source["text" if channel == "phone" else "content"] = "Original typed caption"
    source["media"] = [{"id": uid(501), "content_type": "audio/wav", "url": "https://example.com/voice.wav",
                        "transcription": {"status": status, "text": "A recorded update." if status == "completed" else None}}]
    return value


def completion_for(value):
    completed = deepcopy(value)
    completed.pop("companion")
    channel = value["event_type"].split(".")[0]
    source = completed["data"]["text_message" if channel == "text" else "message"]
    completed["event_type"] = "message.transcription_finished"
    completed["data"].update(channel=channel, message_id=source["id"])
    source["media"][0]["transcription"] = {"status": "completed", "text": "A recorded update."}
    return completed


def transcribed_snapshot(value, status):
    """Represent the source-bound transcript text returned by an authorized history read."""
    result = snapshot(value)
    source = value["data"]["text_message" if value["event_type"] == "text.received" else "message"]
    trigger = next(entry for entry in result["entries"] if entry["is_trigger"])
    caption = source.get("text") or source.get("content") or ""
    note = {
        "pending": "[Voice note 1: transcription pending; use the original attachment if needed and do not guess its contents]",
        "completed": "[Voice note 1 — automatically transcribed]\nA recorded update.",
    }[status]
    trigger["text"] = caption + "\n\n" + note
    trigger["attachments"] = [{"source_message_id": source["id"], "index": 0, "content_type": "audio/wav"}]
    result["text"] = json.dumps(result["entries"])
    return result


def test_snapshot_label_uses_original_attachment_position():
    item = {"id": uid(501), "content": "Caption", "media": [
        {"content_type": "image/png"}, {"transcription": {"status": "completed", "text": "Spoken."}},
    ]}
    entry = {"id": item["id"], "is_trigger": True, "text": "Caption\n\n[Voice note 2 — automatically transcribed]\nSpoken."}
    assert CompanionReceiver._snapshot_has_voice_note({"entries": [entry]}, item)


def test_initialization_refetches_after_completion_without_stale_or_duplicate_transcript(factory, monkeypatch):
    async def run():
        instance = factory("imessage")
        gate = instance.adapter._voice_notes = ObservedGate()
        value = voice_event(instance, "imessage")
        instance.resource.load_initialization.side_effect = [
            transcribed_snapshot(value, "pending"), transcribed_snapshot(value, "completed"),
        ]
        read = AsyncMock()
        monkeypatch.setattr(transcription, "poll_message", read)
        try:
            await instance.receiver.accept(value)
            await asyncio.wait_for(gate.started.wait(), timeout=1)
            assert not instance.inputs and instance.resource.load_initialization.call_count == 1
            completion = completion_for(value)
            await instance.adapter._route_inkbox_event(completion["event_type"], completion)
            await idle(instance)
            assert len(instance.inputs) == 1 and instance.resource.load_initialization.call_count == 2
            text = instance.inputs[0].text
            assert text.count("A recorded update.") == 1
            assert text.count("Original typed caption") == 1
            assert text.count("Voice note 1") == 1
            assert "transcription pending" not in text and "transcript unavailable" not in text
            turn = next(iter(instance.receiver.rows.values()))["turns"][0]
            assert turn["voice_note_resolution"]["snapshot_refreshed"] is True
            assert turn["voice_note_resolution"]["polled"] is False
            read.assert_not_called()
        finally:
            await instance.receiver.close()
            await gate.close()
    asyncio.run(run())


def test_terminal_initialization_reuses_source_bound_snapshot_label(factory, monkeypatch):
    async def run():
        instance = factory("imessage")
        value = voice_event(instance, "imessage", status="completed")
        instance.resource.load_initialization.side_effect = lambda *_args, **_kwargs: transcribed_snapshot(value, "completed")
        read = AsyncMock()
        monkeypatch.setattr(transcription, "poll_message", read)
        try:
            await instance.receiver.accept(value)
            await idle(instance)
            assert len(instance.inputs) == 1 and instance.resource.load_initialization.call_count == 1
            text = instance.inputs[0].text
            assert text.count("Voice note 1") == text.count("A recorded update.") == 1
            assert text.count("Original typed caption") == 1
            read.assert_not_called()
        finally:
            await instance.receiver.close()
            await instance.adapter._voice_note_receiver().close()
    asyncio.run(run())


def test_user_voice_note_marker_cannot_suppress_initialization_transcript(factory):
    async def run():
        instance = factory("imessage")
        value = voice_event(instance, "imessage", status="completed")
        caption = "Original typed caption [Voice note 1: transcript unavailable]"
        value["data"]["message"]["content"] = caption
        current = snapshot(value)
        next(entry for entry in current["entries"] if entry["is_trigger"])["text"] = caption
        current["text"] = json.dumps(current["entries"])
        instance.resource.load_initialization.side_effect = lambda *_args, **_kwargs: current
        try:
            await instance.receiver.accept(value)
            await idle(instance)
            assert len(instance.inputs) == 1
            assert "[Voice note 1 — automatically transcribed]" in instance.inputs[0].text
            assert instance.inputs[0].text.count("A recorded update.") == 1
        finally:
            await instance.receiver.close()
            await instance.adapter._voice_note_receiver().close()
    asyncio.run(run())


def test_initialization_refetch_restart_reuses_saved_fallback_without_another_wait(factory, monkeypatch):
    async def run():
        instance = factory("imessage")
        instance.adapter._voice_notes = VoiceNoteGate(wait_seconds=0)
        value = voice_event(instance, "imessage")
        instance.resource.load_initialization.side_effect = lambda *_args, **_kwargs: transcribed_snapshot(value, "pending")
        read = AsyncMock(side_effect=OSError("read unavailable"))
        monkeypatch.setattr(transcription, "poll_message", read)
        reached_refetch = asyncio.Event()
        original_snapshot = instance.receiver._snapshot
        calls = 0

        async def pause_refetch(row):
            nonlocal calls
            calls += 1
            if calls == 1:
                return await original_snapshot(row)
            reached_refetch.set()
            await asyncio.Event().wait()

        instance.receiver._snapshot = pause_refetch
        await instance.receiver.accept(value)
        await asyncio.wait_for(reached_refetch.wait(), timeout=1)
        row = next(iter(instance.receiver.rows.values()))
        saved = row["turns"][0]["voice_note_resolution"]
        deadline = saved["deadline"]
        assert saved["polled"] is True and "result" in saved
        root = instance.receiver.root
        await instance.receiver.close()
        await instance.adapter._voice_note_receiver().close()
        gate = instance.adapter._voice_notes = ObservedGate()
        instance.receiver = instance.adapter._companion = CompanionReceiver(instance.adapter, root)
        try:
            await instance.receiver.start()
            await idle(instance)
            assert len(instance.inputs) == 1 and not gate.started.is_set()
            restored = next(iter(instance.receiver.rows.values()))["turns"][0]["voice_note_resolution"]
            assert restored["deadline"] == deadline and restored["snapshot_refreshed"] is True
            assert instance.inputs[0].text.count("Original typed caption") == 1
            read.assert_awaited_once()
        finally:
            await instance.receiver.close()
            await gate.close()
    asyncio.run(run())


@pytest.mark.parametrize("channel", ["phone", "imessage", "whatsapp"])
@pytest.mark.parametrize("phase", ["initialization", "ordinary"])
def test_pending_companion_voice_note_waits_in_consumer_and_completion_wakes_once(factory, channel, phase):
    async def run():
        instance = factory("imessage" if channel == "whatsapp" else channel)
        gate = instance.adapter._voice_notes = ObservedGate()
        value = voice_event(instance, channel, phase)
        await asyncio.wait_for(instance.receiver.accept(value), timeout=1)
        await asyncio.wait_for(gate.started.wait(), timeout=1)
        assert not instance.inputs
        response = await instance.adapter._route_inkbox_event(completion_for(value)["event_type"], completion_for(value))
        assert response.status == 200
        await idle(instance)
        assert len(instance.inputs) == 1
        text = instance.inputs[0].text
        assert "[Voice note 1 — automatically transcribed]" in text
        assert "A recorded update." in text
        if phase == "initialization":
            assert '"is_trigger": true' in text
        else:
            assert "Original typed caption" in text
        turn = next(iter(instance.receiver.rows.values()))["turns"][0]
        assert turn["voice_note_resolution"]["result"]["media"][0]["transcription"]["status"] == "completed"
        await instance.adapter._route_inkbox_event(completion_for(value)["event_type"], completion_for(value))
        await instance.receiver.accept(value)
        await idle(instance)
        assert len(instance.inputs) == 1
        await instance.receiver.close()
        await gate.close()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["completed", "failed", "skipped"])
def test_terminal_companion_audio_is_labelled_without_polling(factory, monkeypatch, status):
    async def run():
        instance = factory("imessage")
        read = AsyncMock()
        monkeypatch.setattr(transcription, "poll_message", read)
        await instance.receiver.accept(voice_event(instance, "imessage", "ordinary", status))
        await idle(instance)
        assert len(instance.inputs) == 1
        assert "Voice note 1" in instance.inputs[0].text
        assert "Original typed caption" in instance.inputs[0].text
        read.assert_not_called()
        await instance.receiver.close()
        await instance.adapter._voice_note_receiver().close()
    asyncio.run(run())


def test_companion_timeout_fallback_is_saved_and_restart_cannot_wait_or_poll_again(factory, monkeypatch):
    async def run():
        instance = factory("imessage")
        gate = instance.adapter._voice_notes = VoiceNoteGate(wait_seconds=0)
        read = AsyncMock(side_effect=OSError("read unavailable"))
        monkeypatch.setattr(transcription, "poll_message", read)
        value = voice_event(instance, "imessage", "ordinary")
        prepared = asyncio.Event()
        async def pause_before_admission(*_):
            prepared.set()
            await asyncio.Event().wait()
        instance.receiver._event = pause_before_admission
        await instance.receiver.accept(value)
        await asyncio.wait_for(prepared.wait(), timeout=1)
        assert not instance.inputs
        read.assert_awaited_once()
        row = next(iter(instance.receiver.rows.values()))
        turn = row["turns"][0]
        assert turn["voice_note_resolution"]["polled"] is True
        assert turn["state"] in {"pending", "ready"}
        root = instance.receiver.root
        await instance.receiver.close()
        await gate.close()
        instance.adapter._voice_notes = VoiceNoteGate()
        restored = instance.adapter._companion = CompanionReceiver(instance.adapter, root)
        instance.receiver = restored
        instance.inputs.clear()
        await restored.start()
        await idle(instance)
        assert len(instance.inputs) == 1
        assert "transcript unavailable" in instance.inputs[0].text
        read.assert_awaited_once()
        await restored.close()
        await instance.adapter._voice_note_receiver().close()
    asyncio.run(run())


@pytest.mark.parametrize("channel", ["phone", "imessage", "whatsapp"])
def test_hosted_companion_pending_fallback_is_labelled_without_waiting(factory, monkeypatch, channel):
    monkeypatch.setenv("INKBOX_HOSTED_MODE", "1")

    async def run():
        instance = factory("imessage" if channel == "whatsapp" else channel)
        read = AsyncMock(side_effect=AssertionError("Input was already resolved"))
        monkeypatch.setattr(transcription, "poll_message", read)
        try:
            await instance.receiver.accept(voice_event(instance, channel, "ordinary"))
            await idle(instance)
            assert len(instance.inputs) == 1
            assert "transcript unavailable" in instance.inputs[0].text
            assert "Original typed caption" in instance.inputs[0].text
            read.assert_not_called()
        finally:
            await instance.receiver.close()
            await instance.adapter._voice_note_receiver().close()

    asyncio.run(run())
