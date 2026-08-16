import asyncio
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

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin import a2a_progress as progress_mod
from inkbox_plugin import tools as tools_mod
from inkbox_plugin.a2a_context import (
    activate_next_a2a_turn_context,
    enqueue_a2a_turn_context,
    read_a2a_turn_context,
    write_a2a_turn_context,
)
from inkbox_plugin.adapter import InkboxAdapter


@pytest.fixture(autouse=True)
def fake_web(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    progress_mod._FENCED_TASKS.clear()
    progress_mod._FENCE_OWNER_BY_TASK.clear()
    progress_mod._DELIVERIES_BY_TASK.clear()
    progress_mod._TOOL_NAMES_BY_TASK.clear()
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
    adapter._a2a_progress_tasks = {}
    adapter._a2a_progress_stop_events = {}
    adapter._a2a_progress_interval_seconds = 0
    adapter._a2a_progress_llm = None
    adapter._a2a_ingest_lock = asyncio.Lock()
    adapter._enqueued = []
    adapter._a2a_receipts = []

    task = types.SimpleNamespace(state="submitted", messages=[])
    adapter._a2a_authoritative_task = task

    class Identity:
        def a2a_task(self, _task_id):
            return task

        def a2a_reply(self, task_id, **kwargs):
            adapter._a2a_receipts.append((task_id, kwargs))
            if kwargs.get("intent") == "progress":
                task.state = "working"
            task.messages.append(types.SimpleNamespace(
                role="ROLE_AGENT",
                parts=[{"text": kwargs["text"]}],
            ))

    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: Identity(),
    )
    adapter._bind_a2a_turn_context = lambda *_args, **_kwargs: True

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
    entry = registry["task-1:message-1"]
    assert entry["state"] == "enqueued"
    assert list(entry["phases"]) == [
        "acknowledged",
        "bound",
        "enqueued",
        "received",
    ]
    assert adapter._a2a_receipts == [(
        "task-1",
        {
            "intent": "progress",
            "text": (
                "Task task-1 received. Work is queued and starting. "
                "Periodic progress updates are disabled."
            ),
        },
    )]


@pytest.mark.parametrize(
    "stopped_state",
    [
        "completed",
        "failed",
        "canceled",
        "rejected",
        "input_required",
        "auth_required",
    ],
)
def test_stale_stopped_a2a_webhook_never_enqueues_model(
    tmp_path,
    stopped_state,
):
    adapter = _adapter(tmp_path)
    adapter._a2a_authoritative_task.state = stopped_state

    response = asyncio.run(adapter._on_a2a_event(_event()))

    assert response.status == 200
    assert response.text == "stopped"
    assert adapter._enqueued == []
    assert adapter._a2a_tasks_by_chat == {}
    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert registry["task-1:message-1"]["state"] == "finalized"
    assert registry["task-1:message-1"]["outcome"] == stopped_state


@pytest.mark.parametrize(
    ("interval", "expectation"),
    [
        (180, "Expect progress updates about every 3 minutes."),
        (60, "Expect progress updates about every 1 minute."),
        (1, "Expect progress updates about every 1 second."),
        (0.5, "Expect progress updates about every 0.5 seconds."),
        (0, "Periodic progress updates are disabled."),
    ],
)
def test_a2a_receipt_reports_configured_progress_frequency(
    tmp_path,
    interval,
    expectation,
):
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = interval

    response = asyncio.run(adapter._on_a2a_event(_event()))

    assert response.status == 200
    receipt = adapter._a2a_receipts[0][1]["text"]
    assert receipt.startswith("Task task-1 received. Work is queued and starting.")
    assert receipt.endswith(expectation)


def test_concurrent_duplicate_a2a_delivery_sends_one_receipt(tmp_path):
    adapter = _adapter(tmp_path)

    async def deliver_duplicates():
        return await asyncio.gather(
            adapter._on_a2a_event(_event("evt-1")),
            adapter._on_a2a_event(_event("evt-2")),
        )

    responses = asyncio.run(deliver_duplicates())

    assert {response.text for response in responses} == {"ok", "duplicate"}
    assert len(adapter._enqueued) == 1
    assert len(adapter._a2a_receipts) == 1


