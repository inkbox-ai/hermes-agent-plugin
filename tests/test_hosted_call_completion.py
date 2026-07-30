import asyncio
import sys
import types
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod


def _adapter(tmp_path, *, transcript_rows=None, transcript_error=None):
    instance = adapter_mod.InkboxAdapter.__new__(adapter_mod.InkboxAdapter)
    instance.platform = types.SimpleNamespace(value="inkbox")
    instance._identity_handle = "agent-one"
    instance._voice_recently_closed = {}
    instance._hosted_post_call_active_chats = {}
    instance._hosted_post_call_failure_replies = {}
    instance._hosted_call_registry_owner = f"owner-{id(instance)}"
    instance._last_inbound_modality = {"contact-1": "sms"}
    instance._active_call_ws = {}
    instance._hosted_call_registry_path = (
        tmp_path / "hosted-call-completions.json"
    )
    events = []

    class Identity:
        def list_transcripts(self, call_id):
            assert call_id
            if transcript_error:
                raise transcript_error
            return transcript_rows or []

    instance._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: Identity(),
    )
    instance._resolve_channel_overrides = (
        lambda *_args: (None, ["inkbox:inkbox-call-review"])
    )

    async def _enqueue(event):
        events.append(event)

    instance._enqueue = _enqueue
    return instance, events


def _payload(
    *,
    direction="outbound",
    mode="hosted_agent",
    call_id="call-1",
    event_id="evt-call-1",
    remote_phone_number="+15551112222",
    memories=None,
):
    if memories is None:
        memories = ["Prefers concise calls."]
    return {
        "id": event_id,
        "event_type": "call.ended",
        "data": {
            "call": {
                "id": call_id,
                "mode": mode,
                "direction": direction,
                "status": "completed",
                "hangup_reason": "remote",
                "remote_phone_number": remote_phone_number,
                "reason": "Call about the release",
            },
            "contacts": [{
                "id": "contact-1",
                "name": "Alex",
                "memories": memories,
            }],
            "outcome": "completed",
            "transcript": {
                "entries": [
                    {"party": "remote", "text": "Friday works.", "ts_ms": 0},
                ],
                "abridged": False,
                "url": "https://example.invalid/transcript",
            },
            "transcript_url": "https://example.invalid/transcript",
            "post_call_action_items": [{
                "id": "action-1",
                "seq": 1,
                "action": "Email the release details",
                "details": "Send them today.",
                "status": "open",
            }],
        },
    }


def test_hosted_completion_fetches_full_transcript_and_enqueues_one_turn(
    tmp_path,
):
    rows = [
        types.SimpleNamespace(party="local", text="Does Friday work?"),
        types.SimpleNamespace(party="remote", text="Friday works."),
    ]
    instance, events = _adapter(tmp_path, transcript_rows=rows)

    response = asyncio.run(instance._on_call_ended(_payload()))

    assert response.status == 200
    assert len(events) == 1
    event = events[0]
    assert "Inkbox Voice AI finished" in event.text
    assert "Does Friday work?" in event.text
    assert "Email the release details" in event.text
    assert "execute every still-needed commitment" in event.text.lower()
    assert event.source.thread_id is None
    assert event.message_id == "call:call-1:ended"
    assert event.auto_skill == ["inkbox:inkbox-call-review"]
    assert "contact-1" in instance._voice_recently_closed
    assert "contact-1" not in instance._last_inbound_modality


def test_hosted_completion_marks_current_remote_number_as_authoritative(
    tmp_path,
):
    instance, events = _adapter(tmp_path)
    payload = _payload(
        direction="inbound",
        remote_phone_number="+15551112222",
        memories=["Alex also uses +15559990000."],
    )

    response = asyncio.run(instance._on_call_ended(payload))

    assert response.status == 200
    assert len(events) == 1
    event = events[0]
    assert "Remote party phone number: +15551112222" in event.text
    assert (
        "For a callback or other phone follow-up to this call's remote party, "
        "use that exact number."
    ) in event.text
    assert "Contact memories are background only and must not override it." in event.text
    assert "Alex also uses +15559990000." in event.text
    assert event.source.user_id_alt == "+15551112222"


def test_hosted_completion_falls_back_to_inline_transcript_for_inbound(
    tmp_path,
):
    instance, events = _adapter(
        tmp_path,
        transcript_error=RuntimeError("unavailable"),
    )

    response = asyncio.run(instance._on_call_ended(
        _payload(direction="inbound"),
    ))

    assert response.status == 200
    assert len(events) == 1
    assert "Friday works." in events[0].text
    assert events[0].source.thread_id == "call:call-1"


def test_client_websocket_completion_is_acknowledged_without_enqueue(tmp_path):
    instance, events = _adapter(tmp_path)

    response = asyncio.run(instance._on_call_ended(
        _payload(mode="client_websocket"),
    ))

    assert response.status == 200
    assert response.text == "ignored"
    assert events == []


