import asyncio
import json
import sys
import types
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.hosted_call_context import (
    activate_next_hosted_turn_context,
    enqueue_hosted_turn_context,
    observe_hosted_tool_call,
    observe_hosted_tool_start,
)


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
    instance.handle_message = _enqueue
    return instance, events


def _record_sms_result(
    tmp_path,
    monkeypatch,
    *,
    attempt=1,
    result=None,
    target="+15551112222",
    count=1,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    enqueue_hosted_turn_context("session-1", {
        "call_id": "call-1",
        "remote_phone": "+15551112222",
        "sms_required": True,
        "attempt": attempt,
    })
    activate_next_hosted_turn_context("session-1")
    for _ in range(count):
        observe_hosted_tool_call(
            tool_name="inkbox_send_sms",
            args={"to": target, "text": "release-ready"},
            result=json.dumps(result or {"ok": True}),
            task_id="session-1",
            status="ok" if (result or {"ok": True}).get("ok") is True else "error",
        )


def _record_sms_start(tmp_path, monkeypatch, *, attempt=1):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    enqueue_hosted_turn_context("session-1", {
        "call_id": "call-1",
        "remote_phone": "+15551112222",
        "sms_required": True,
        "attempt": attempt,
    })
    activate_next_hosted_turn_context("session-1")
    observe_hosted_tool_start(
        tool_name="inkbox_send_sms",
        args={"to": "+15551112222", "text": "release-ready"},
        task_id="tool-task-99",
        session_id="session-1",
        tool_call_id=f"sms-attempt-{attempt}",
    )


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


def test_hosted_sms_commitment_uses_exact_tool_success_contract(tmp_path):
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "id": "action-sms",
        "action": "Send one SMS containing exactly: release-ready",
        "status": "open",
    }]
    payload["data"]["transcript"]["entries"] = [{
        "party": "remote",
        "text": "After we hang up, text me release-ready.",
    }]

    response = asyncio.run(instance._on_call_ended(payload))

    assert response.status == 200
    assert len(events) == 1
    prompt = events[0].text
    assert "SMS post-call tool contract:" in prompt
    assert "use inkbox_send_sms" in prompt
    assert "tool_search" in prompt
    assert "`to` set to the exact authoritative remote number `+15551112222`" in prompt
    assert "`text` set to the requested SMS body" in prompt
    assert "Do not use conversationId" in prompt
    assert "`ok: true`" in prompt
    assert "correct the arguments once and retry once" in prompt
    assert "If the first error is nonrecoverable" in prompt
    assert "the one corrected call also fails" in prompt
    assert "do not call the tool again; reply exactly [SILENT]" in prompt
    assert (
        "Do not reply [SILENT] while a safe SMS commitment remains unattempted"
        in prompt
    )
    assert "unattempted or lacks a successful tool result" not in prompt


@pytest.mark.parametrize(
    "text",
    [
        "After we hang up, send me one SMS containing release-ready.",
        "After this call ends, text the release status to the team.",
        "When the call ends, send her an SMS with the confirmation.",
        "I will text you once this call is over.",
    ],
)
def test_hosted_sms_commitment_is_detected_in_transcript(tmp_path, text):
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = []
    payload["data"]["transcript"]["entries"] = [{
        "party": "remote",
        "text": text,
    }]

    asyncio.run(instance._on_call_ended(payload))

    assert instance._hosted_sms_required(events[0]) is True


def test_hosted_sms_commitment_uses_fetched_authoritative_transcript(tmp_path):
    rows = [types.SimpleNamespace(
        party="remote",
        text="After we hang up, send me one SMS containing release-ready.",
    )]
    instance, events = _adapter(tmp_path, transcript_rows=rows)
    payload = _payload()
    payload["data"]["post_call_action_items"] = []
    payload["data"].pop("transcript")

    asyncio.run(instance._on_call_ended(payload))

    assert instance._hosted_sms_required(events[0]) is True


@pytest.mark.parametrize(
    "text",
    [
        "I got your text yesterday.",
        "She texted me about the release.",
        "Our SMS history has the details.",
        "After we hang up, review the release notes.",
        "After we hang up, review the text history.",
        "After we hang up, review the text conversation.",
        "After we hang up, send me the report by email.",
        "I will send her the confirmation.",
        "Please text me the final release status.",
        "I will send her an SMS with the confirmation.",
        "The call ended after I sent the message.",
        "After we hang up, do not send me an SMS.",
        "Don't text me after the call ends.",
        "After the call ends, never text me.",
    ],
)
def test_hosted_sms_history_does_not_create_a_commitment(tmp_path, text):
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = []
    payload["data"]["transcript"]["entries"] = [{
        "party": "remote",
        "text": text,
    }]

    asyncio.run(instance._on_call_ended(payload))

    assert instance._hosted_sms_required(events[0]) is False


