"""Call cleanup retries and per-test ownership without external services."""

import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def cleanup(monkeypatch):
    spec = importlib.util.spec_from_file_location("live_call_cleanup", Path(__file__).parent / "live/conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "AUT_KEY", "synthetic")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return module


def start_fixture(module):
    fixture = module._clean_up_calls_created_by_live_test
    assert fixture._fixture_function_marker.scope == "function"
    fn = fixture.__wrapped__
    return fn(**{name: None for name in inspect.signature(fn).parameters})


def test_watchdog_retries_until_terminal_and_preserves_preexisting_calls(cleanup, monkeypatch):
    old = SimpleNamespace(id="old", status="in_progress", local_phone_number="local")
    new = SimpleNamespace(id="new", status="queued", local_phone_number="local")
    calls, attempts = [old], []

    def hangup(identifier):
        assert identifier == "new"
        attempts.append(identifier)
        if len(attempts) == 1:
            raise RuntimeError("call has no active leg yet")
        if len(attempts) == 3:
            new.status = "completed"

    client = SimpleNamespace(
        calls=SimpleNamespace(list=lambda **kw: list(calls), hangup=hangup, get=lambda _: new),
        phone_numbers=SimpleNamespace(list=lambda: [SimpleNamespace(number="local")]),
    )
    monkeypatch.setattr(cleanup, "_client", lambda _: client)
    monkeypatch.delenv("VOICE_SCENARIO", raising=False)

    class Event:
        polls = 0

        def wait(self, _):
            self.polls += 1
            return self.polls > 4

        def set(self):
            pass

    class Thread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            calls.append(new)
            self.target()

        def join(self, **kw):
            pass

    monkeypatch.setattr(cleanup.threading, "Event", Event)
    monkeypatch.setattr(cleanup.threading, "Thread", Thread)
    run = start_fixture(cleanup)
    next(run)
    assert attempts == ["new"] * 3
    with pytest.raises(StopIteration):
        next(run)
    assert old.status == "in_progress"


def test_teardown_finishes_this_tests_call_before_next_test(cleanup, monkeypatch):
    calls, attempts = [], []
    new = SimpleNamespace(id="new", status="queued", local_phone_number="local")

    def hangup(identifier):
        attempts.append(identifier)
        if len(attempts) == 1:
            raise RuntimeError("call has no active leg yet")
        new.status = "completed"

    client = SimpleNamespace(
        calls=SimpleNamespace(list=lambda **kw: list(calls), hangup=hangup, get=lambda _: new),
        phone_numbers=SimpleNamespace(list=lambda: [SimpleNamespace(number="local")]),
    )
    monkeypatch.setattr(cleanup, "_client", lambda _: client)
    monkeypatch.setenv("VOICE_SCENARIO", "synthetic")
    run = start_fixture(cleanup)
    next(run)
    calls.append(new)
    with pytest.raises(StopIteration):
        next(run)
    assert attempts == ["new", "new"]
    assert new.status == "completed"


def test_cleanup_fails_when_termination_cannot_be_verified(cleanup, monkeypatch):
    call = SimpleNamespace(id="new", status="in_progress", local_phone_number="local")
    client = SimpleNamespace(calls=SimpleNamespace(
        list=lambda **kw: [call], hangup=lambda _: None,
    ))
    clock = iter((0, 13))
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="remained active"):
        cleanup._finish_new_calls(client, "local", set())
