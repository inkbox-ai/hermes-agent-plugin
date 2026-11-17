"""Ordinary HTTP ACK, private receipt recovery, and one-poll voice-note admission."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web

from inkbox_plugin import adapter as adapter_module
from inkbox_plugin import voice_note_ingress as ingress_module
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.transcription import VoiceNoteGate
from tests.test_transcription import message


class Request:
    def __init__(self, envelope):
        self.body = json.dumps(envelope).encode()
        self.headers = {"X-Inkbox-Signature": "sha256=synthetic"}
        self.url = "https://example.com/webhook"

    async def read(self):
        return self.body


def original():
    return {"id": "original-event", "event_type": "text.received", "data": {"text_message": message()}}


def completed():
    value = original()
    value["event_type"] = "text.transcription_finished"
    value["data"]["text_message"]["media"][0]["transcription"] = {"status": "completed", "text": "Move the review to Thursday."}
    return value


def receiver(tmp_path, monkeypatch):
    monkeypatch.delenv("INKBOX_HOSTED_ACTIVATION_ID", raising=False)
    monkeypatch.setattr(adapter_module, "_inkbox_state_path", lambda: tmp_path / "identity.json")
    provider = SimpleNamespace(name="inkbox", verify=Mock(return_value=True))
    monkeypatch.setattr(adapter_module, "match_provider", lambda _: provider)
    adapter = object.__new__(InkboxAdapter)
    adapter._base_url = "https://example.com"
    adapter._identity_id = "synthetic-agent"
    adapter._signing_key = "whsec_synthetic"
    adapter._require_signature = True
    adapter._companion = None
    rows = []
    delivered = asyncio.Event()

    async def route(event_type, envelope, headers=None):
        rows.append(deepcopy(envelope))
        delivered.set()
        return web.Response(status=200)

    adapter._route_inkbox_event = route
    ingress = adapter._ordinary_voice_note_receiver()
    return adapter, ingress, provider, rows, delivered


async def close(adapter, ingress):
    await ingress.close()
    await adapter._voice_note_receiver().close()


def test_real_ordinary_handler_acknowledges_saved_original_promptly_and_recovers_after_restart(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, _, rows, _ = receiver(tmp_path, monkeypatch)
        ingress.start()
        response = await asyncio.wait_for(adapter._handle_webhook(Request(original())), timeout=1)
        assert response.status == 202
        assert not rows
        assert ingress.db.execute("SELECT state FROM deliveries").fetchone()[0] == "pending"
        deadline = ingress.db.execute("SELECT deadline FROM voice_note_resolution").fetchone()[0]
        await close(adapter, ingress)

        recovered, inbox, _, restored, delivered = receiver(tmp_path, monkeypatch)
        inbox.start()
        assert inbox.db.execute("SELECT deadline FROM voice_note_resolution").fetchone()[0] == deadline
        response = await asyncio.wait_for(recovered._handle_webhook(Request(completed())), timeout=1)
        assert response.status == 202
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert len(restored) == 1
        assert restored[0]["data"]["text_message"]["content"] == "/original"
        assert restored[0]["data"]["text_message"]["media"][0]["transcription"]["text"] == "Move the review to Thursday."
        assert inbox.db.execute("SELECT state FROM deliveries").fetchone()[0] == "completed"
        await recovered._handle_webhook(Request(original()))
        await recovered._handle_webhook(Request(completed()))
        await asyncio.sleep(0)
        assert len(restored) == 1
        await close(recovered, inbox)
    asyncio.run(scenario())


def test_rejected_signature_never_saves_or_schedules_a_voice_note(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, provider, rows, _ = receiver(tmp_path, monkeypatch)
        provider.verify.return_value = False
        response = await adapter._handle_webhook(Request(original()))
        assert response.status == 401
        assert not rows
        assert ingress.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
        await close(adapter, ingress)
    asyncio.run(scenario())


def test_storage_failure_is_not_acknowledged_as_saved(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, _, rows, _ = receiver(tmp_path, monkeypatch)
        monkeypatch.setattr(ingress, "accept", Mock(side_effect=OSError("disk full")))
        response = await adapter._handle_webhook(Request(original()))
        assert response.status == 503
        assert not rows
        await close(adapter, ingress)
    asyncio.run(scenario())


def test_restart_during_timeout_read_does_not_poll_again_or_drop_fallback(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, _, _, _ = receiver(tmp_path, monkeypatch)
        adapter._voice_notes = VoiceNoteGate(wait_seconds=0)
        started = asyncio.Event()
        async def poll(*_):
            started.set()
            await asyncio.Event().wait()
        read = AsyncMock(side_effect=poll)
        monkeypatch.setattr(ingress_module, "poll_message", read)
        ingress.start()
        await adapter._handle_webhook(Request(original()))
        await asyncio.wait_for(started.wait(), timeout=1)
        await close(adapter, ingress)
        read.assert_awaited_once()

        recovered, inbox, _, rows, delivered = receiver(tmp_path, monkeypatch)
        recovered._voice_notes = VoiceNoteGate(wait_seconds=0)
        inbox.start()
        await asyncio.wait_for(delivered.wait(), timeout=1)
        read.assert_awaited_once()
        assert rows[0]["data"]["text_message"] == message()
        await close(recovered, inbox)
    asyncio.run(scenario())


def test_out_of_order_completion_is_saved_across_restart_without_waking_alone(tmp_path, monkeypatch):
    async def scenario():
        adapter, ingress, _, rows, _ = receiver(tmp_path, monkeypatch)
        ingress.start()
        await adapter._handle_webhook(Request(completed()))
        assert not rows and not ingress.tasks
        await close(adapter, ingress)
        recovered, inbox, _, rows, delivered = receiver(tmp_path, monkeypatch)
        inbox.start()
        await recovered._handle_webhook(Request(original()))
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert len(rows) == 1
        await close(recovered, inbox)
    asyncio.run(scenario())