def test_a2a_bind_failure_is_retryable_before_enqueue(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._bind_a2a_turn_context = lambda *_args, **_kwargs: False

    failed = asyncio.run(adapter._on_a2a_event(_event()))

    assert failed.status == 503
    assert adapter._enqueued == []
    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert registry["task-1:message-1"]["last_error"] == {
        "at": registry["task-1:message-1"]["last_error"]["at"],
        "code": "session_unavailable",
        "phase": "binding",
    }

    adapter._bind_a2a_turn_context = lambda *_args, **_kwargs: True
    retried = asyncio.run(adapter._on_a2a_event(_event("evt-retry")))

    assert retried.status == 200
    assert len(adapter._enqueued) == 1
    assert len(adapter._a2a_receipts) == 1


def test_a2a_enqueue_failure_rolls_back_and_retries(tmp_path):
    adapter = _adapter(tmp_path)
    attempts = 0

    async def flaky_enqueue(event):
        nonlocal attempts
        attempts += 1
        registry = json.loads(adapter._a2a_registry_path.read_text())
        assert "acknowledged" in registry["task-1:message-1"]["phases"]
        if attempts == 1:
            raise RuntimeError("queue unavailable")
        adapter._enqueued.append(event)

    adapter._enqueue = flaky_enqueue

    failed = asyncio.run(adapter._on_a2a_event(_event()))

    assert failed.status == 503
    assert adapter._a2a_tasks_by_chat == {}
    assert adapter._a2a_events_by_chat == {}
    assert adapter._a2a_active_chats == set()

    retried = asyncio.run(adapter._on_a2a_event(_event("evt-retry")))

    assert retried.status == 200
    assert attempts == 2
    assert len(adapter._enqueued) == 1
    assert len(adapter._a2a_receipts) == 1


def test_a2a_enqueue_cancellation_rolls_back_before_propagating(tmp_path):
    adapter = _adapter(tmp_path)

    async def canceled_enqueue(_event):
        raise asyncio.CancelledError

    adapter._enqueue = canceled_enqueue

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter._on_a2a_event(_event()))

    assert adapter._a2a_tasks_by_chat == {}
    assert adapter._a2a_events_by_chat == {}
    assert adapter._a2a_active_chats == set()
    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert registry["task-1:message-1"]["last_error"]["code"] == (
        "queue_activation_canceled"
    )


def test_a2a_receipt_failure_retries_without_reenqueue(tmp_path):
    adapter = _adapter(tmp_path)
    attempts = 0

    class Identity:
        task = types.SimpleNamespace(state="submitted", messages=[])

        def a2a_task(self, _task_id):
            return self.task

        def a2a_reply(self, task_id, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("receipt unavailable")
            adapter._a2a_receipts.append((task_id, kwargs))

    identity = Identity()
    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: identity,
    )

    failed = asyncio.run(adapter._on_a2a_event(_event()))

    assert adapter._enqueued == []

    retried = asyncio.run(adapter._on_a2a_event(_event("evt-retry")))

    assert failed.status == 503
    assert retried.status == 200
    assert len(adapter._enqueued) == 1
    assert attempts == 2
    assert len(adapter._a2a_receipts) == 1


def test_a2a_receipt_is_idempotent_when_response_is_lost(tmp_path):
    adapter = _adapter(tmp_path)
    attempts = 0
    receipt = (
        "Task task-1 received. Work is queued and starting. "
        "Periodic progress updates are disabled."
    )
    task = types.SimpleNamespace(state="submitted", messages=[])

    class Identity:
        def a2a_task(self, _task_id):
            return task

        def a2a_reply(self, _task_id, **_kwargs):
            nonlocal attempts
            attempts += 1
            task.messages.append(types.SimpleNamespace(
                role="agent",
                parts=[{"text": receipt}],
            ))
            raise RuntimeError("response lost")

    identity = Identity()
    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: identity,
    )

    failed = asyncio.run(adapter._on_a2a_event(_event()))
    retried = asyncio.run(adapter._on_a2a_event(_event("evt-retry")))

    assert failed.status == 503
    assert retried.status == 200
    assert len(adapter._enqueued) == 1
    assert attempts == 1
    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert registry["task-1:message-1"]["state"] == "enqueued"


def test_a2a_caller_cannot_spoof_delivered_receipt(tmp_path):
    adapter = _adapter(tmp_path)
    receipt = (
        "Task task-1 received. Work is queued and starting. "
        "Periodic progress updates are disabled."
    )
    adapter._a2a_authoritative_task.messages.append(
        types.SimpleNamespace(
            role="caller",
            parts=[{"text": receipt}],
        )
    )

    response = asyncio.run(adapter._on_a2a_event(_event()))

    assert response.status == 200
    assert adapter._a2a_receipts == [(
        "task-1",
        {"intent": "progress", "text": receipt},
    )]


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


def test_a2a_running_and_turn_failure_are_durable(tmp_path):
    adapter = _adapter(tmp_path)
    asyncio.run(adapter._on_a2a_event(_event()))
    event = adapter._enqueued[0]

    asyncio.run(adapter.on_processing_start(event))

    registry = json.loads(adapter._a2a_registry_path.read_text())
    entry = registry["task-1:message-1"]
    assert entry["state"] == "running"
    assert "running" in entry["phases"]

    asyncio.run(adapter.on_processing_complete(
        event,
        types.SimpleNamespace(value="failure"),
    ))

    registry = json.loads(adapter._a2a_registry_path.read_text())
    entry = registry["task-1:message-1"]
    assert entry["state"] == "turn_failed"
    assert entry["last_error"]["phase"] == "execution"
    assert entry["last_error"]["code"] == "turn_failed"