def test_negated_open_action_does_not_create_sms_commitment(tmp_path):
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Do not send me an SMS after this call.",
        "status": "open",
    }]
    payload["data"]["transcript"]["entries"] = []

    asyncio.run(instance._on_call_ended(payload))

    assert instance._hosted_sms_required(events[0]) is False


def test_hosted_sms_missing_tool_enqueues_one_correction(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "id": "action-sms",
        "action": "Send one SMS containing exactly: release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    event = events[0]

    asyncio.run(instance.on_processing_start(event))
    asyncio.run(instance.on_processing_complete(event, "success"))

    assert len(events) == 2
    correction = events[1]
    assert "only correction attempt" in correction.text
    assert "Do not reply [SILENT] or skip the tool" in correction.text
    assert correction.raw_message["_inkbox_hosted_reconciliation_attempt"] == 2
    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "queued"
    assert receipt["envelope"]["_inkbox_hosted_reconciliation_attempt"] == 2


def test_queued_hosted_sms_correction_replays_as_correction_after_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    asyncio.run(instance.on_processing_start(events[0]))
    asyncio.run(instance.on_processing_complete(events[0], "success"))

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())

    assert len(restarted_events) == 1
    replay = restarted_events[0]
    assert "only correction attempt" in replay.text
    assert replay.raw_message["_inkbox_hosted_reconciliation_attempt"] == 2


def test_clean_running_hosted_sms_correction_replays_after_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    asyncio.run(instance.on_processing_start(events[0]))
    asyncio.run(instance.on_processing_complete(events[0], "success"))
    correction = events[1]
    asyncio.run(instance.on_processing_start(correction))

    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "running"
    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())
    assert len(restarted_events) == 1
    assert "only correction attempt" in restarted_events[0].text


def test_clean_running_initial_hosted_sms_turn_replays_after_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    asyncio.run(instance.on_processing_start(events[0]))

    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "running"
    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())
    assert len(restarted_events) == 1


@pytest.mark.parametrize("attempt", [1, 2])
def test_started_hosted_sms_tool_is_terminal_across_restart(
    tmp_path,
    monkeypatch,
    attempt,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    first = events[0]
    asyncio.run(instance.on_processing_start(first))
    if attempt == 2:
        asyncio.run(instance.on_processing_complete(first, "success"))
        asyncio.run(instance.on_processing_start(events[1]))
    _record_sms_start(tmp_path, monkeypatch, attempt=attempt)

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())

    assert restarted_events == []
    receipt = restarted._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "failed"
    assert receipt["retryable"] is False
    assert "envelope" not in receipt


def test_recoverable_initial_sms_result_recovers_as_one_correction(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    asyncio.run(instance.on_processing_start(events[0]))
    _record_sms_start(tmp_path, monkeypatch, attempt=1)
    observe_hosted_tool_call(
        tool_name="inkbox_send_sms",
        args={"to": "+15551112222", "text": ""},
        result=json.dumps({"error": "`text` is required"}),
        task_id="tool-task-99",
        session_id="session-1",
        tool_call_id="sms-attempt-1",
        status="error",
    )

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())

    assert len(restarted_events) == 1
    assert "only correction attempt" in restarted_events[0].text
    assert restarted_events[0].raw_message[
        "_inkbox_hosted_reconciliation_attempt"
    ] == 2


def test_recoverable_correction_result_is_terminal_after_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    asyncio.run(instance.on_processing_start(events[0]))
    asyncio.run(instance.on_processing_complete(events[0], "success"))
    asyncio.run(instance.on_processing_start(events[1]))
    _record_sms_start(tmp_path, monkeypatch, attempt=2)
    observe_hosted_tool_call(
        tool_name="inkbox_send_sms",
        args={"to": "+15551112222", "text": ""},
        result=json.dumps({"error": "`text` is required"}),
        session_id="session-1",
        tool_call_id="sms-attempt-2",
        status="error",
    )

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())

    assert restarted_events == []
    receipt = restarted._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "failed"
    assert receipt["retryable"] is False