def test_queued_hosted_completion_recovers_once_after_adapter_restart(tmp_path):
    first, first_events = _adapter(tmp_path)
    assert asyncio.run(first._on_call_ended(_payload())).status == 200
    assert len(first_events) == 1

    restarted, restarted_events = _adapter(tmp_path)
    replay = asyncio.run(restarted._on_call_ended(_payload()))

    assert replay.status == 200
    assert replay.text == "ok"
    assert len(restarted_events) == 1

    same_process_replay = asyncio.run(restarted._on_call_ended(_payload()))
    assert same_process_replay.text == "duplicate"
    assert len(restarted_events) == 1


def test_running_hosted_completion_recovers_after_adapter_restart(tmp_path):
    first, first_events = _adapter(tmp_path)
    asyncio.run(first._on_call_ended(_payload()))
    asyncio.run(first.on_processing_start(first_events[0]))
    assert first._read_hosted_call_registry()["call-1"]["state"] == "running"

    restarted, restarted_events = _adapter(tmp_path)
    replay = asyncio.run(restarted._on_call_ended(_payload()))

    assert replay.text == "ok"
    assert len(restarted_events) == 1


def test_completed_hosted_completion_stays_duplicate_after_restart(tmp_path):
    first, first_events = _adapter(tmp_path)
    asyncio.run(first._on_call_ended(_payload()))
    asyncio.run(first.on_processing_start(first_events[0]))
    asyncio.run(first.on_processing_complete(first_events[0], "success"))

    restarted, restarted_events = _adapter(tmp_path)
    replay = asyncio.run(restarted._on_call_ended(_payload()))

    assert replay.text == "duplicate"
    assert restarted_events == []


def test_hosted_processing_suppresses_text_for_entire_turn(tmp_path):
    instance, events = _adapter(tmp_path)
    asyncio.run(instance._on_call_ended(_payload()))
    event = events[0]

    # Simulate a turn that starts with stale channel routing and runs well
    # beyond the local call-close grace window.
    instance._last_inbound_modality["contact-1"] = "sms"
    instance._voice_recently_closed["contact-1"] = time.time() - 3_600
    asyncio.run(instance.on_processing_start(event))
    result = asyncio.run(instance.send(
        "contact-1",
        "Internal reconciliation summary that must not be delivered.",
        metadata={"mode": "sms"},
    ))

    assert result.success is True
    assert result.message_id == "suppressed-hosted-post-call-text"
    assert "contact-1" in instance._hosted_post_call_active_chats

    asyncio.run(instance.on_processing_complete(event, "success"))
    assert "contact-1" not in instance._hosted_post_call_active_chats
    registry = instance._read_hosted_call_registry()
    assert registry["call-1"]["state"] == "completed"
    assert registry["call-1"]["outcome"] == "success"


def test_overlapping_hosted_turns_keep_suppression_until_both_finish(tmp_path):
    instance, events = _adapter(tmp_path)
    asyncio.run(instance._on_call_ended(_payload()))
    asyncio.run(instance._on_call_ended(_payload(
        call_id="call-2",
        event_id="evt-call-2",
    )))
    first, second = events

    asyncio.run(instance.on_processing_start(first))
    asyncio.run(instance.on_processing_start(second))
    assert instance._hosted_post_call_active_chats["contact-1"] == 2

    asyncio.run(instance.on_processing_complete(first, "success"))
    assert instance._hosted_post_call_active_chats["contact-1"] == 1
    result = asyncio.run(instance.send(
        "contact-1",
        "The second turn is still processing.",
    ))
    assert result.message_id == "suppressed-hosted-post-call-text"

    asyncio.run(instance.on_processing_complete(second, "success"))
    assert "contact-1" not in instance._hosted_post_call_active_chats


def test_failed_hosted_turn_suppresses_host_error_after_complete(tmp_path):
    instance, events = _adapter(tmp_path)
    asyncio.run(instance._on_call_ended(_payload()))
    event = events[0]
    instance._last_inbound_modality["contact-1"] = "email"
    instance._voice_recently_closed["contact-1"] = time.time() - 3_600

    asyncio.run(instance.on_processing_start(event))
    asyncio.run(instance.on_processing_complete(event, "failure"))
    assert "contact-1" not in instance._hosted_post_call_active_chats

    result = asyncio.run(instance.send(
        "contact-1",
        "Sorry, I encountered an error (RuntimeError).\nqueue failed",
    ))

    assert result.message_id == "suppressed-hosted-post-call-error"
    assert "contact-1" not in instance._hosted_post_call_failure_replies


def test_hosted_completion_enqueue_failure_allows_webhook_retry(tmp_path):
    instance, _events = _adapter(tmp_path)

    async def _fail(_event):
        raise RuntimeError("queue unavailable")

    instance._enqueue = _fail
    with pytest.raises(RuntimeError, match="queue unavailable"):
        asyncio.run(instance._on_call_ended(_payload()))

    assert instance._read_hosted_call_registry() == {}