def test_a2a_progress_summary_uses_auxiliary_task_and_safe_tool_names():
    calls = []

    class Llm:
        async def acomplete(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return types.SimpleNamespace(
                text="I'm reviewing the worker lifecycle and validating its behavior."
            )

    update = asyncio.run(progress_mod.build_a2a_progress_update(
        Llm(),
        task_text="Inspect the worker implementation.",
        tool_names=["read_file", "run_tests"],
    ))

    assert update == (
        "I'm reviewing the worker lifecycle and validating its behavior."
    )
    messages, kwargs = calls[0]
    assert "Inspect the worker implementation." in messages[1]["content"]
    assert "read_file; run_tests" in messages[1]["content"]
    assert "Do not copy the previous update's wording" in messages[0]["content"]
    assert kwargs == {
        "task": "inkbox_a2a_progress",
        "purpose": "inbound_a2a_progress",
        "temperature": 0.1,
        "max_tokens": 64,
        "timeout": 10,
    }


@pytest.mark.parametrize(
    "claim",
    [
        "Done — the task is complete.",
        "The final result is ready.",
        "I am waiting for your input.",
    ],
)
def test_a2a_progress_summary_rejects_terminal_claim(claim):
    class Llm:
        async def acomplete(self, _messages, **_kwargs):
            return types.SimpleNamespace(text=claim)

    update = asyncio.run(progress_mod.build_a2a_progress_update(
        Llm(),
        task_text="Inspect the worker implementation.",
        tool_names=["run_tests"],
    ))

    assert update == "I'm continuing the requested work."


def test_a2a_progress_summary_rejects_echoed_tool_identifier():
    class Llm:
        async def acomplete(self, _messages, **_kwargs):
            return types.SimpleNamespace(text="I'm using browser search to investigate.")

    update = asyncio.run(progress_mod.build_a2a_progress_update(
        Llm(),
        task_text="Research the requested topic.",
        tool_names=["browser_search"],
    ))

    assert update == "I'm continuing the requested work."


def test_a2a_progress_summary_keeps_nonterminal_intermediate_status():
    assert progress_mod._clean_update(
        "The first calculation is ready, and I'm continuing with the second.",
        [],
    ) == "The first calculation is ready, and I'm continuing with the second."


def test_a2a_progress_normalizes_tool_names_without_classifying_them():
    assert progress_mod._safe_tool_name(" List Directory Users ") == "list_directory_users"
    assert progress_mod._safe_tool_name("run/sql query\n") == "run_sql_query"
    assert progress_mod._fallback_update() == "I'm continuing the requested work."


def test_a2a_progress_summary_enforces_short_word_limit():
    class Llm:
        async def acomplete(self, _messages, **_kwargs):
            return types.SimpleNamespace(
                text=(
                    "I am carefully reviewing all of the requested records while also "
                    "checking the relevant data and preparing a concise summary for you."
                )
            )

    update = asyncio.run(progress_mod.build_a2a_progress_update(
        Llm(),
        task_text="Review the requested records.",
        tool_names=["list_directory_users"],
    ))

    assert len(update.split()) == progress_mod.A2A_PROGRESS_MAX_WORDS
    assert update.endswith("…")


def test_a2a_progress_tool_capture_does_not_retain_inputs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    progress_mod.start_a2a_progress("task-1")
    write_a2a_turn_context(
        "session-1",
        {
            "task_id": "task-1",
            "context_id": "context-1",
            "message_id": "message-1",
            "reply_intent_committed": False,
        },
    )

    progress_mod.observe_a2a_tool_start(
        tool_name="browser_search",
        args={"query": "private-value"},
        task_id="session-1",
        session_id="session-1",
    )

    snapshot = progress_mod.a2a_tool_snapshot("task-1")
    progress_mod.stop_a2a_progress("task-1")
    assert snapshot == ["browser_search"]
    assert "private-value" not in json.dumps(snapshot)


def test_a2a_progress_update_is_durable_and_nonterminal(tmp_path):
    adapter = _adapter(tmp_path)
    asyncio.run(adapter._on_a2a_event(_event()))
    adapter._a2a_progress_llm = types.SimpleNamespace(
        acomplete=lambda *_args, **_kwargs: None,
    )
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
    )
    progress_mod.start_a2a_progress("task-1")

    keep_running = asyncio.run(adapter._emit_a2a_progress_update(
        task_id="task-1",
        message_id="message-1",
    ))

    assert keep_running is True
    assert adapter._a2a_receipts[-1][0] == "task-1"
    assert adapter._a2a_receipts[-1][1]["intent"] == "progress"
    assert "continuing the requested work" in adapter._a2a_receipts[-1][1]["text"]
    registry = json.loads(adapter._a2a_registry_path.read_text())
    progress = registry["task-1:message-1"]["progress"]
    assert progress["delivered_count"] == 1
    assert "pending" not in progress
    assert registry["task-1:message-1"]["state"] == "running"


