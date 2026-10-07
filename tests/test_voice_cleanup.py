"""Offline proof for bounded, terminal-state-verified live call cleanup."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest


class ControlError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def cleanup():
    path = Path(__file__).parent / "live" / "test_voice.py"
    spec = importlib.util.spec_from_file_location("voice_cleanup_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    module.time = clock = Clock()
    return module, clock


def test_transient_hangup_retries_same_call_only_after_active_read(cleanup):
    module, clock = cleanup
    operations = []

    def hangup(call_id):
        operations.append(("hangup", call_id))
        if len(operations) == 1:
            raise ControlError(502)

    def get(call_id):
        operations.append(("get", call_id))
        return NS(status="answered" if len(operations) == 2 else "completed")

    module._hangup_call(NS(calls=NS(hangup=hangup, get=get)), "same-call")
    assert operations == [("hangup", "same-call"), ("get", "same-call"),
                          ("hangup", "same-call"), ("get", "same-call")]
    assert clock.now == 0.5


@pytest.mark.parametrize("initial_error,status", [(502, "answered"), (403, "answered"), (502, "unknown")])
def test_persistent_or_unverified_cleanup_remains_strict_failure(cleanup, initial_error, status):
    module, clock = cleanup
    calls = NS(hangup=Mock(side_effect=ControlError(initial_error)), get=Mock(return_value=NS(status=status)))
    with pytest.raises(RuntimeError, match="failed to hang up live test call"):
        module._hangup_call(NS(calls=calls), "same-call")
    assert calls.hangup.call_count == (3 if initial_error == 502 and status == "answered" else 1)
    assert all(call.args == ("same-call",) for call in calls.hangup.call_args_list + calls.get.call_args_list)
    assert clock.now == 10.0


def test_successful_control_preserves_sweep_terminal_grace(cleanup):
    module, clock = cleanup

    def get(_call_id):
        return NS(status="completed" if clock.now >= 12 else "answered")

    calls = NS(hangup=Mock(), get=Mock(side_effect=get))
    module._sweep_matching_calls(NS(calls=calls), lambda: [NS(id="same-call", status="answered")])
    assert calls.hangup.call_count == 1
    assert clock.now == 12.0


def test_successful_control_does_not_let_sweep_accept_nonterminal_call(cleanup):
    module, clock = cleanup
    calls = NS(hangup=Mock(), get=Mock(return_value=NS(status="answered")))
    with pytest.raises(pytest.fail.Exception, match="did not end before setup"):
        module._sweep_matching_calls(NS(calls=calls), lambda: [NS(id="same-call", status="answered")])
    assert calls.hangup.call_count == 1
    assert clock.now == 30.0


def test_ended_race_needs_no_second_command(cleanup):
    module, clock = cleanup
    calls = NS(hangup=Mock(side_effect=ControlError(409)), get=Mock(return_value=NS(status="completed")))
    module._hangup_call(NS(calls=calls), "same-call")
    assert calls.hangup.call_count == calls.get.call_count == 1
    assert clock.now == 0


def test_state_read_consuming_deadline_does_not_start_another_command(cleanup):
    module, clock = cleanup

    def get(_call_id):
        clock.now = 10.0
        return NS(status="answered")

    calls = NS(hangup=Mock(side_effect=ControlError(502)), get=get)
    with pytest.raises(RuntimeError, match="status='answered'"):
        module._hangup_call(NS(calls=calls), "same-call")
    assert calls.hangup.call_count == 1
    assert clock.now == 10.0
