import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.live import test_voice


class _Calls:
    def __init__(self, segments):
        self._segments = segments
        self.hung_up = []

    def transcripts(self, _call_id):
        return self._segments

    def hangup(self, call_id):
        self.hung_up.append(call_id)


def _segment(party: str, text: str):
    return SimpleNamespace(party=party, text=text)


def _call(call_id: str, created_at: datetime):
    return SimpleNamespace(id=call_id, created_at=created_at)


def test_two_way_proof_returns_aut_local_speech():
    aut = SimpleNamespace(
        calls=_Calls(
            [
                _segment("remote", "driver request"),
                _segment("local", "agent reply"),
            ]
        )
    )

    assert test_voice._wait_for_two_way_call(aut, "unused", "aut-call") == "agent reply"


def test_driver_proof_requires_driver_local_speech():
    driver = SimpleNamespace(
        calls=_Calls(
            [
                _segment("local", "driver request"),
                _segment("remote", "agent reply"),
            ]
        )
    )

    assert (
        test_voice._wait_for_driver_local_speech(
            driver,
            "unused",
            "driver-call",
            deadline=test_voice.time.monotonic() + 1,
        )
        == "driver request"
    )


def test_fresh_pair_requires_one_call_per_owner_and_close_timestamps(monkeypatch):
    monkeypatch.setattr(test_voice, "POLL_EVERY_S", 0)
    created_at = datetime.now(timezone.utc)
    driver = _call("driver-new", created_at)
    aut = _call("aut-new", created_at)

    assert test_voice._wait_for_fresh_call_pair(
        lambda: [driver],
        lambda: [aut],
        {"driver-old"},
        {"aut-old"},
        not_before=created_at,
        deadline=test_voice.time.monotonic() + 1,
        label="test",
    ) == (driver, aut)


def test_fresh_pair_rejects_ambiguous_owner_records(monkeypatch):
    monkeypatch.setattr(test_voice, "POLL_EVERY_S", 0)
    created_at = datetime.now(timezone.utc)

    with pytest.raises(AssertionError, match="duplicate driver"):
        test_voice._wait_for_fresh_call_pair(
            lambda: [_call("driver-a", created_at), _call("driver-b", created_at)],
            lambda: [_call("aut-a", created_at)],
            set(),
            set(),
            not_before=created_at,
            deadline=test_voice.time.monotonic() + 1,
            label="test",
        )


def test_fresh_pair_ignores_old_records_that_arrive_after_snapshot(monkeypatch):
    monkeypatch.setattr(test_voice, "POLL_EVERY_S", 0)
    request_time = datetime.now(timezone.utc)
    old_created = request_time - test_voice.timedelta(minutes=5)
    current_created = request_time + test_voice.timedelta(seconds=1)
    old_driver = _call("late-old-driver", old_created)
    current_driver = _call("current-driver", current_created)
    current_aut = _call("current-aut", current_created)

    assert test_voice._wait_for_fresh_call_pair(
        lambda: [old_driver, current_driver],
        lambda: [current_aut],
        set(),
        set(),
        not_before=request_time,
        deadline=test_voice.time.monotonic() + 1,
        label="test",
    ) == (current_driver, current_aut)


def test_cleanup_ends_every_call_created_after_snapshot():
    calls = _Calls([])
    client = SimpleNamespace(calls=calls)
    old = SimpleNamespace(id="old")
    new_a = SimpleNamespace(id="new-a")
    new_b = SimpleNamespace(id="new-b")

    test_voice._hangup_fresh_calls(client, lambda: [old, new_a, new_b], {"old"})

    assert calls.hung_up == ["new-a", "new-b"]


def test_pretest_sweep_ends_matching_calls():
    calls = _Calls([])
    calls.get = lambda call_id: SimpleNamespace(id=call_id, status="completed")
    client = SimpleNamespace(calls=calls)
    old_a = SimpleNamespace(id="old-a", status="answered")
    old_b = SimpleNamespace(id="old-b", status="ringing")

    test_voice._sweep_matching_calls(client, lambda: [old_a, old_b])

    assert calls.hung_up == ["old-a", "old-b"]


def test_inbound_voice_sweeps_both_call_owners_before_placement():
    lines = inspect.getsource(test_voice.test_inbound_call_inkbox_tts_stt)

    assert lines.index("_sweep_matching_calls(remote, _driver_outbound)") < lines.index(
        "call = remote.calls.place("
    )
    assert lines.index("_sweep_matching_calls(aut, _aut_inbound)") < lines.index(
        "call = remote.calls.place("
    )
