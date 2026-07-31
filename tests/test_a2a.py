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
    activate_next_a2a_turn_context,
    enqueue_a2a_turn_context,
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
    adapter.platform = "inkbox"
    adapter._identity_id = "identity-1"
    adapter._identity_handle = "agent"
    adapter._a2a_registry_path = tmp_path / "a2a.json"
    adapter._a2a_tasks_by_chat = {}
    adapter._a2a_events_by_chat = {}
    adapter._a2a_active_chats = set()
    adapter._a2a_default_reply_allowed = {}
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


def test_detects_only_unsupported_a2a_event_validation_errors():
    unsupported = types.SimpleNamespace(
        status_code=422,
        detail="a2a.task.created is not a valid event type",
    )
    unrelated = types.SimpleNamespace(status_code=422, detail="invalid webhook URL")
    local_validation = ValueError(
        "event_type 'a2a.task.created' does not belong to any known channel"
    )

    assert adapter_mod._is_unsupported_a2a_event_types(unsupported)
    assert adapter_mod._is_unsupported_a2a_event_types(local_validation)
    assert not adapter_mod._is_unsupported_a2a_event_types(unrelated)


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
    assert registry["task-1:message-1"]["state"] == "queued"


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


def test_same_context_a2a_events_are_dispatched_one_at_a_time(tmp_path):
    adapter = _adapter(tmp_path)

    asyncio.run(adapter._on_a2a_event(_event()))
    second = _event("evt-2")
    second["data"]["task_id"] = "task-2"
    second["data"]["message_id"] = "message-2"
    asyncio.run(adapter._on_a2a_event(second))

    assert len(adapter._enqueued) == 1
    assert len(adapter._a2a_events_by_chat["a2a:identity-1:context-1"]) == 2

    handed_off = []

    async def handle_message(event):
        handed_off.append(event)

    adapter.handle_message = handle_message
    asyncio.run(
        adapter.on_processing_complete(
            adapter._enqueued[0],
            types.SimpleNamespace(value="success"),
        )
    )

    assert [event.message_id for event in handed_off] == ["message-2"]


def test_failed_a2a_turn_cannot_default_complete(tmp_path):
    adapter = _adapter(tmp_path)
    chat_id = "a2a:identity-1:context-1"
    adapter._a2a_tasks_by_chat[chat_id] = ["task-1"]
    adapter._a2a_default_reply_allowed[chat_id] = False

    result = asyncio.run(adapter._send_a2a_reply(chat_id, "Error text."))

    assert result.success is True
    assert result.message_id == "a2a-no-reply"


def test_a2a_home_channel_notice_does_not_complete_task(tmp_path):
    adapter = _adapter(tmp_path)
    chat_id = "a2a:identity-1:context-1"
    adapter._a2a_tasks_by_chat[chat_id] = ["task-1"]
    adapter._last_inbound_imessage = {}

    result = asyncio.run(
        adapter.send(
            chat_id,
            "📬 No home channel is set for Inkbox. Type /sethome to configure one.",
        )
    )

    assert result.success is True
    assert result.message_id == "suppressed-admin-notice"
    assert adapter._a2a_tasks_by_chat[chat_id] == ["task-1"]