def test_a2a_progress_retry_recovers_accepted_reply_without_duplicate(tmp_path):
    adapter = _adapter(tmp_path)
    asyncio.run(adapter._on_a2a_event(_event()))
    update = "I'm validating the work. (60s elapsed)"
    adapter._a2a_authoritative_task.messages.append(
        types.SimpleNamespace(role="ROLE_AGENT", parts=[{"text": update}])
    )
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
        progress_text=update,
    )
    receipt_count = len(adapter._a2a_receipts)

    keep_running = asyncio.run(adapter._emit_a2a_progress_update(
        task_id="task-1",
        message_id="message-1",
    ))

    assert keep_running is True
    assert len(adapter._a2a_receipts) == receipt_count
    registry = json.loads(adapter._a2a_registry_path.read_text())
    progress = registry["task-1:message-1"]["progress"]
    assert progress["last_delivered_text"] == update
    assert "pending" not in progress


def test_a2a_caller_cannot_spoof_pending_progress_delivery(tmp_path):
    adapter = _adapter(tmp_path)
    update = "I'm validating the work. (60s elapsed)"
    adapter._a2a_authoritative_task.messages.append(
        types.SimpleNamespace(role="caller", parts=[{"text": update}])
    )
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
        progress_text=update,
    )

    asyncio.run(adapter._emit_a2a_progress_update(
        task_id="task-1",
        message_id="message-1",
    ))

    assert adapter._a2a_receipts == [(
        "task-1",
        {"intent": "progress", "text": update},
    )]


def test_a2a_progress_elapsed_time_continues_across_caller_follow_up(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
    )
    first = json.loads(adapter._a2a_registry_path.read_text())
    started_at = first["task-1:message-1"]["progress"]["started_at"]
    follow_up = _event()["data"] | {"message_id": "message-2"}

    adapter._write_a2a_registry(
        "task-1:message-2",
        follow_up,
        "running",
        progress_started=True,
    )

    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert registry["task-1:message-2"]["progress"]["started_at"] == started_at


def test_a2a_progress_pending_moves_to_follow_up_without_duplication(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    first = _event()["data"]
    update = "I'm validating the work. (59s elapsed)"
    adapter._write_a2a_registry(
        "task-1:message-1",
        first,
        "running",
        progress_started=True,
        progress_text=update,
    )
    follow_up = first | {"message_id": "message-2"}
    adapter._write_a2a_registry(
        "task-1:message-2",
        follow_up,
        "running",
        progress_started=True,
    )
    adapter._a2a_authoritative_task.messages.append(
        types.SimpleNamespace(role="ROLE_AGENT", parts=[{"text": update}])
    )
    original_emit = adapter._emit_a2a_progress_update

    async def no_sleep(_delay):
        pytest.fail("inherited pending progress must reconcile before sleeping")

    async def reconcile_once(**kwargs):
        result = await original_emit(**kwargs)
        assert result is True
        return False

    adapter._emit_a2a_progress_update = reconcile_once
    monkeypatch.setattr(adapter_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        adapter_mod,
        "build_a2a_progress_update",
        lambda *_args, **_kwargs: pytest.fail(
            "inherited pending text must not be regenerated"
        ),
    )

    asyncio.run(adapter._run_a2a_progress_updates(
        task_id="task-1",
        message_id="message-2",
    ))

    registry = json.loads(adapter._a2a_registry_path.read_text())
    progress = registry["task-1:message-2"]["progress"]
    assert progress["delivered_count"] == 1
    assert progress["last_delivered_text"] == update
    assert "pending" not in progress


def test_a2a_progress_stops_for_terminal_task(tmp_path):
    adapter = _adapter(tmp_path)
    asyncio.run(adapter._on_a2a_event(_event()))
    adapter._a2a_authoritative_task.state = "completed"
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
        progress_text="I'm validating the work. (60s elapsed)",
    )
    receipt_count = len(adapter._a2a_receipts)

    keep_running = asyncio.run(adapter._emit_a2a_progress_update(
        task_id="task-1",
        message_id="message-1",
    ))

    assert keep_running is False
    assert len(adapter._a2a_receipts) == receipt_count


