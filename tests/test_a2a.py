import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
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