def test_default_a2a_reply_completes_oldest_task(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    chat_id = "a2a:identity-1:context-1"
    adapter._a2a_tasks_by_chat[chat_id] = ["task-1", "task-2"]
    replies = []

    class Identity:
        def a2a_task(self, _task_id):
            return types.SimpleNamespace(state="working")

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


def test_a2a_delegation_tools_send_check_wait_and_reply(monkeypatch):
    clients = []

    class A2A:
        def __init__(self):
            self.calls = []
            self.closed = False
            clients.append(self)

        def fetch_card(self, card_url):
            self.calls.append(("fetch_card", card_url))
            return {"rpc_url": "https://worker.example/a2a"}

        def send(self, target, **kwargs):
            self.calls.append(("send", target, kwargs))
            return {
                "kind": "task",
                "task": {
                    "id": kwargs.get("task_id") or "task-1",
                    "contextId": kwargs.get("context_id") or "context-1",
                },
            }

        def get_task(self, target, task_id):
            self.calls.append(("get_task", target, task_id))
            return {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}

        def wait(self, target, task_id):
            self.calls.append(("wait", target, task_id))
            return {"id": task_id, "status": {"state": "TASK_STATE_COMPLETED"}}

        def close(self):
            self.closed = True

    identity = types.SimpleNamespace(a2a_client=A2A)
    monkeypatch.setattr(
        tools_mod,
        "_client_and_identity",
        lambda: (None, None, identity),
    )

    created = json.loads(
        tools_mod.inkbox_a2a_call({
            "cardUrl": "https://worker.example/card",
            "text": "Investigate this.",
            "contextId": "context-1",
            "messageId": "message-1",
        })
    )
    checked = json.loads(
        tools_mod.inkbox_a2a_check({
            "card_url": "https://worker.example/card",
            "task_id": "task-1",
        })
    )
    waited = json.loads(
        tools_mod.inkbox_a2a_check({
            "cardUrl": "https://worker.example/card",
            "taskId": "task-1",
            "wait": True,
        })
    )
    replied = json.loads(
        tools_mod.inkbox_a2a_reply({
            "cardUrl": "https://worker.example/card",
            "taskId": "task-1",
            "text": "Use the production window.",
            "messageId": "message-2",
        })
    )

    assert created["result"]["task"]["id"] == "task-1"
    assert checked["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert waited["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert replied["result"]["task"]["id"] == "task-1"
    assert clients[0].calls[-1][2] == {
        "text": "Investigate this.",
        "context_id": "context-1",
        "task_id": None,
        "message_id": "message-1",
    }
    assert clients[3].calls[-1][2] == {
        "text": "Use the production window.",
        "task_id": "task-1",
        "message_id": "message-2",
    }
    assert all(client.closed for client in clients)


def test_a2a_delegation_tools_validate_required_fields(monkeypatch):
    monkeypatch.setattr(
        tools_mod,
        "_client_and_identity",
        lambda: pytest.fail("invalid input must not create an SDK client"),
    )

    assert json.loads(tools_mod.inkbox_a2a_call({"text": "Work"})) == {
        "error": "cardUrl is required"
    }
    assert json.loads(
        tools_mod.inkbox_a2a_check({"cardUrl": "https://worker.example/card"})
    ) == {"error": "taskId is required"}
    assert json.loads(
        tools_mod.inkbox_a2a_reply({
            "cardUrl": "https://worker.example/card",
            "taskId": "task-1",
        })
    ) == {"error": "text is required"}


def test_a2a_sent_history_tools_are_scoped_to_configured_identity(
    monkeypatch,
):
    calls = []

    class Identity:
        def a2a_tasks(self, **kwargs):
            calls.append(("history", kwargs))
            return {"items": [], "next_cursor": None}

        def a2a_messages(self, **kwargs):
            calls.append(("messages", kwargs))
            return {"items": [], "next_cursor": None}

        def a2a_sent_tasks(self, **kwargs):
            calls.append(("list", kwargs))
            return {
                "items": [
                    {
                        "id": "task-1",
                        "state": "completed",
                        "target": {"handle": "worker-agent"},
                    }
                ],
                "next_cursor": "next-page",
            }

        def a2a_sent_task(self, task_id):
            calls.append(("get", task_id))
            return {
                "id": task_id,
                "messages": [
                    {"role": "agent", "parts": [{"text": "Done."}]}
                ],
            }

    monkeypatch.setattr(
        tools_mod,
        "_client_and_identity",
        lambda: (None, None, Identity()),
    )

    history = json.loads(
        tools_mod.inkbox_list_a2a_tasks({
            "direction": "both",
            "requesterHandle": "requester",
            "workerHandle": "worker",
            "state": "completed",
            "contextId": "context-1",
            "query": "summary",
            "since": "2026-07-01T00:00:00Z",
            "cursor": "cursor-1",
            "limit": 3,
        })
    )
    messages = json.loads(
        tools_mod.inkbox_list_a2a_messages({
            "direction": "outbound",
            "requesterHandle": "requester",
            "workerHandle": "worker",
            "taskId": "task-1",
            "contextId": "context-1",
            "role": "agent",
            "query": "done",
            "since": "2026-07-01T00:00:00Z",
            "cursor": "cursor-2",
            "limit": 4,
        })
    )
    page = json.loads(
        tools_mod.inkbox_list_a2a_sent_tasks({
            "requesterHandle": "requester",
            "workerHandle": "worker",
            "state": "completed",
            "contextId": "context-1",
            "query": "summary",
            "since": "2026-07-01T00:00:00Z",
            "cursor": "cursor-1",
            "limit": 3,
        })
    )
    task = json.loads(
        tools_mod.inkbox_get_a2a_sent_task({"taskId": "task-1"})
    )

    assert history["page"]["items"] == []
    assert messages["page"]["items"] == []
    assert page["page"]["next_cursor"] == "next-page"
    assert page["page"]["items"][0]["target"]["handle"] == "worker-agent"
    assert task["task"]["messages"][0]["parts"][0]["text"] == "Done."
    assert calls == [
        (
            "history",
            {
                "direction": "both",
                "requester_handle": "requester",
                "worker_handle": "worker",
                "state": "completed",
                "context_id": "context-1",
                "q": "summary",
                "since": "2026-07-01T00:00:00Z",
                "cursor": "cursor-1",
                "limit": 3,
            },
        ),
        (
            "messages",
            {
                "direction": "outbound",
                "requester_handle": "requester",
                "worker_handle": "worker",
                "task_id": "task-1",
                "context_id": "context-1",
                "role": "agent",
                "q": "done",
                "since": "2026-07-01T00:00:00Z",
                "cursor": "cursor-2",
                "limit": 4,
            },
        ),
        (
            "list",
            {
                "requester_handle": "requester",
                "worker_handle": "worker",
                "state": "completed",
                "context_id": "context-1",
                "q": "summary",
                "since": "2026-07-01T00:00:00Z",
                "cursor": "cursor-1",
                "limit": 3,
            },
        ),
        ("get", "task-1"),
    ]


def test_a2a_sent_history_rejects_unbounded_page_and_missing_task_id(
    monkeypatch,
):
    monkeypatch.setattr(
        tools_mod,
        "_client_and_identity",
        lambda: (
            None,
            None,
            types.SimpleNamespace(a2a_sent_tasks=lambda **_kwargs: {}),
        ),
    )

    page = json.loads(
        tools_mod.inkbox_list_a2a_sent_tasks({"limit": 101})
    )
    history = json.loads(tools_mod.inkbox_list_a2a_tasks({"limit": 0}))
    messages = json.loads(tools_mod.inkbox_list_a2a_messages({"limit": 101}))
    task = json.loads(tools_mod.inkbox_get_a2a_sent_task({}))

    assert page["error"] == "limit must be between 1 and 100"
    assert history["error"] == "limit must be between 1 and 100"
    assert messages["error"] == "limit must be between 1 and 100"
    assert task["error"] == "taskId is required"


def test_a2a_context_activates_in_hermes_turn_order(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    enqueue_a2a_turn_context(
        "session-1",
        {
            "task_id": "task-1",
            "context_id": "context-1",
            "message_id": "message-1",
            "reply_intent_committed": False,
        },
    )
    enqueue_a2a_turn_context(
        "session-1",
        {
            "task_id": "task-2",
            "context_id": "context-1",
            "message_id": "message-2",
            "reply_intent_committed": False,
        },
    )

    first = activate_next_a2a_turn_context("session-1")
    second = activate_next_a2a_turn_context("session-1")

    assert first["task_id"] == "task-1"
    assert second["task_id"] == "task-2"
    assert read_a2a_turn_context("session-1")["task_id"] == "task-2"


def test_a2a_turn_context_uses_host_session_store_and_gateway_tool_task_key(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(tmp_path)
    adapter._session_store = types.SimpleNamespace(
        get_or_create_session=lambda _source: types.SimpleNamespace(
            session_id="raw-session-id",
            session_key="gateway-session-key",
        )
    )
    adapter._message_handler = lambda _event: None
    source = types.SimpleNamespace(chat_id="a2a:identity-1:context-1")

    adapter._bind_a2a_turn_context(
        source,
        task_id="task-1",
        context_id="context-1",
        message_id="message-1",
    )

    assert adapter._a2a_session_by_chat[source.chat_id] == "raw-session-id"
    assert (
        adapter._a2a_session_key_by_chat[source.chat_id]
        == "gateway-session-key"
    )
    activated = activate_next_a2a_turn_context("gateway-session-key")
    assert activated["task_id"] == "task-1"


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


def test_a2a_catch_up_skips_sdk_without_task_inbox(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: types.SimpleNamespace(),
    )

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", inline)
    asyncio.run(adapter._catch_up_a2a_tasks())

    assert adapter._enqueued == []
