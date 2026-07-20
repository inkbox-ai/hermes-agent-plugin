import json
import sys
import threading
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import reply_guard


class _Entry:
    session_id = "session-123"


class _Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {"inkbox:contact-123": _Entry()}

    def _ensure_loaded(self):
        return None


class _Gateway:
    @staticmethod
    def _session_key_for_source(_source):
        return "inkbox:contact-123"


@pytest.fixture(autouse=True)
def _reset_guard():
    reply_guard._reset_for_tests()
    yield
    reply_guard._reset_for_tests()


def _record_current_route():
    source = types.SimpleNamespace(
        platform=types.SimpleNamespace(value="inkbox"),
        thread_id="imessage:imconv-123",
        user_id_alt="+15555550101",
    )
    event = types.SimpleNamespace(source=source)
    reply_guard.record_inbound_route(
        event=event,
        gateway=_Gateway(),
        session_store=_Store(),
    )


def test_same_thread_explicit_send_suppresses_one_final_response():
    _record_current_route()
    reply_guard.note_imessage_tool_delivery(
        tool_name="inkbox_send_imessage",
        args={"conversationId": "imconv-123", "text": "chart", "mediaPaths": ["/tmp/chart.png"]},
        result=json.dumps({"ok": True, "conversation_id": "imconv-123"}),
        session_id="session-123",
        status="ok",
    )

    assert reply_guard.suppress_duplicate_final(
        response_text="Sent it!",
        session_id="session-123",
        platform="inkbox",
    ) == "[SILENT]"
    assert reply_guard.suppress_duplicate_final(
        response_text="A later reply",
        session_id="session-123",
        platform="inkbox",
    ) is None


def test_different_conversation_keeps_confirmation():
    _record_current_route()
    reply_guard.note_imessage_tool_delivery(
        tool_name="inkbox_send_imessage",
        args={"conversationId": "someone-else", "text": "hello"},
        result=json.dumps({"ok": True, "conversation_id": "someone-else"}),
        session_id="session-123",
        status="ok",
    )

    assert reply_guard.suppress_duplicate_final(
        response_text="I messaged them.",
        session_id="session-123",
        platform="inkbox",
    ) is None


def test_failed_send_and_other_platform_do_not_suppress():
    _record_current_route()
    reply_guard.note_imessage_tool_delivery(
        tool_name="inkbox_send_imessage",
        args={"conversationId": "imconv-123", "text": "hello"},
        result=json.dumps({"error": "not connected"}),
        session_id="session-123",
        status="error",
    )
    assert reply_guard.suppress_duplicate_final(
        response_text="It failed.", session_id="session-123", platform="inkbox"
    ) is None

    reply_guard.note_imessage_tool_delivery(
        tool_name="inkbox_send_imessage",
        args={"conversationId": "imconv-123", "text": "hello"},
        result=json.dumps({"ok": True, "conversation_id": "imconv-123"}),
        session_id="session-123",
        status="ok",
    )
    assert reply_guard.suppress_duplicate_final(
        response_text="CLI output", session_id="session-123", platform="local"
    ) is None