def test_hosted_sms_failed_model_turn_still_enqueues_only_correction(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    event = events[0]

    asyncio.run(instance.on_processing_start(event))
    asyncio.run(instance.on_processing_complete(event, "failure"))

    assert len(events) == 2
    assert events[1].raw_message["_inkbox_hosted_reconciliation_attempt"] == 2
    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "queued"


def test_hosted_sms_exact_success_completes_without_correction(tmp_path, monkeypatch):
    _record_sms_result(tmp_path, monkeypatch)
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "id": "action-sms",
        "action": "Send one SMS containing exactly: release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    event = events[0]

    asyncio.run(instance.on_processing_start(event))
    asyncio.run(instance.on_processing_complete(event, "success"))

    assert len(events) == 1
    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "completed"


def test_hosted_sms_missing_then_successful_correction_completes(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    first = events[0]
    asyncio.run(instance.on_processing_start(first))
    asyncio.run(instance.on_processing_complete(first, "success"))
    correction = events[1]

    _record_sms_result(tmp_path, monkeypatch, attempt=2)
    asyncio.run(instance.on_processing_start(correction))
    asyncio.run(instance.on_processing_complete(correction, "success"))

    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "completed"
    assert len(events) == 2


def test_hosted_sms_recoverable_failure_enqueues_one_correction(
    tmp_path,
    monkeypatch,
):
    _record_sms_result(
        tmp_path,
        monkeypatch,
        result={"error": "`text` is required"},
    )
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    first = events[0]

    asyncio.run(instance.on_processing_start(first))
    asyncio.run(instance.on_processing_complete(first, "success"))

    assert len(events) == 2
    assert "deterministic pre-send error" in events[1].text
    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "queued"


@pytest.mark.parametrize(
    ("target", "count"),
    [("+15559990000", 1), ("+15551112222", 2)],
)
def test_hosted_sms_wrong_or_duplicate_attempt_is_terminal(
    tmp_path,
    monkeypatch,
    target,
    count,
):
    _record_sms_result(
        tmp_path,
        monkeypatch,
        target=target,
        count=count,
    )
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    event = events[0]
    asyncio.run(instance.on_processing_start(event))
    asyncio.run(instance.on_processing_complete(event, "success"))

    assert len(events) == 1
    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "failed"
    assert receipt["retryable"] is False

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())
    assert restarted_events == []


def test_hosted_sms_failed_correction_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance, events = _adapter(tmp_path)
    payload = _payload()
    payload["data"]["post_call_action_items"] = [{
        "action": "Text release-ready",
        "status": "open",
    }]
    asyncio.run(instance._on_call_ended(payload))
    first = events[0]
    asyncio.run(instance.on_processing_start(first))
    asyncio.run(instance.on_processing_complete(first, "success"))
    correction = events[1]

    asyncio.run(instance.on_processing_start(correction))
    asyncio.run(instance.on_processing_complete(correction, "success"))

    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "failed"
    assert receipt["retryable"] is False
    assert len(events) == 2


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
    receipt = first._read_hosted_call_registry()["call-1"]
    assert receipt["envelope"]["data"]["call"]["id"] == "call-1"
    assert "transcript" not in receipt["envelope"]["data"]
    assert "memories" not in receipt["envelope"]["data"]["contacts"][0]

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())

    assert len(restarted_events) == 1
    assert "Email the release details" in restarted_events[0].text

    asyncio.run(restarted._catch_up_hosted_call_completions())
    assert len(restarted_events) == 1


def test_running_hosted_completion_recovers_after_adapter_restart(tmp_path):
    first, first_events = _adapter(tmp_path)
    asyncio.run(first._on_call_ended(_payload()))
    asyncio.run(first.on_processing_start(first_events[0]))
    assert first._read_hosted_call_registry()["call-1"]["state"] == "running"

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())

    assert len(restarted_events) == 1


def test_recovery_waits_when_authoritative_transcript_fetch_fails(tmp_path):
    first, first_events = _adapter(tmp_path)
    asyncio.run(first._on_call_ended(_payload()))
    asyncio.run(first.on_processing_start(first_events[0]))

    unsettled, unsettled_events = _adapter(
        tmp_path,
        transcript_error=RuntimeError("transcript endpoint not settled"),
    )
    asyncio.run(unsettled._catch_up_hosted_call_completions())
    assert unsettled_events == []
    assert unsettled._read_hosted_call_registry()["call-1"]["state"] == "running"

    settled, settled_events = _adapter(tmp_path)
    asyncio.run(settled._catch_up_hosted_call_completions())
    assert len(settled_events) == 1


