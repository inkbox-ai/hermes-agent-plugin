import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin import tools as tools_mod
from inkbox_plugin.a2a_context import (
    read_a2a_turn_context,
    write_a2a_turn_context,
)
from inkbox_plugin.adapter import InkboxAdapter


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )


def _adapter(tmp_path):
    adapter = object.__new__(InkboxAdapter)
    adapter._identity_id = "identity-1"
    adapter._identity_handle = "agent"
    adapter._a2a_registry_path = tmp_path / "a2a.json"
    adapter._a2a_tasks_by_chat = {}
    adapter._a2a_session_by_chat = {}
    adapter._a2a_session_key_by_chat = {}
    adapter._a2a_suppress_next_reply_by_chat = set()
    adapter._enqueued = []

    async def enqueue(event):
        adapter._enqueued.append(event)

    adapter._enqueue = enqueue
    return adapter


def _event(event_id="evt-1", event_type="a2a.task.created"):
    return {
        "id": event_id,
        "event_type": event_type,
        "data": {
            "task_id": "task-1",
            "context_id": "context-1",
            "state": "submitted",
            "caller": {
                "identity_id": "caller-1",
                "organization_id": "org-1",
                "handle": "caller",
            },
            "message_id": "message-1",
            "parts": [{"text": "Please investigate."}],
        },
    }


def test_a2a_event_is_persisted_before_enqueue_and_deduplicated(tmp_path):
    adapter = _adapter(tmp_path)

    first = asyncio.run(adapter._on_a2a_event(_event()))
    second = asyncio.run(adapter._on_a2a_event(_event()))

    assert first.text == "ok"
    assert second.text == "duplicate"
    assert len(adapter._enqueued) == 1
    assert adapter._enqueued[0].source.chat_id == "a2a:identity-1:context-1"
    assert adapter._a2a_registry_path.exists()
    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert registry["task-1:message-1"]["state"] == "running"


def test_a2a_cancel_removes_only_the_addressed_task(tmp_path):
    adapter = _adapter(tmp_path)
    chat_id = "a2a:identity-1:context-1"
    adapter._a2a_tasks_by_chat[chat_id] = ["task-1", "task-2"]

    response = asyncio.run(
        adapter._on_a2a_event(_event("evt-cancel", "a2a.task.canceled"))
    )

    assert response.text == "ok"
    assert adapter._a2a_tasks_by_chat[chat_id] == ["task-2"]
    assert adapter._enqueued == []


def test_canceled_task_late_output_cannot_complete_the_next_task(tmp_path):
    adapter = _adapter(tmp_path)
    chat_id = "a2a:identity-1:context-1"
    adapter._a2a_tasks_by_chat[chat_id] = ["task-1", "task-2"]

    asyncio.run(
        adapter._on_a2a_event(_event("evt-cancel", "a2a.task.canceled"))
    )
    result = asyncio.run(adapter._send_a2a_reply(chat_id, "Late result."))

    assert result.success is True
    assert result.message_id == "a2a-canceled"
    assert adapter._a2a_tasks_by_chat[chat_id] == ["task-2"]


def test_default_a2a_reply_completes_oldest_task(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    chat_id = "a2a:identity-1:context-1"
    adapter._a2a_tasks_by_chat[chat_id] = ["task-1", "task-2"]
    replies = []

    class Identity:
        def a2a_reply(self, task_id, **kwargs):
            replies.append((task_id, kwargs))

    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: Identity(),
    )

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", inline)
    result = asyncio.run(adapter._send_a2a_reply(chat_id, "Done."))

    assert result.success is True
    assert replies == [("task-1", {"intent": "complete", "text": "Done."})]
    assert adapter._a2a_tasks_by_chat[chat_id] == ["task-2"]


def test_a2a_intent_tools_require_verified_turn_context(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outside = json.loads(
        tools_mod.inkbox_a2a_complete({"text": "Done."}, task_id="ordinary")
    )
    assert "only available" in outside["error"]

    replies = []
    identity = types.SimpleNamespace(
        a2a_reply=lambda task_id, **kwargs: replies.append((task_id, kwargs))
    )
    monkeypatch.setattr(
        tools_mod,
        "_client_and_identity",
        lambda: (None, None, identity),
    )
    write_a2a_turn_context(
        "session-1",
        {
            "task_id": "task-1",
            "context_id": "context-1",
            "message_id": "message-1",
            "reply_intent_committed": False,
        },
    )

    inside = json.loads(
        tools_mod.inkbox_a2a_ask_caller(
            {"text": "Which region?"},
            task_id="session-1",
        )
    )

    assert inside["ok"] is True
    assert replies == [
        ("task-1", {"intent": "ask_caller", "text": "Which region?"})
    ]
    assert read_a2a_turn_context("session-1")["reply_intent_committed"] is True


def test_a2a_catch_up_resumes_nonfinal_registry_entries(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
    )
    task = types.SimpleNamespace(
        id="task-1",
        context_id="context-1",
        state="working",
        caller=types.SimpleNamespace(
            identity_id="caller-1",
            organization_id="org-1",
            handle="caller",
        ),
        messages=[
            types.SimpleNamespace(
                message_id="message-1",
                parts=[{"text": "Resume this."}],
            )
        ],
    )
    identity = types.SimpleNamespace(
        a2a_task=lambda _task_id: task,
        iter_a2a_tasks=lambda **_kwargs: iter(()),
    )
    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: identity,
    )

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", inline)
    asyncio.run(adapter._catch_up_a2a_tasks())

    assert len(adapter._enqueued) == 1
    assert adapter._enqueued[0].text.endswith("Resume this.")
    assert adapter._a2a_tasks_by_chat[
        "a2a:identity-1:context-1"
    ] == ["task-1"]
