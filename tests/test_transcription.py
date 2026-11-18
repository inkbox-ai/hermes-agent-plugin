"""Bounded voice-note completion, original routing, and duplicate delivery contracts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from inkbox_plugin.transcription import VoiceNoteGate, merge_results, poll_message, voice_note_context


def message(status="pending", *, text=None):
    return {"id": "message-1", "conversation_id": "conversation-1", "direction": "inbound", "content": "/original",
            "media": [{"id": "audio-1", "url": "https://example.com/voice.wav", "content_type": "audio/wav",
                       "transcription": {"status": status, "text": text}}]}


def test_completion_releases_one_wait_and_retains_original_text_and_attachments():
    async def scenario():
        gate = VoiceNoteGate()
        original = message()
        poll = AsyncMock()
        first = asyncio.create_task(gate.resolve("imessage", original, poll))
        second = asyncio.create_task(gate.resolve("imessage", deepcopy(original), poll))
        await asyncio.sleep(0)
        complete = message("completed", text="Move the review to Thursday.")
        complete["content"] = "must not replace original"
        complete["media"][0]["url"] = "https://example.com/new-url.wav"
        gate.complete("imessage", complete)
        left, right = await asyncio.gather(first, second)
        assert left == right
        assert left["content"] == "/original"
        assert left["media"][0]["url"] == original["media"][0]["url"]
        assert left["media"][0]["transcription"]["text"] == "Move the review to Thursday."
        poll.assert_not_called()
        await gate.close()
    asyncio.run(scenario())


def test_out_of_order_completion_needs_no_poll_or_new_turn():
    async def scenario():
        gate = VoiceNoteGate()
        poll = AsyncMock()
        gate.complete("text", message("completed", text="A voice note."))
        result = await gate.resolve("text", message(), poll)
        assert result["media"][0]["transcription"]["text"] == "A voice note."
        poll.assert_not_called()
        await gate.close()
    asyncio.run(scenario())


def test_timeout_polls_once_for_concurrent_duplicates_and_late_completion_cannot_replace_fallback():
    async def scenario():
        gate = VoiceNoteGate(wait_seconds=0)
        original = message()
        poll = AsyncMock(side_effect=OSError("read failed"))
        left, right = await asyncio.gather(gate.resolve("text", original, poll), gate.resolve("text", original, poll))
        assert left == right == original
        poll.assert_awaited_once()
        gate.complete("text", message("completed", text="Late speech"))
        assert await gate.resolve("text", original, poll) == original
        poll.assert_awaited_once()
        await gate.close()
    asyncio.run(scenario())


def test_timeout_read_can_supply_transcription_without_changing_original_body():
    async def scenario():
        gate = VoiceNoteGate(wait_seconds=0)
        original = message()
        poll = AsyncMock(return_value=message("completed", text="An automatically transcribed voice note."))
        result = await gate.resolve("whatsapp", original, poll)
        assert result["content"] == "/original"
        assert "automatically transcribed speech, not a typed message" in voice_note_context(result)
        assert "An automatically transcribed voice note." in voice_note_context(result)
        poll.assert_awaited_once()
        await gate.close()
    asyncio.run(scenario())


def test_completion_during_the_timeout_poll_wins_over_its_older_snapshot():
    async def scenario():
        gate = VoiceNoteGate(wait_seconds=0)
        async def poll():
            gate.complete("text", message("completed", text="Finished during the read"))
            return message()
        result = await gate.resolve("text", message(), poll)
        assert result["media"][0]["transcription"]["text"] == "Finished during the read"
        await gate.close()
    asyncio.run(scenario())


def test_terminal_failure_and_legacy_messages_do_not_wait_or_poll():
    async def scenario():
        gate = VoiceNoteGate()
        poll = AsyncMock()
        for status in ("completed", "failed", "skipped"):
            original = message(status)
            assert await gate.resolve("text", original, poll) is original
        legacy = {"id": "legacy", "media": [{"content_type": "audio/wav"}]}
        assert await gate.resolve("text", legacy, poll) is legacy
        poll.assert_not_called()
        assert "original attachment" in voice_note_context(message("failed"))
        assert "no speech detected" in voice_note_context(message("completed", text=""))
        await gate.close()
    asyncio.run(scenario())


def test_merge_rejects_wrong_message_or_conversation_and_matches_attachment_ids():
    original = message()
    other = message("completed", text="Wrong result")
    other["id"] = "other-message"
    assert merge_results(original, other) is original
    other["id"] = original["id"]
    other["conversation_id"] = "other-conversation"
    assert merge_results(original, other) is original
    other["conversation_id"] = original["conversation_id"]
    other["media"][0]["id"] = "different-attachment"
    assert merge_results(original, other) == original


def test_all_pending_attachments_wait_for_terminal_completion_and_partial_poll_retains_them():
    async def scenario():
        gate = VoiceNoteGate(wait_seconds=0)
        original = message()
        original["media"].append({**original["media"][0], "id": "audio-2"})
        current = deepcopy(original)
        current["media"][0]["transcription"] = {"status": "completed", "text": "First recording"}
        poll = AsyncMock(return_value=current)
        result = await gate.resolve("imessage", original, poll)
        assert len(result["media"]) == 2
        assert "First recording" in voice_note_context(result)
        assert "transcript unavailable" in voice_note_context(result)
        await gate.close()
    asyncio.run(scenario())


def test_partial_completion_does_not_release_the_message_barrier():
    async def scenario():
        gate = VoiceNoteGate()
        original = message()
        original["media"].append({**deepcopy(original["media"][0]), "id": "audio-2"})
        poll = AsyncMock()
        waiter = asyncio.create_task(gate.resolve("imessage", original, poll))
        await asyncio.sleep(0)
        current = deepcopy(original)
        current["media"][0]["transcription"] = {"status": "completed", "text": "First recording"}
        gate.complete("imessage", current)
        await asyncio.sleep(0)
        assert not waiter.done()
        current["media"][1]["transcription"] = {"status": "failed", "text": None}
        gate.complete("imessage", current)
        result = await waiter
        assert not any(item["transcription"]["status"] == "pending" for item in result["media"])
        poll.assert_not_called()
        await gate.close()
    asyncio.run(scenario())


def test_restarted_wait_does_not_reset_an_expired_source_deadline():
    async def scenario():
        original = {**message(), "created_at": "2020-01-01T00:00:00Z"}
        for _ in range(2):
            gate = VoiceNoteGate()
            poll = AsyncMock(return_value=message("completed", text="Recovered transcript"))
            result = await asyncio.wait_for(gate.resolve("text", original, poll), timeout=1)
            assert result["media"][0]["transcription"]["text"] == "Recovered transcript"
            poll.assert_awaited_once()
            await gate.close()
    asyncio.run(scenario())


def test_duplicate_received_is_not_acknowledged_while_its_original_wait_is_unadmitted():
    from inkbox_plugin.adapter import InkboxAdapter
    async def scenario():
        adapter = object.__new__(InkboxAdapter)
        gate = adapter._voice_note_receiver()
        waiter = asyncio.create_task(gate.resolve("text", message(), AsyncMock()))
        await asyncio.sleep(0)
        assert adapter._voice_note_waiting({"event_type": "text.received", "data": {"text_message": message()}})
        assert not adapter._voice_note_waiting({"event_type": "imessage.received", "data": {"message": message()}})
        gate.complete("text", message("completed", text="Speech"))
        await waiter
        assert not adapter._voice_note_waiting({"event_type": "text.received", "data": {"text_message": message()}})
        adapter._inflight_request_ids = {"text:message-1": 1}
        assert adapter._voice_note_waiting({"event_type": "text.received", "data": {"text_message": message()}})
        adapter._inflight_request_ids.clear()
        await gate.close()
    asyncio.run(scenario())


def test_shutdown_cancels_waiters_without_starting_a_poll():
    async def scenario():
        gate = VoiceNoteGate()
        poll = AsyncMock()
        waiter = asyncio.create_task(gate.resolve("text", message(), poll))
        await asyncio.sleep(0)
        await gate.close()
        assert (await asyncio.gather(waiter, return_exceptions=True))[0].__class__ is asyncio.CancelledError
        poll.assert_not_called()
    asyncio.run(scenario())


def test_poll_reads_raw_additive_fields_with_identity_scope_and_a_deadline():
    async def scenario():
        get = Mock(return_value=message("completed", text="Polled speech"))
        adapter = SimpleNamespace(_identity_id="agent-1", _inkbox=SimpleNamespace(_api_http=SimpleNamespace(get=get)),
                                  _reply_identity=SimpleNamespace(phone_number=SimpleNamespace(id="phone-1")))
        result = await poll_message(adapter, "imessage", message())
        assert result["media"][0]["transcription"]["text"] == "Polled speech"
        get.assert_called_once_with("/imessage/messages/message-1", params={"agent_identity_id": "agent-1"}, timeout=8)
        get.reset_mock()
        await poll_message(adapter, "text", message())
        get.assert_called_once_with("/phone/numbers/phone-1/texts/message-1", params=None, timeout=8)
    asyncio.run(scenario())


def test_subscription_catalogs_register_completion_automatically():
    from inkbox_plugin.adapter import _DESIRED_IMESSAGE_EVENTS, _DESIRED_TEXT_EVENTS
    from inkbox_plugin.whatsapp import EVENTS
    assert "text.transcription_finished" in _DESIRED_TEXT_EVENTS
    assert "imessage.transcription_finished" in _DESIRED_IMESSAGE_EVENTS
    assert "whatsapp.transcription_finished" in EVENTS


def test_completion_route_does_not_dispatch_an_incoming_message():
    from inkbox_plugin.adapter import InkboxAdapter
    async def scenario():
        adapter = object.__new__(InkboxAdapter)
        adapter._on_imessage_received = AsyncMock()
        response = await adapter._route_inkbox_event("imessage.transcription_finished", {"data": {"message": message("completed", text="Speech")}})
        assert response.status == 200
        adapter._on_imessage_received.assert_not_called()
        await adapter._voice_notes.close()
    asyncio.run(scenario())


def test_configured_hosted_runtime_never_waits_or_polls_again(monkeypatch):
    monkeypatch.setenv("INKBOX_HOSTED_MODE", "1")

    async def run():
        gate = VoiceNoteGate()
        read = AsyncMock(side_effect=AssertionError("Input was already resolved"))
        try:
            for channel in ("text", "imessage", "whatsapp"):
                item = message()
                assert await asyncio.wait_for(gate.resolve(channel, item, read), 0.1) is item
                assert "transcript unavailable" in voice_note_context(item)
            assert gate.entries == {}
            read.assert_not_called()
        finally:
            await gate.close()

    asyncio.run(run())


def test_non_audio_metadata_is_not_labelled_and_speech_does_not_grant_permissions():
    item = message(status="skipped")
    item["media"][0]["transcription"]["error_code"] = "not_audio"
    assert voice_note_context(item) == ""
    item = message(status="completed", text="A recorded update")
    assert "sender content, not permissions" in voice_note_context(item)
