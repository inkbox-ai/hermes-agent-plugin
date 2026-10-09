import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
import sys
import types
from pathlib import Path
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("inkbox_plugin", pkg)
from inkbox_plugin import send_outcome as subject


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("INKBOX_SEND_POLL_SECONDS", "0.18")
    monkeypatch.setenv("INKBOX_SEND_POLL_INTERVAL_SECONDS", "0.05")


@pytest.mark.parametrize("kind,service,status,group,final", [
    ("imessage", "imessage", "sent", False, False),
    ("imessage", "rcs", "delivered", False, True),
    ("imessage", "sms", "sent", False, True),
    ("imessage", "imessage", "sent", True, True),
    ("imessage", "imessage", "error", False, True),
    ("sms", None, "sent", False, False),
    ("sms", None, "delivery_failed", True, True),
    ("sms", None, "delivery_unconfirmed", False, True),
])
def test_finality(kind, service, status, group, final):
    result = subject.outcome({"status": status, "service": service}, kind, group=group)
    assert result["delivery_final"] is final
    if status == "delivery_unconfirmed":
        assert "unknown" in result["note"]
        assert "Do not resend" in result["note"]


def test_server_flag_and_pending_transport():
    result = subject.outcome({"status": "pending", "service": "imessage", "delivery_final": False}, "imessage")
    assert result["service"] is None
    assert not result["delivery_final"]
    assert not subject.outcome({"status": "sent", "service": "sms", "delivery_final": False}, "imessage")["delivery_final"]


def test_poll_transitions_and_stops_early():
    reads = []
    def get(message_id):
        reads.append(message_id)
        return {"status": "sent", "service": "sms"}
    client = SimpleNamespace(imessages=SimpleNamespace(get=get))
    result = subject.poll_send_outcome(client, None, "imessage", {"id": "message", "status": "pending"})
    assert result["delivery_final"]
    assert reads == ["message"]
    assert "device delivery receipt" in result["note"]


def test_final_skips_get_and_failure_marks_inline():
    def get(_):
        pytest.fail("final sends must not be polled")
    client = SimpleNamespace(imessages=SimpleNamespace(get=get))
    result = subject.poll_send_outcome(client, None, "imessage", {"id": "failed", "status": "error", "error_detail": "Unavailable"})
    assert result["error_detail"] == "Unavailable"
    assert asyncio.run(subject.reported_inline("failed"))


def test_get_error_keeps_last_state():
    calls = 0
    def get(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("read unavailable")
        return {"status": "sent", "service": "rcs"}
    result = subject.poll_send_outcome(SimpleNamespace(imessages=SimpleNamespace(get=get)), None, "imessage", {"id": "message", "status": "pending"})
    assert result["status"] == "sent"
    assert result["service"] == "rcs"
    assert not result["delivery_final"]


def test_slow_get_cannot_overrun_budget():
    release = threading.Event()
    calls = []
    def get(_):
        calls.append(1)
        release.wait(2)
        return {"status": "delivered"}
    start = time.monotonic()
    try:
        result = subject.poll_send_outcome(None, SimpleNamespace(get_text=get), "sms", {"id": "slow", "delivery_status": "queued"})
        assert time.monotonic() - start < 0.7
        assert result["status"] == "queued"
        assert calls == [1]
    finally:
        release.set()


def test_missing_sdk_get_keeps_accepted_state():
    result = subject.poll_send_outcome(None, None, "imessage", {"id": "old-sdk", "status": "pending"})
    assert not result["delivery_final"]
    assert "do not resend" in result["note"]


def test_webhook_waits_for_inline_observation():
    async def run():
        subject._mark("race", "polling")
        waiting = asyncio.create_task(subject.reported_inline("race"))
        await asyncio.sleep(0.01)
        assert not waiting.done()
        subject._mark("race", "inline")
        assert await waiting
    asyncio.run(run())


def test_settings_are_bounded(monkeypatch):
    monkeypatch.setenv("KNOB", "nan")
    assert subject._setting("KNOB", 5, 10) == 5
    monkeypatch.setenv("KNOB", "99999")
    assert subject._setting("KNOB", 5, 10) == 10


def test_webhook_first_does_not_start_another_retry():
    assert not asyncio.run(subject.reported_inline("first"))
    result = subject.poll_send_outcome(None, None, "imessage", {"id": "first", "status": "error"})
    assert "do not start another retry" in result["note"]


def test_queued_transport_and_mms_are_preserved():
    assert subject.outcome({"status": "queued", "service": "sms"}, "imessage")["service"] == "sms"
    assert subject.outcome({"delivery_status": "sent", "type": "mms"}, "sms")["service"] == "mms"


def test_read_count_is_bounded(monkeypatch):
    monkeypatch.setenv("INKBOX_SEND_POLL_SECONDS", "10")
    monkeypatch.setenv("INKBOX_SEND_POLL_INTERVAL_SECONDS", "0.05")
    tick = [0.0]
    monkeypatch.setattr(subject, "time", SimpleNamespace(monotonic=lambda: tick[0], sleep=lambda seconds: tick.__setitem__(0, tick[0] + seconds)))
    monkeypatch.setattr(subject, "_mark", lambda *_: None)
    reads = []
    monkeypatch.setattr(subject, "_read_with_deadline", lambda *args: reads.append(args) or {"status": "queued"})
    result = subject.poll_send_outcome(None, SimpleNamespace(get_text=lambda _: None), "sms", {"id": "count", "status": "queued"})
    assert not result["delivery_final"]
    assert len(reads) == 20


def test_repeated_observation_cannot_erase_inline_report():
    subject._mark("repeat", "inline")
    subject.poll_send_outcome(None, None, "sms", {"id": "repeat", "status": "queued"})
    assert asyncio.run(subject.reported_inline("repeat"))


def test_ambiguous_send_is_final_but_unknown_and_must_not_be_resent():
    result = subject.outcome({"status": "error", "error_code": "send_outcome_ambiguous", "error_message": "Delivery could not be confirmed."}, "imessage")
    assert result["delivery_final"]
    assert result["error_detail"] == "Delivery could not be confirmed."
    assert "unknown; do not resend" in result["note"]
    assert "Delivery failed" not in result["note"]


def test_rejection_preserves_explanation_from_error_message():
    result = subject.outcome({"status": "error", "error_code": "message_send_rejected", "error_message": "Message content could not be sent."}, "imessage")
    assert result["error_detail"] == "Message content could not be sent."
    assert "Message content could not be sent." in result["note"]