def test_a2a_progress_runner_waits_configured_interval(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
    )
    sleeps = []
    emissions = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def stop_after_one(**kwargs):
        emissions.append(kwargs)
        return False

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    adapter._emit_a2a_progress_update = stop_after_one

    asyncio.run(adapter._run_a2a_progress_updates(
        task_id="task-1",
        message_id="message-1",
    ))

    assert sleeps == [pytest.approx(60, abs=0.1)]
    assert emissions == [{"task_id": "task-1", "message_id": "message-1"}]


def test_a2a_progress_runner_preserves_restart_phase(monkeypatch, tmp_path):
    now = 1_000.0
    monkeypatch.setattr(adapter_mod.time, "time", lambda: now)
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
    )
    now = 1_059.0
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def stop_after_one(**_kwargs):
        return False

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    adapter._emit_a2a_progress_update = stop_after_one

    asyncio.run(adapter._run_a2a_progress_updates(
        task_id="task-1",
        message_id="message-1",
    ))

    assert sleeps == [1]


def test_a2a_progress_runner_preserves_follow_up_phase(monkeypatch, tmp_path):
    now = 2_000.0
    monkeypatch.setattr(adapter_mod.time, "time", lambda: now)
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
    )
    now = 2_059.0
    follow_up = _event()["data"] | {"message_id": "message-2"}
    adapter._write_a2a_registry(
        "task-1:message-2",
        follow_up,
        "running",
        progress_started=True,
    )
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def stop_after_one(**_kwargs):
        return False

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    adapter._emit_a2a_progress_update = stop_after_one

    asyncio.run(adapter._run_a2a_progress_updates(
        task_id="task-1",
        message_id="message-2",
    ))

    assert sleeps == [1]


def test_a2a_progress_runner_retries_pending_delivery_immediately(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
        progress_text="I'm validating the work. (59s elapsed)",
    )

    async def unexpected_sleep(_delay):
        pytest.fail("a pending delivery must be retried before sleeping")

    async def stop_after_one(**_kwargs):
        return False

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", unexpected_sleep)
    adapter._emit_a2a_progress_update = stop_after_one

    asyncio.run(adapter._run_a2a_progress_updates(
        task_id="task-1",
        message_id="message-1",
    ))


def test_a2a_progress_runner_retries_active_delivery_after_five_seconds(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
    )
    reply_attempts = 0

    class Identity:
        def a2a_task(self, _task_id):
            return types.SimpleNamespace(state="working", messages=[])

        def a2a_reply(self, task_id, **kwargs):
            nonlocal reply_attempts
            reply_attempts += 1
            if reply_attempts == 1:
                raise OSError("delivery unavailable")
            adapter._a2a_receipts.append((task_id, kwargs))

    adapter._inkbox = types.SimpleNamespace(get_identity=lambda _handle: Identity())
    summary_calls = 0

    async def summary(*_args, **_kwargs):
        nonlocal summary_calls
        summary_calls += 1
        return "I'm validating the work."

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(adapter_mod, "build_a2a_progress_update", summary)
    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    original_emit = adapter._emit_a2a_progress_update

    async def stop_after_retry(**kwargs):
        result = await original_emit(**kwargs)
        return False if reply_attempts == 2 else result

    adapter._emit_a2a_progress_update = stop_after_retry

    asyncio.run(adapter._run_a2a_progress_updates(
        task_id="task-1",
        message_id="message-1",
    ))

    assert sleeps[0] == pytest.approx(60, abs=0.1)
    assert sleeps[1:] == [5]
    assert summary_calls == 1
    assert reply_attempts == 2
    assert adapter._a2a_receipts[-1][1]["text"].startswith(
        "I'm validating the work."
    )


