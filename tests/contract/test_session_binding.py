"""Exercise session binding with the host's real callback and session store."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway.config import GatewayConfig, Platform
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.session import SessionStore

from tests.test_a2a import _adapter, _event
from inkbox_plugin.a2a_context import activate_next_a2a_turn_context


def test_a2a_ingestion_with_real_host_session_wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(PlatformEntry(
        name="inkbox", label="Inkbox", adapter_factory=lambda config: None,
        check_fn=lambda: True,
    ), scope=str(tmp_path))
    adapter = _adapter(tmp_path)
    adapter.platform = Platform("inkbox")
    del adapter._bind_a2a_turn_context
    store = SessionStore(tmp_path / "sessions", GatewayConfig())

    class Runner(GatewayAdapterLifecycleMixin):
        config = SimpleNamespace(multiplex_profiles=False)

        def _standalone_launch_scope(self):
            return nullcontext()

        async def _handle_message(self, event):
            return store.get_or_create_session(event.source).session_id

    adapter.set_message_handler(Runner()._primary_message_handler())
    adapter.set_session_store(store)
    response = asyncio.run(adapter._on_a2a_event(_event()))

    assert response.status == 200
    assert len(adapter._enqueued) == len(adapter._a2a_receipts) == 1
    event = adapter._enqueued[0]
    session_id = adapter._a2a_session_by_chat[event.source.chat_id]
    assert asyncio.run(adapter._message_handler(event)) == session_id
    context = activate_next_a2a_turn_context(session_id)
    assert context["task_id"] == "task-1"
    assert context["message_id"] == "message-1"
    assert asyncio.run(adapter._on_a2a_event(_event("retry"))).text == "duplicate"
    assert len(adapter._enqueued) == len(adapter._a2a_receipts) == 1


def test_group_sources_share_real_host_session_without_merging_private_chat(tmp_path, monkeypatch):
    """Both contact-backed participants use one shared conversation session."""
    from gateway.platforms.base import MessageType
    from gateway.config import PlatformConfig
    from inkbox_plugin.adapter import InkboxAdapter
    from datetime import datetime, timezone
    from unittest.mock import Mock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
    adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent"}))
    adapter._resolve_channel_overrides = Mock(return_value=(None, None))
    adapter._contact_marker = Mock(return_value="contact")
    config = GatewayConfig()
    config.group_sessions_per_user = True
    config.thread_sessions_per_user = False
    store = SessionStore(tmp_path / "sessions", config)
    sessions = []
    for author in ("+15555550101", "+15555550102"):
        event = adapter._build_sms_text_event(
            envelope={}, text_id=author, remote=author, contact={"id": author},
            chat_id="sms:group-1", contact_name="Participant", body="Hello",
            timestamp=datetime.now(timezone.utc), message_type=MessageType.TEXT,
            conversation_id="group-1", is_group=True,
        )
        assert event.source.chat_type == "group"
        sessions.append(store.get_or_create_session(event.source).session_id)
    assert sessions[0] == sessions[1]
    private = adapter.build_source(chat_id="+15555550101", chat_type="dm", user_id="+15555550101")
    other = adapter.build_source(chat_id="sms:group-2", chat_type="group", user_id="+15555550101", thread_id="sms:group-2")
    assert len({sessions[0], store.get_or_create_session(private).session_id,
                store.get_or_create_session(other).session_id}) == 3


def test_group_approval_author_gate_before_real_host_active_dispatch(tmp_path, monkeypatch):
    """Exercise the actual host active-session command path, not only its flag."""
    from unittest.mock import AsyncMock
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType, SendResult
    from inkbox_plugin.adapter import InkboxAdapter
    from inkbox_plugin.conversation import ConversationState

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "group_reply_mode": "auto"}))
        adapter._identity_handle = "sample-agent"
        adapter._conversation_journal = ConversationState(tmp_path / "routes")
        adapter._conversation_journal.row("sms:group-1")["active_author"] = "+15555550101"
        adapter._pending_conversation_control = lambda source: True
        handler = AsyncMock(return_value="Approval recorded")
        adapter._message_handler = handler
        adapter.send = AsyncMock(return_value=SendResult(success=True))
        adapter._handle_message_while_active = AsyncMock(wraps=adapter._handle_message_while_active)

        def incoming(author, message_id):
            source = adapter.build_source(chat_id="sms:group-1", chat_type="group", user_id=author,
                                          user_id_alt=author, thread_id="sms:group-1", message_id=message_id)
            return MessageEvent(text="[inkbox:group_sms] /approve", message_type=MessageType.TEXT, source=source,
                                message_id=message_id, raw_message={"event_type": "text.received", "data": {"text_message": {
                                    "id": message_id, "conversation_id": "group-1", "sender_phone_number": author,
                                    "text": "/approve",
                                }}})
        bystander = incoming("+15555550102", "bystander")
        key = adapter._event_session_key(bystander)
        adapter._active_sessions[key] = asyncio.Event()
        adapter._session_tasks[key] = asyncio.current_task()
        await (await adapter._enqueue(bystander))
        adapter._handle_message_while_active.assert_not_awaited()
        handler.assert_not_awaited()

        asked = incoming("+15555550101", "asked")
        await (await adapter._enqueue(asked))
        adapter._handle_message_while_active.assert_awaited_once()
        handler.assert_awaited_once()
        assert handler.call_args.args[0].get_command() == "approve"
        adapter.send.assert_awaited_once()
        adapter._active_sessions.clear()
        adapter._session_tasks.clear()
    asyncio.run(run())


def test_group_numeric_clarify_answer_is_not_rewritten_as_approval(tmp_path, monkeypatch):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType
    from tools import clarify_gateway
    from inkbox_plugin.adapter import InkboxAdapter
    from inkbox_plugin.conversation import ConversationState

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
    adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "group_reply_mode": "mention"}))
    adapter._identity_handle = "sample-agent"
    adapter._conversation_journal = ConversationState(tmp_path / "routes")
    adapter._conversation_journal.row("sms:group-1")["active_author"] = "+15555550101"
    session_key = "profile-a:choice-question"
    adapter.gateway_runner = SimpleNamespace(_session_key_for_source=lambda source: session_key)
    source = adapter.build_source(chat_id="sms:group-1", chat_type="group", user_id="+15555550101",
                                  user_id_alt="+15555550101", thread_id="sms:group-1")
    prompt = clarify_gateway.register("choice-question", session_key, "Which one?", ["First", "Second"])
    try:
        assert clarify_gateway.get_pending_for_session(session_key) is None
        assert adapter._pending_conversation_control(source)
        incoming = MessageEvent(text="[inkbox:group_sms] 1", message_type=MessageType.TEXT, source=source,
                                message_id="answer", raw_message={"event_type": "text.received", "data": {"text_message": {
                                    "id": "answer", "sender_phone_number": "+15555550101", "conversation_id": "group-1", "text": "1",
                                }}})
        assert adapter._prepare_conversation_event(incoming)
        assert incoming.text == "1" and incoming.allow_gateway_control
        assert clarify_gateway.resolve_text_response_for_session(session_key, incoming.text)
        assert prompt.event.is_set()
    finally:
        clarify_gateway.clear_session(session_key)


@pytest.mark.parametrize("stop", [False, True])
def test_real_host_group_queue_preserves_turns_routes_and_inline_controls(tmp_path, monkeypatch, stop):
    from unittest.mock import AsyncMock
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType, SendResult
    from inkbox_plugin.adapter import InkboxAdapter
    from inkbox_plugin.conversation import ConversationState, reply_route

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "group_reply_mode": "auto"}))
        adapter._identity_handle = "sample-agent"
        adapter._conversation_journal = ConversationState(tmp_path / "routes")
        adapter.send = AsyncMock(return_value=SendResult(success=True))
        adapter._busy_session_handler = AsyncMock(side_effect=AssertionError("Ordinary turns must not enter native interrupt/merge handling"))
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def handler(incoming):
            route = reply_route.get()
            seen.append((incoming.message_id, incoming.get_command(), route["author"], route["message_id"]))
            if incoming.message_id == "first":
                started.set()
                await release.wait()
            return "Acknowledged"
        adapter.set_message_handler(handler)

        def incoming(author, message_id, text):
            source = adapter.build_source(chat_id="sms:group-1", chat_type="group", user_id=author,
                                          user_id_alt=author, thread_id="sms:group-1", message_id=message_id)
            return MessageEvent(text="[inkbox:group_sms] " + text, message_type=MessageType.TEXT, source=source,
                                message_id=message_id, raw_message={"event_type": "text.received", "data": {"text_message": {
                                    "id": message_id, "conversation_id": "group-1", "sender_phone_number": author, "text": text,
                                }}})

        first = await adapter._enqueue(incoming("+15555550101", "first", "First question"))
        await asyncio.wait_for(started.wait(), 2)
        second = await adapter._enqueue(incoming("+15555550102", "second", "Second question"))
        third = await adapter._enqueue(incoming("+15555550103", "third", "Third question"))
        await asyncio.sleep(0.02)
        assert [item[0] for item in seen] == ["first"]

        adapter._pending_conversation_control = lambda source: True
        await (await adapter._enqueue(incoming("+15555550101", "approval", "allow")))
        assert seen[-1] == ("approval", "approve", "+15555550101", "approval")
        adapter._pending_conversation_control = lambda source: False

        # Health is a real native inline status command, not a queued model turn.
        await (await adapter._enqueue(incoming("+15555550102", "health", "/health")))
        assert seen[-1] == ("health", "status", "+15555550102", "health")
        if stop:
            await (await adapter._enqueue(incoming("+15555550101", "stop", "/cancel")))
            assert seen[-1] == ("stop", "stop", "+15555550101", "stop")
        else:
            release.set()
        await asyncio.wait_for(asyncio.gather(first, second, third, return_exceptions=True), 3)
        if stop:
            assert second.cancelled() and third.cancelled()
            assert not any(item[0] in {"second", "third"} for item in seen)
        else:
            assert seen[-2:] == [("second", None, "+15555550102", "second"),
                                 ("third", None, "+15555550103", "third")]
        adapter._busy_session_handler.assert_not_awaited()
        await asyncio.sleep(0)  # Flush done callbacks for an already-finished owner.
        assert not adapter._group_dispatch_lanes
        assert not adapter._active_sessions
    asyncio.run(run())


def test_real_host_pre_admission_drop_releases_quiet_context(tmp_path, monkeypatch):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType
    from inkbox_plugin.adapter import InkboxAdapter
    from inkbox_plugin.conversation import ConversationState

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "group_reply_mode": "mention"}))
        adapter._identity_handle = "sample-agent"
        adapter._conversation_journal = ConversationState(tmp_path / "routes")
        journal = adapter._conversation_journal
        journal.quiet("sms:group-1", "quiet", "Retained context")
        source = adapter.build_source(chat_id="sms:group-1", chat_type="group", user_id="+15555550101",
                                      thread_id="sms:group-1")
        incoming = MessageEvent(text="[inkbox:group_sms] @agent summarize", message_type=MessageType.TEXT,
                                source=source, message_id="wake", raw_message={"event_type": "text.received", "data": {
                                    "text_message": {"id": "wake", "sender_phone_number": "+15555550101", "text": "@agent summarize"}}})
        # No registered native message handler: handle_message drops this before
        # acceptance without ever firing on_processing_complete.
        await (await adapter._enqueue(incoming))
        assert incoming._gateway_accepted is False
        assert "turn" not in journal.row("sms:group-1")["quiet"][0]
        assert "wake" in journal.row("sms:group-1")["completed_routes"]
        assert not adapter._group_dispatch_lanes
    asyncio.run(run())


@pytest.mark.parametrize("command", ["/new", "/clear"])
@pytest.mark.parametrize("outcome", ["reset", "denied", "failed", "reset_then_failed"])
def test_real_store_reset_clears_quiet_only_after_session_rotation(tmp_path, monkeypatch, command, outcome):
    from unittest.mock import AsyncMock
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType, SendResult
    from inkbox_plugin.adapter import InkboxAdapter
    from inkbox_plugin.conversation import ConversationState

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "group_reply_mode": "mention"}))
        adapter._identity_handle = "sample-agent"
        adapter._conversation_journal = ConversationState(tmp_path / "routes")
        journal = adapter._conversation_journal
        journal.quiet("sms:group-1", "quiet", "Old context")
        journal.quiet("sms:other", "other", "Other group's context")
        journal.remember("sms:group-1", "old-turn", {"chat_id": "sms:group-1"})
        source = adapter.build_source(chat_id="sms:group-1", chat_type="group", user_id="+15555550101",
                                      thread_id="sms:group-1")
        store = SessionStore(tmp_path / "sessions", GatewayConfig())
        original = store.get_or_create_session(source)
        adapter.set_session_store(store)
        key = adapter._conversation_session_key(source)

        async def handler(incoming):
            assert incoming.get_command() == "new"
            if outcome in {"reset", "reset_then_failed"}:
                assert store.reset_session(key).session_id != original.session_id
            if outcome in {"failed", "reset_then_failed"}:
                raise RuntimeError("Synthetic command failure")
            # Deliberately identical text: a reply must never be reset evidence.
            return "Started a fresh session."

        adapter.set_message_handler(handler)
        adapter.send = AsyncMock(return_value=SendResult(success=True))
        incoming = MessageEvent(text="[inkbox:group_sms] " + command, message_type=MessageType.TEXT,
                                source=source, message_id="command", raw_message={"event_type": "text.received", "data": {
                                    "text_message": {"id": "command", "sender_phone_number": "+15555550101", "text": command}}})
        await (await adapter._enqueue(incoming))
        await asyncio.gather(*list(adapter._background_tasks))
        assert bool(journal.row("sms:group-1")["quiet"]) == (outcome in {"denied", "failed"})
        assert journal.row("sms:other")["quiet"][0]["text"] == "Other group's context"
        assert "old-turn" in journal.row("sms:group-1")["routes"]
    asyncio.run(run())


@pytest.mark.parametrize("behavior", ["ordinary", "capture", "unknown", "quiet", "approval", "status", "denied_source", "denied_bot"])
def test_native_group_interruption_preserves_fifo_and_protects_nonordinary_work(tmp_path, monkeypatch, behavior):
    import threading
    from unittest.mock import AsyncMock, Mock
    from run_agent import AIAgent
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType, SendResult
    from gateway.run_turn import GatewayTurnMixin
    from inkbox_plugin.adapter import InkboxAdapter
    from inkbox_plugin.conversation import ConversationState, reply_route

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent", "group_reply_mode": "mention"}))
        adapter._identity_handle = "sample-agent"
        adapter._conversation_journal = ConversationState(tmp_path / "routes")
        adapter.send = AsyncMock(return_value=SendResult(success=True))
        model = AIAgent.__new__(AIAgent)
        model._interrupt_requested, model._interrupt_message = False, None
        model._hard_interrupt_requested = threading.Event()
        model._execution_thread_id = None
        model._active_children, model._active_children_lock = [], threading.Lock()
        model.quiet_mode = True
        model.interrupt = Mock(wraps=model.interrupt)
        started, release, cancelled, finish_cancel = (asyncio.Event() for _ in range(4))
        seen = []

        class Runner:
            state = SimpleNamespace(turn=SimpleNamespace(event=None, agent=None))
            _draining = False

            def _peek_session_state(self, key):
                return self.state

            def _is_user_authorized_for_source(self, source):
                return behavior != "denied_source"

            def _admit_bot_message_for_source(self, source):
                return behavior != "denied_bot"

            def _promote_queued_event(self, key, transport, pending):
                return pending

            async def handle(self, incoming):
                route = reply_route.get()
                seen.append((incoming.message_id, route["author"], route["message_id"]))
                if incoming.message_id != "first":
                    return incoming.message_id
                self.state.turn.event, self.state.turn.agent = incoming, model
                if behavior == "unknown":
                    self.state.turn.event = SimpleNamespace(metadata={}, raw_message={})
                started.set()
                try:
                    await release.wait()
                    return "first"
                except asyncio.CancelledError:
                    cancelled.set()
                    await finish_cancel.wait()
                    # Simulate a host/transport returning a late partial despite
                    # cancellation. The plugin must not deliver this as a reply.
                    return "Stale partial from first"
                finally:
                    self.state.turn.agent = None

        runner = Runner()
        runner._evict_cached_agent = Mock()
        adapter.set_message_handler(runner.handle)

        def incoming(message_id, author, text):
            source = adapter.build_source(chat_id="sms:group-1", chat_type="group", user_id=author,
                                          user_id_alt=author, thread_id="sms:group-1")
            return MessageEvent(text="[inkbox:group_sms] " + text, message_type=MessageType.TEXT,
                                source=source, message_id=message_id, raw_message={"event_type": "text.received", "data": {
                                    "text_message": {"id": message_id, "sender_phone_number": author, "text": text}}})

        first_event = incoming("first", "+15555550101", "@agent first")
        if behavior == "capture":
            adapter._prepare_conversation_event(first_event)
            first_event.raw_message = {"synthetic": "delivery_failure"}
        first = await adapter._enqueue(first_event)
        await asyncio.wait_for(started.wait(), 2)
        if behavior == "approval":
            adapter._pending_conversation_control = lambda source: True
        second_event = incoming("second", "+15555550101" if behavior == "approval" else "+15555550102",
                                {"quiet": "Unaddressed context", "approval": "allow", "status": "/status"}.get(behavior, "@agent second"))
        submitting = asyncio.create_task(adapter._enqueue(second_event))
        if behavior == "ordinary":
            await asyncio.wait_for(cancelled.wait(), 2)
            assert submitting.done(), "Webhook admission must not wait for model cancellation cleanup"
            third = await adapter._enqueue(incoming("third", "+15555550103", "@agent third"))
            model.interrupt.assert_called_once_with()
            assert model._interrupt_requested and model._interrupt_message is None
            key = adapter._event_session_key(first_event)
            assert await GatewayTurnMixin._run_agent_drain_pending(
                runner, {"interrupted": True, "interrupt_message": model._interrupt_message}, adapter, first_event.source, key,
            ) == (None, None)
            finish_cancel.set()
        else:
            await asyncio.sleep(0.02)
            model.interrupt.assert_not_called()
            assert not cancelled.is_set()
            release.set()
            third = None
        second = await submitting
        await asyncio.wait_for(asyncio.gather(first, second, *([third] if third else []), return_exceptions=True), 3)
        await asyncio.sleep(0)
        if behavior == "ordinary":
            assert seen == [("first", "+15555550101", "first"), ("second", "+15555550102", "second"),
                            ("third", "+15555550103", "third")]
            assert [call.kwargs.get("content", call.args[1] if len(call.args) > 1 else None)
                    for call in adapter.send.call_args_list] == ["second", "third"]
        elif behavior in {"quiet", "denied_source", "denied_bot"}:
            assert [item[0] for item in seen] == ["first"]
        else:
            assert [item[0] for item in seen] == ["first", "second"]
        assert not adapter._group_dispatch_lanes
    asyncio.run(run())


@pytest.mark.parametrize("lifecycle", ["finished_model", "executor_unwinding", "wedged_owner"])
def test_native_interrupted_agent_is_not_reused_while_executor_unwinds(tmp_path, monkeypatch, lifecycle):
    """Adapter-task completion is not evidence that the native worker thread exited."""
    import threading
    from unittest.mock import AsyncMock, Mock
    from run_agent import AIAgent
    from agent.turn_context import _bind_interrupt_scope
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType, SendResult
    from gateway.run import GatewayRunner
    from inkbox_plugin.adapter import InkboxAdapter
    from inkbox_plugin.conversation import ConversationState, reply_route

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
        adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent"}))
        adapter._identity_handle = "sample-agent"
        adapter._conversation_journal = ConversationState(tmp_path / "routes")
        adapter.send = AsyncMock(return_value=SendResult(success=True))
        started, cancelled, finish_owner = (asyncio.Event() for _ in range(3))
        finish_worker = threading.Event()
        loop = asyncio.get_running_loop()
        models, received = [], []

        def new_model():
            model = AIAgent.__new__(AIAgent)
            model._interrupt_requested, model._interrupt_message = False, None
            model._hard_interrupt_requested = threading.Event()
            model._execution_thread_id = None
            model._active_children, model._active_children_lock = [], threading.Lock()
            model.quiet_mode = True
            model.interrupt = Mock(wraps=model.interrupt)
            model.clear_interrupt = Mock(wraps=model.clear_interrupt)
            models.append(model)
            return model

        class Runner(GatewayRunner):
            def _get_executor(self):
                return None

            def _is_user_authorized_for_source(self, source):
                return True

            def _admit_bot_message_for_source(self, source):
                return True

            async def handle(self, event):
                key = adapter._event_session_key(event)
                entry = self.session_store.get_or_create_session(event.source)
                received.append((event.message_id, entry.session_id, reply_route.get()["author"]))
                model = self._agent_cache.get(key)
                if model is None:
                    self._agent_cache[key] = model = new_model()
                if event.message_id != "first":
                    # Use native turn-start binding: it deliberately preserves
                    # a pending interrupt, so a stale cached agent loses B.
                    _bind_interrupt_scope(model, lambda: SimpleNamespace(_set_interrupt=lambda *a, **kw: None))
                    return None if model._interrupt_requested else event.message_id
                self._session_state(key).turn.event = event
                self._session_state(key).turn.agent = model

                def run_model():
                    if lifecycle != "finished_model":
                        loop.call_soon_threadsafe(started.set)
                        assert finish_worker.wait(12)
                    model.clear_interrupt()
                    return "first"

                ctx = SimpleNamespace(agent_holder=[model], session_key=key, run_generation=None, session_id=entry.session_id)
                self.worker = self._run_agent_start_turn_worker(ctx, run_model)
                try:
                    result = await self._run_agent_await_turn_worker(self.worker, ctx, asyncio.Event(), None)
                    started.set()
                    await finish_owner.wait()  # Executor finished; runner slot still belongs to A.
                    return result
                except asyncio.CancelledError:
                    cancelled.set()
                    if lifecycle == "wedged_owner":
                        await finish_owner.wait()
                    return "Cancelled A must never be delivered"
                finally:
                    self._session_state(key).turn.agent = None

        runner = Runner.__new__(Runner)
        runner.config = GatewayConfig()
        runner.session_store = SessionStore(tmp_path / "sessions", runner.config)
        runner._agent_cache, runner._agent_cache_lock = {}, threading.Lock()
        runner._spawn_release_thread = Mock(side_effect=AssertionError("Eviction must not tear down the still-owned agent"))
        adapter.set_message_handler(runner.handle)

        def incoming(message_id, author):
            source = adapter.build_source(chat_id="sms:group-1", chat_type="group", user_id=author,
                                          user_id_alt=author, thread_id="sms:group-1")
            return MessageEvent(text="[inkbox:group_sms] " + message_id, message_type=MessageType.TEXT,
                                source=source, message_id=message_id, raw_message={"event_type": "text.received", "data": {
                                    "text_message": {"id": message_id, "sender_phone_number": author, "text": message_id}}})

        first_event = incoming("first", "+15555550101")
        first = await adapter._enqueue(first_event)
        await asyncio.wait_for(started.wait(), 2)
        original = models[0]
        initial_clear_count = original.clear_interrupt.call_count
        key = adapter._event_session_key(first_event)
        owner = adapter._session_tasks[key]
        try:
            second = await adapter._enqueue(incoming("second", "+15555550102"))
            await asyncio.wait_for(cancelled.wait(), 2)
            if lifecycle == "wedged_owner":
                for _ in range(120):
                    if key not in adapter._active_sessions:
                        break
                    await asyncio.sleep(0.05)
                assert not owner.done()
                assert key not in runner._agent_cache
                assert original._interrupt_requested
                assert not runner.worker.worker_done.is_set()
                finish_owner.set()
            await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert owner.done()
            original.interrupt.assert_called_once_with()
            assert original.clear_interrupt.call_count == initial_clear_count
            assert original._interrupt_requested, "Never clear an interrupt based only on the adapter task finishing"
            assert runner._agent_cache[key] is not original
            runner._spawn_release_thread.assert_not_called()
            assert received[0][1] == received[1][1], "Evicting a model must preserve the native conversation session"
            assert [item[2] for item in received] == ["+15555550101", "+15555550102"]
            assert [call.kwargs.get("content", call.args[1] if len(call.args) > 1 else None)
                    for call in adapter.send.call_args_list] == ["second"]
            assert runner.worker.worker_done.is_set() == (lifecycle == "finished_model")
        finally:
            finish_owner.set()
            finish_worker.set()
            await asyncio.wait_for(runner.worker.executor_task, 2)
            await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
    asyncio.run(run())
