"""Exercise voice-note resolution through the durable Companion consumer."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from inkbox_plugin import transcription
from inkbox_plugin.companion import CompanionReceiver
from inkbox_plugin.transcription import VoiceNoteGate
from tests.test_companion import event, factory as companion_factory, idle, uid
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
    completed["event_type"] = value["event_type"].replace(".received", ".transcription_finished")
    source = completed["data"]["text_message" if completed["event_type"].startswith("text.") else "message"]
    source["media"][0]["transcription"] = {"status": "completed", "text": "A recorded update."}
    return completed


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
        assert "automatically transcribed speech, not a typed message" in text
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