def test_a2a_progress_runner_retries_local_preparation_failure(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    attempts = 0

    async def fake_sleep(_delay):
        return None

    async def fail_once_then_stop(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary local failure")
        return False

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    adapter._emit_a2a_progress_update = fail_once_then_stop

    asyncio.run(adapter._run_a2a_progress_updates(
        task_id="task-1",
        message_id="message-1",
    ))

    assert attempts == 2


def test_a2a_processing_completion_stops_progress_timer(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60

    async def scenario():
        await adapter._on_a2a_event(_event())
        event = adapter._enqueued[0]
        await adapter.on_processing_start(event)
        assert "task-1" in adapter._a2a_progress_tasks
        await adapter.on_processing_complete(
            event,
            types.SimpleNamespace(value="success"),
        )
        assert adapter._a2a_progress_tasks == {}

    asyncio.run(scenario())


def test_a2a_cancel_waits_for_inflight_reply_thread(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    entered = threading.Event()
    release = threading.Event()
    replies = []

    class Identity:
        def a2a_task(self, _task_id):
            return types.SimpleNamespace(state="working", messages=[])

        def a2a_reply(self, task_id, **kwargs):
            entered.set()
            release.wait(timeout=5)
            replies.append((task_id, kwargs))

    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: Identity(),
    )
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
        progress_text="I'm validating the work. (60s elapsed)",
    )

    async def scenario():
        await adapter._start_a2a_progress_updates(
            task_id="task-1",
            message_id="message-1",
            data=_event()["data"],
        )
        while not entered.is_set():
            await asyncio.sleep(0.01)
        canceled = _event("evt-cancel", "a2a.task.canceled")
        cancel_task = asyncio.create_task(adapter._on_a2a_event(canceled))
        await asyncio.sleep(0.05)
        assert not cancel_task.done()
        release.set()
        await cancel_task
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert len(replies) == 1
    assert adapter._a2a_progress_tasks == {}


def test_late_receipt_cannot_regress_running_or_failed_state(tmp_path):
    adapter = _adapter(tmp_path)

    async def start_before_enqueue_returns(event):
        adapter._enqueued.append(event)
        await adapter.on_processing_start(event)

    adapter._enqueue = start_before_enqueue_returns

    response = asyncio.run(adapter._on_a2a_event(_event()))

    assert response.status == 200
    registry = json.loads(adapter._a2a_registry_path.read_text())
    entry = registry["task-1:message-1"]
    assert entry["state"] == "running"
    assert "acknowledged" in entry["phases"]

    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "turn_failed",
        error_phase="execution",
        error_code="turn_failed",
    )
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "acknowledged",
    )

    registry = json.loads(adapter._a2a_registry_path.read_text())
    entry = registry["task-1:message-1"]
    assert entry["state"] == "turn_failed"
    assert entry["last_error"]["phase"] == "execution"


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


def test_generic_a2a_reply_cannot_complete_task(monkeypatch, tmp_path):
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
    assert result.message_id == "a2a-explicit-outcome-required"
    assert replies == []
    assert adapter._a2a_tasks_by_chat[chat_id] == ["task-1", "task-2"]


def test_explicit_a2a_intent_finalizes_with_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(tmp_path)
    chat_id = "a2a:identity-1:context-1"
    adapter._a2a_tasks_by_chat[chat_id] = ["task-1"]
    adapter._a2a_session_by_chat[chat_id] = "session-1"
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
    )
    write_a2a_turn_context(
        "session-1",
        {
            "task_id": "task-1",
            "context_id": "context-1",
            "message_id": "message-1",
            "reply_intent_committed": True,
            "reply_intent": "fail",
        },
    )

    result = asyncio.run(adapter._send_a2a_reply(chat_id, "[SILENT]"))

    assert result.success is True
    assert adapter._a2a_tasks_by_chat[chat_id] == []
    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert registry["task-1:message-1"]["state"] == "finalized"
    assert registry["task-1:message-1"]["outcome"] == "failed"


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
    assert read_a2a_turn_context("session-1")["reply_intent"] == "ask_caller"


def test_a2a_terminal_tool_waits_for_inflight_progress(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(tmp_path)
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
    )
    progress_mod.start_a2a_progress("task-1", reset_fence=True)
    write_a2a_turn_context(
        "session-1",
        {
            "task_id": "task-1",
            "context_id": "context-1",
            "message_id": "message-1",
            "reply_intent_committed": False,
        },
    )
    progress_entered = asyncio.Event()
    release_progress = asyncio.Event()
    terminal_replies = []

    async def paused_summary(*_args, **_kwargs):
        progress_entered.set()
        await release_progress.wait()
        return "I'm validating the work."

    monkeypatch.setattr(adapter_mod, "build_a2a_progress_update", paused_summary)
    monkeypatch.setattr(
        tools_mod,
        "_client_and_identity",
        lambda: (
            None,
            None,
            types.SimpleNamespace(
                a2a_reply=lambda task_id, **kwargs: terminal_replies.append(
                    (task_id, kwargs)
                )
            ),
        ),
    )

    async def scenario():
        progress_task = asyncio.create_task(adapter._emit_a2a_progress_update(
            task_id="task-1",
            message_id="message-1",
        ))
        await progress_entered.wait()
        terminal_task = asyncio.create_task(asyncio.to_thread(
            tools_mod.inkbox_a2a_complete,
            {"text": "Done."},
            task_id="session-1",
        ))
        await asyncio.sleep(0.05)
        assert terminal_replies == []
        release_progress.set()
        await progress_task
        await terminal_task

    asyncio.run(scenario())

    assert adapter._a2a_receipts[-1][1]["intent"] == "progress"
    assert terminal_replies == [(
        "task-1",
        {"intent": "complete", "text": "Done."},
    )]


