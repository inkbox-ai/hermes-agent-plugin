import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.hosted_call_context import (
    activate_next_hosted_turn_context,
    clear_hosted_call_context,
    enqueue_hosted_turn_context,
    hosted_sms_settlement,
    observe_hosted_tool_call,
    observe_hosted_tool_start,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _activate(*, attempt=1):
    enqueue_hosted_turn_context("session-1", {
        "call_id": "call-1",
        "remote_phone": "+15551112222",
        "sms_required": True,
        "attempt": attempt,
    })
    activate_next_hosted_turn_context("session-1")


def _observe(*, args=None, result=None, status="ok"):
    observe_hosted_tool_call(
        tool_name="inkbox_send_sms",
        args=args or {"to": "+15551112222", "text": "release-ready"},
        result=json.dumps(result or {"ok": True}),
        task_id="session-1",
        status=status,
    )


def test_exact_target_and_positive_result_settle_success():
    _activate()
    _observe()
    assert hosted_sms_settlement("call-1", 1) == "success"


def test_observer_prefers_real_session_id_over_distinct_task_id():
    _activate()
    observe_hosted_tool_call(
        tool_name="inkbox_send_sms",
        args={"to": "+15551112222", "text": "release-ready"},
        result=json.dumps({"ok": True}),
        task_id="tool-task-99",
        session_id="session-1",
        status="ok",
    )
    assert hosted_sms_settlement("call-1", 1) == "success"


def test_pre_and_post_observers_replace_one_attempt_by_tool_call_id():
    _activate()
    observe_hosted_tool_start(
        tool_name="inkbox_send_sms",
        args={"to": "+15551112222", "text": "release-ready"},
        task_id="tool-task-99",
        session_id="session-1",
        tool_call_id="call-tool-1",
    )
    assert hosted_sms_settlement("call-1", 1) == "terminal"

    observe_hosted_tool_call(
        tool_name="inkbox_send_sms",
        args={"to": "+15551112222", "text": "release-ready"},
        result=json.dumps({"ok": True}),
        task_id="tool-task-99",
        session_id="session-1",
        tool_call_id="call-tool-1",
        status="ok",
    )
    assert hosted_sms_settlement("call-1", 1) == "success"


def test_two_pre_and_post_tool_calls_are_terminal_duplicates():
    _activate()
    for tool_call_id in ("call-tool-1", "call-tool-2"):
        observe_hosted_tool_start(
            tool_name="inkbox_send_sms",
            args={"to": "+15551112222", "text": "release-ready"},
            session_id="session-1",
            tool_call_id=tool_call_id,
        )
        observe_hosted_tool_call(
            tool_name="inkbox_send_sms",
            args={"to": "+15551112222", "text": "release-ready"},
            result=json.dumps({"ok": True}),
            session_id="session-1",
            tool_call_id=tool_call_id,
            status="ok",
        )
    assert hosted_sms_settlement("call-1", 1) == "terminal"


def test_missing_wrong_target_and_duplicate_are_not_success():
    _activate()
    assert hosted_sms_settlement("call-1", 1) == "missing"

    _observe(args={"to": "+15559990000", "text": "release-ready"})
    assert hosted_sms_settlement("call-1", 1) == "terminal"

    _observe()
    assert hosted_sms_settlement("call-1", 1) == "terminal"


@pytest.mark.parametrize(
    "result",
    [
        {"error": "`text` is required"},
        {"error": "blocked", "error_code": "message_blocked_spam_filter", "rule": "emoji_overload", "status_code": 422},
        {"error": "too long", "error_code": "sms_too_long"},
    ],
)
def test_deterministic_pre_send_failure_is_recoverable(result):
    _activate()
    _observe(result=result, status="error")
    assert hosted_sms_settlement("call-1", 1) == "recoverable"


@pytest.mark.parametrize(
    "result",
    [
        {"error": "blocked", "error_code": "message_blocked_spam_filter", "rule": "duplicate_body", "status_code": 422},
        {"error": "upstream timeout", "status_code": 502},
        {"error": "rate limited", "status_code": 429},
        {"error": "recipient opted out", "error_code": "recipient_opted_out", "status_code": 403},
        {"error": "temporarily unavailable"},
        {"error": "blocked", "error_code": "message_blocked_spam_filter", "rule": "unanswered_limit", "status_code": 422},
    ],
)
def test_ambiguous_or_terminal_failure_is_not_recoverable(result):
    _activate()
    _observe(result=result, status="error")
    assert hosted_sms_settlement("call-1", 1) == "terminal"


def test_attempts_are_settled_independently():
    _activate(attempt=1)
    _observe(result={"error": "`text` is required"}, status="error")
    assert hosted_sms_settlement("call-1", 1) == "recoverable"

    _activate(attempt=2)
    _observe()
    assert hosted_sms_settlement("call-1", 2) == "success"


def test_terminal_cleanup_prevents_later_session_sms_misattribution():
    _activate()
    clear_hosted_call_context("call-1")
    _observe()
    assert hosted_sms_settlement("call-1", 1) == "missing"


def test_durable_context_uses_private_directory_and_files(tmp_path):
    _activate()
    root = tmp_path / "inkbox_hosted_call_contexts"

    assert root.stat().st_mode & 0o777 == 0o700
    assert list(root.glob("*.json"))
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in root.glob("*.json"))
    assert list(root.glob("*.tmp")) == []
