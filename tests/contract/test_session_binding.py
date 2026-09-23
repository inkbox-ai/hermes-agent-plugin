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