@pytest.mark.parametrize(
    ("intent_tool", "args"),
    [
        (tools_mod.inkbox_a2a_complete, {"text": "Done."}),
        (tools_mod.inkbox_a2a_ask_caller, {"text": "Which region?"}),
        (tools_mod.inkbox_a2a_fail, {"reason": "Cannot continue."}),
    ],
)
def test_a2a_ambiguous_outcome_restart_keeps_progress_fenced(
    monkeypatch,
    tmp_path,
    intent_tool,
    args,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(tmp_path)
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
        progress_started=True,
    )
    progress_mod.start_a2a_progress("task-1", reset_fence=True)
    write_a2a_turn_context(
        "session-1",
        {
            "task_id": "task-1",
            "context_id": "context-1",
            "message_id": "message-1",
            "reply_intent_committed": False,
        },
    )
    monkeypatch.setattr(
        tools_mod,
        "_client_and_identity",
        lambda: (
            None,
            None,
            types.SimpleNamespace(
                a2a_reply=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError("ambiguous delivery")
                ),
                a2a_task=lambda _task_id: types.SimpleNamespace(state="working"),
            ),
        ),
    )

    result = json.loads(intent_tool(args, task_id="session-1"))
    progress_mod._FENCED_TASKS.clear()
    progress_mod._FENCE_OWNER_BY_TASK.clear()
    keep_running = asyncio.run(adapter._emit_a2a_progress_update(
        task_id="task-1",
        message_id="message-1",
    ))

    assert "ambiguous delivery" in result["error"]
    assert keep_running is False
    assert adapter._a2a_receipts == []


def test_a2a_ask_caller_follow_up_reacquires_progress(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    first = _event()["data"]
    adapter._write_a2a_registry(
        "task-1:message-1",
        first,
        "finalized",
        outcome="input_required",
        progress_started=True,
    )
    progress_mod.start_a2a_progress("task-1", reset_fence=True)
    progress_mod.fence_a2a_progress_delivery("task-1", "message-1")
    follow_up = first | {"message_id": "message-2"}

    async def scenario():
        await adapter._start_a2a_progress_updates(
            task_id="task-1",
            message_id="message-2",
            data=follow_up,
        )
        await adapter._stop_a2a_progress_updates("task-1")
        return await adapter._emit_a2a_progress_update(
            task_id="task-1",
            message_id="message-2",
        )

    assert asyncio.run(scenario()) is True
    assert adapter._a2a_receipts[-1][1]["intent"] == "progress"


def test_a2a_same_key_restart_with_older_sibling_keeps_fence(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(tmp_path)
    adapter._a2a_progress_interval_seconds = 60
    first = _event()["data"]
    second = first | {"message_id": "message-2"}
    adapter._write_a2a_registry(
        "task-1:message-1",
        first,
        "finalized",
        progress_started=True,
    )
    adapter._write_a2a_registry(
        "task-1:message-2",
        second,
        "running",
        progress_started=True,
    )
    progress_mod.start_a2a_progress("task-1", reset_fence=True)
    progress_mod.fence_a2a_progress_delivery("task-1", "message-2")
    progress_mod._FENCED_TASKS.clear()
    progress_mod._FENCE_OWNER_BY_TASK.clear()

    async def scenario():
        await adapter._start_a2a_progress_updates(
            task_id="task-1",
            message_id="message-2",
            data=second,
        )
        await adapter._stop_a2a_progress_updates("task-1")
        return await adapter._emit_a2a_progress_update(
            task_id="task-1",
            message_id="message-2",
        )

    assert asyncio.run(scenario()) is False
    assert adapter._a2a_receipts == []


def test_a2a_restart_does_not_enqueue_outcome_fenced_message(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    data = _event()["data"]
    adapter._write_a2a_registry(
        "task-1:message-1",
        data,
        "running",
    )
    progress_mod.fence_a2a_progress_delivery("task-1", "message-1")
    progress_mod._FENCED_TASKS.clear()
    progress_mod._FENCE_OWNER_BY_TASK.clear()
    task = types.SimpleNamespace(
        id="task-1",
        context_id="context-1",
        state="working",
        caller=types.SimpleNamespace(
            identity_id="caller-1",
            organization_id="org-1",
            handle="caller",
        ),
        messages=[types.SimpleNamespace(
            message_id="message-1",
            role="ROLE_CALLER",
            parts=[{"text": "Please investigate."}],
        )],
    )
    identity = types.SimpleNamespace(
        a2a_task=lambda _task_id: task,
        a2a_reply=lambda *_args, **_kwargs: None,
        iter_a2a_tasks=lambda **_kwargs: iter((task,)),
    )
    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: identity,
    )

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", inline)

    async def scenario():
        await adapter._catch_up_a2a_tasks()
        await adapter._on_a2a_event(_event())
        follow_up = _event("evt-2")
        follow_up["data"]["message_id"] = "message-2"
        await adapter._on_a2a_event(follow_up)

    asyncio.run(scenario())

    assert len(adapter._enqueued) == 1
    assert adapter._enqueued[0].message_id == "message-2"


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
                role="ROLE_CALLER",
                parts=[{"text": "SDK copy must not replace persisted input."}],
            ),
            types.SimpleNamespace(
                message_id="receipt-1",
                role="ROLE_AGENT",
                parts=[{"text": (
                    "Task task-1 received. Work is queued and starting. "
                    "Periodic progress updates are disabled."
                )}],
            ),
            types.SimpleNamespace(
                message_id="progress-1",
                role="agent",
                parts=[{"text": "I'm continuing the requested work. (60s elapsed)"}],
            ),
        ],
    )
    identity = types.SimpleNamespace(
        a2a_task=lambda _task_id: task,
        a2a_reply=lambda *_args, **_kwargs: None,
        iter_a2a_tasks=lambda **_kwargs: iter((task,)),
    )
    adapter._inkbox = types.SimpleNamespace(
        get_identity=lambda _handle: identity,
    )

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", inline)
    asyncio.run(adapter._catch_up_a2a_tasks())

    assert len(adapter._enqueued) == 1
    assert adapter._enqueued[0].text.endswith("Please investigate.")
    assert adapter._a2a_tasks_by_chat[
        "a2a:identity-1:context-1"
    ] == ["task-1"]
    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert list(registry) == ["task-1:message-1"]
    assert registry["task-1:message-1"]["state"] == "enqueued"
    assert registry["task-1:message-1"]["attempt"] == 2