def test_completed_hosted_completion_stays_duplicate_after_restart(tmp_path):
    first, first_events = _adapter(tmp_path)
    asyncio.run(first._on_call_ended(_payload()))
    asyncio.run(first.on_processing_start(first_events[0]))
    asyncio.run(first.on_processing_complete(first_events[0], "success"))

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())

    assert restarted_events == []
    assert "envelope" not in restarted._read_hosted_call_registry()["call-1"]


def test_hosted_completion_catch_up_ignores_invalid_snapshot(tmp_path):
    instance, events = _adapter(tmp_path)
    instance._hosted_call_registry_path.write_text(
        '{"call-1":{"state":"queued","owner_id":"old","envelope":{}}}'
    )

    asyncio.run(instance._catch_up_hosted_call_completions())

    assert events == []


def test_connect_automatically_catches_up_hosted_completions(monkeypatch):
    instance = adapter_mod.InkboxAdapter.__new__(adapter_mod.InkboxAdapter)
    instance._api_key = "agent-key"
    instance._identity_handle = "agent-one"
    instance._voice_stack_invalid_value = ""
    instance._require_signature = False
    instance._signing_key = ""
    instance._host = "127.0.0.1"
    instance._port = 9_876
    instance._webhook_path = "/webhook"
    instance._ws_path = "/phone/media/ws"
    instance._public_url_override = "https://agent.example"
    instance._base_url = ""
    instance._acquire_platform_lock = lambda **_kwargs: True
    instance._release_platform_lock = lambda: None
    instance._cleanup = AsyncMock()
    instance._patch_identity_objects = lambda: None
    instance._mark_connected = lambda: None
    order = []

    async def _catch_up_hosted():
        order.append("hosted")

    async def _catch_up_a2a():
        order.append("a2a")

    instance._catch_up_hosted_call_completions = _catch_up_hosted
    instance._catch_up_a2a_tasks = _catch_up_a2a

    class FakeRunner:
        async def setup(self):
            pass

    class FakeSite:
        async def start(self):
            pass

    monkeypatch.setattr(adapter_mod, "check_inkbox_requirements", lambda: True)
    monkeypatch.setattr(adapter_mod, "Inkbox", lambda **_kwargs: object())
    monkeypatch.setattr(adapter_mod.web, "Application", lambda: types.SimpleNamespace(
        router=types.SimpleNamespace(
            add_get=lambda *_args: None,
            add_post=lambda *_args: None,
        ),
    ))
    monkeypatch.setattr(adapter_mod.web, "AppRunner", lambda _app: FakeRunner())
    monkeypatch.setattr(
        adapter_mod.web,
        "TCPSite",
        lambda _runner, _host, _port: FakeSite(),
    )

    assert asyncio.run(instance.connect()) is True
    assert order == ["hosted", "a2a"]


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
    receipt = instance._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "failed"
    assert receipt["envelope"]["data"]["call"]["id"] == "call-1"

    result = asyncio.run(instance.send(
        "contact-1",
        "Sorry, I encountered an error (RuntimeError).\nqueue failed",
    ))

    assert result.message_id == "suppressed-hosted-post-call-error"
    assert "contact-1" not in instance._hosted_post_call_failure_replies

    restarted, restarted_events = _adapter(tmp_path)
    asyncio.run(restarted._catch_up_hosted_call_completions())
    assert len(restarted_events) == 1

    asyncio.run(restarted.on_processing_start(restarted_events[0]))
    asyncio.run(restarted.on_processing_complete(restarted_events[0], "success"))
    receipt = restarted._read_hosted_call_registry()["call-1"]
    assert receipt["state"] == "completed"
    assert "envelope" not in receipt


def test_hosted_completion_enqueue_failure_allows_webhook_retry(tmp_path):
    instance, _events = _adapter(tmp_path)

    async def _fail(_event):
        raise RuntimeError("queue unavailable")

    instance._enqueue = _fail
    with pytest.raises(RuntimeError, match="queue unavailable"):
        asyncio.run(instance._on_call_ended(_payload()))

    assert instance._read_hosted_call_registry() == {}
