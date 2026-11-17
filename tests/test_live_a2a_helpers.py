"""A caller's request cannot substitute for the worker's result."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = spec_from_file_location("live_a2a_helpers", Path(__file__).parent / "live/a2a_driver.py")
driver = module_from_spec(spec)
spec.loader.exec_module(driver)


def message(role, text):
    return {"role": role, "parts": [{"text": text}]}


def test_final_answer_does_not_echo_caller_or_earlier_progress():
    task = SimpleNamespace(raw={"history": [
        message("user", "expected-code expected-result"),
        message("agent", "Working on expected-code expected-result"),
        message("ROLE_AGENT", "Wrong final result"),
        message("user", "expected-code expected-result"),
    ]})
    assert driver._wire_final_answer(task) == "Wrong final result"


def test_final_answer_requires_an_agent_message():
    task = SimpleNamespace(raw={"history": [message("user", "expected-result")]})
    with pytest.raises(AssertionError, match="no agent answer"):
        driver._wire_final_answer(task)


def test_final_answer_accepts_the_latest_agent_result():
    task = SimpleNamespace(raw={"history": [
        message("user", "Please solve this"),
        message("agent", "Working"),
        message("ROLE_AGENT", "expected-code expected-result"),
    ]})
    assert driver._wire_final_answer(task) == "expected-code expected-result"