def test_a2a_catch_up_finalizes_auth_required_without_rerun(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter._write_a2a_registry(
        "task-1:message-1",
        _event()["data"],
        "running",
    )
    task = types.SimpleNamespace(state="auth_required")
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

    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert registry["task-1:message-1"]["state"] == "finalized"
    assert registry["task-1:message-1"]["outcome"] == "auth_required"
    assert adapter._enqueued == []


def test_a2a_catch_up_new_task_selects_latest_caller_message(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    task = types.SimpleNamespace(
        id="task-1",
        context_id="context-1",
        state="submitted",
        caller=types.SimpleNamespace(
            identity_id="caller-1",
            organization_id="org-1",
            handle="caller",
        ),
        messages=[
            types.SimpleNamespace(
                message_id="message-1",
                role="ROLE_CALLER",
                parts=[{"text": "Use this caller request."}],
            ),
            types.SimpleNamespace(
                message_id="progress-1",
                role="ROLE_AGENT",
                parts=[{"text": "Ignore this worker progress."}],
            ),
        ],
    )
    identity = types.SimpleNamespace(
        a2a_task=lambda _task_id: task,
        a2a_reply=lambda *_args, **_kwargs: None,
        iter_a2a_tasks=lambda **_kwargs: iter((task,)),
    )
    adapter._inkbox = types.SimpleNamespace(get_identity=lambda _handle: identity)

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", inline)
    asyncio.run(adapter._catch_up_a2a_tasks())

    assert len(adapter._enqueued) == 1
    assert adapter._enqueued[0].message_id == "message-1"
    assert adapter._enqueued[0].text.endswith("Use this caller request.")
    registry = json.loads(adapter._a2a_registry_path.read_text())
    assert list(registry) == ["task-1:message-1"]


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


def test_a2a_turn_context_receives_verified_headers(tmp_path):
    """Verified ``x-inkbox-*`` headers reach the turn-context binding."""
    adapter = _adapter(tmp_path)
    captured = {}

    def _capture(*_args, **kwargs):
        captured.update(kwargs)
        return True

    adapter._bind_a2a_turn_context = _capture

    asyncio.run(
        adapter._on_a2a_event(
            _event(),
            headers={"x-inkbox-example": "value-1", "x-inkbox-other": "value-2"},
        )
    )

    assert captured["headers"] == {
        "x-inkbox-example": "value-1",
        "x-inkbox-other": "value-2",
    }
    assert captured["task_id"] == "task-1"


def test_a2a_turn_context_headers_are_optional(tmp_path):
    """A caller that passes no headers still binds, unchanged."""
    adapter = _adapter(tmp_path)
    captured = {}

    def _capture(*_args, **kwargs):
        captured.update(kwargs)
        return True

    adapter._bind_a2a_turn_context = _capture

    response = asyncio.run(adapter._on_a2a_event(_event()))

    assert response.text == "ok"
    assert captured["headers"] is None


def test_context_headers_filter_to_the_inkbox_namespace():
    """Unrelated headers are dropped, and the signature is never retained."""
    selected = adapter_mod._a2a_context_headers(
        {
            "Authorization": "Bearer secret",
            "Cookie": "session=abc",
            "X-Inkbox-Signature": "sig",
            "X-Inkbox-Timestamp": "123",
            "X-Inkbox-Example": "kept",
        }
    )

    assert selected == {"x-inkbox-example": "kept"}
