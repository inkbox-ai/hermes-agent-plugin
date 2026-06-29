import asyncio
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin import tools
from inkbox_plugin.adapter import (
    InkboxAdapter,
    _imessage_conversation_target,
    send_inkbox_direct,
)


class FakeIMessage:
    id = "im-1"
    conversation_id = "imconv-123"
    service = "imessage"
    status = "queued"


class FakeIdentity:
    def __init__(self):
        self.sent_imessages = []
        self.calls = []

    def send_imessage(self, **kwargs):
        self.sent_imessages.append(kwargs)
        return FakeIMessage()

    def list_imessage_conversations(self, **kwargs):
        self.calls.append(("list_imessage_conversations", kwargs))
        return [{
            "id": "imconv-123",
            "remote_number": "+15555550101",
            "latest_text": "hi",
            "unread_count": 1,
            "total_count": 3,
        }]

    def list_imessages(self, **kwargs):
        self.calls.append(("list_imessages", kwargs))
        return [{"id": "im-1", "conversation_id": kwargs.get("conversation_id")}]

    def mark_imessage_conversation_read(self, conversation_id):
        self.calls.append(("mark_imessage_conversation_read", conversation_id))
        return types.SimpleNamespace(updated_count=2)

    def send_imessage_reaction(self, **kwargs):
        self.calls.append(("send_imessage_reaction", kwargs))
        return types.SimpleNamespace(id="react-1", reaction="like")

    def send_imessage_typing(self, conversation_id):
        self.calls.append(("send_imessage_typing", conversation_id))


class FakeInkboxClient:
    def __init__(self, identity):
        self.identity = identity
        self.contacts = types.SimpleNamespace(get=lambda _contact_id: None)

    def get_identity(self, _handle):
        return self.identity

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_imessage_conversation_target_parsing():
    assert _imessage_conversation_target("imessage:conversation:imconv-1") == "imconv-1"
    assert _imessage_conversation_target("imessage:imconv-1") == "imconv-1"
    assert _imessage_conversation_target("inkbox:imessage:conversation:imconv-1") == "imconv-1"
    assert _imessage_conversation_target("imessage:+15555550101") is None
    assert _imessage_conversation_target("sms:conversation:conv-1") is None
    assert _imessage_conversation_target("") is None


def test_send_imessage_tool_prefers_conversation_id(monkeypatch):
    identity = FakeIdentity()
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))

    out = json.loads(tools.inkbox_send_imessage({
        "conversationId": "imconv-123",
        "text": "hello",
    }))

    assert out["ok"] is True
    assert identity.sent_imessages == [{"text": "hello", "conversation_id": "imconv-123"}]


def test_send_imessage_tool_rejects_text_over_limit(monkeypatch):
    identity = FakeIdentity()
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))

    out = json.loads(tools.inkbox_send_imessage({
        "conversationId": "imconv-123",
        "text": "x" * (tools.IMESSAGE_MAX_LENGTH + 1),
    }))

    assert out["error_code"] == "imessage_too_long"
    assert out["char_count"] == tools.IMESSAGE_MAX_LENGTH + 1
    assert identity.sent_imessages == []


def test_send_imessage_tool_requires_exactly_one_target(monkeypatch):
    identity = FakeIdentity()
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))

    both = json.loads(tools.inkbox_send_imessage({
        "conversationId": "imconv-123",
        "to": "+15555550101",
        "text": "hello",
    }))
    neither = json.loads(tools.inkbox_send_imessage({"text": "hello"}))

    assert "error" in both
    assert "error" in neither
    assert identity.sent_imessages == []


def test_imessage_conversation_tools_use_conversation_id(monkeypatch):
    identity = FakeIdentity()
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))

    conversations = json.loads(tools.inkbox_list_imessage_conversations({}))
    history = json.loads(tools.inkbox_get_imessage_conversation({"conversationId": "imconv-123"}))
    marked = json.loads(tools.inkbox_mark_imessage_conversation_read({"conversationId": "imconv-123"}))
    reacted = json.loads(tools.inkbox_send_imessage_reaction({
        "messageId": "im-1",
        "reaction": "like",
    }))

    assert conversations["count"] == 1
    assert history["messages"][0]["conversation_id"] == "imconv-123"
    assert marked["updated_count"] == 2
    assert reacted["ok"] is True
    assert identity.calls == [
        ("list_imessage_conversations", {"limit": 25, "offset": 0}),
        ("list_imessages", {"conversation_id": "imconv-123", "limit": 50, "offset": 0}),
        ("mark_imessage_conversation_read", "imconv-123"),
        ("send_imessage_reaction", {"message_id": "im-1", "reaction": "like", "part_index": 0}),
    ]


def test_adapter_imessage_reply_uses_last_inbound_conversation_id(monkeypatch):
    identity = FakeIdentity()

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    adapter = object.__new__(InkboxAdapter)
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"contact-123": "imessage"}
    adapter._last_inbound_imessage = {
        "contact-123": {
            "conversation_id": "imconv-123",
            "remote_number": "+15555550101",
            "message_id": "im-in",
        },
    }
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"

    # No explicit mode: the stashed inbound modality routes this to iMessage.
    result = asyncio.run(adapter.send("contact-123", "reply"))

    assert result.success is True
    assert identity.sent_imessages == [{"conversation_id": "imconv-123", "text": "reply"}]


def test_adapter_imessage_reply_rejects_text_over_limit():
    identity = FakeIdentity()
    adapter = object.__new__(InkboxAdapter)
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"contact-123": "imessage"}
    adapter._last_inbound_imessage = {
        "contact-123": {
            "conversation_id": "imconv-123",
            "remote_number": "+15555550101",
            "message_id": "im-in",
        },
    }
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"

    result = asyncio.run(adapter.send("contact-123", "x" * (adapter_mod.IMESSAGE_MAX_LENGTH + 1)))

    assert result.success is False
    assert result.raw_response["error_code"] == "imessage_too_long"
    assert result.raw_response["char_count"] == adapter_mod.IMESSAGE_MAX_LENGTH + 1
    assert identity.sent_imessages == []


def test_adapter_imessage_reply_uses_thread_conversation_id(monkeypatch):
    identity = FakeIdentity()

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    adapter = object.__new__(InkboxAdapter)
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"contact-123": "imessage"}
    adapter._last_inbound_imessage = {
        "contact-123": {
            "conversation_id": "stale-conv",
            "remote_number": "+15555550101",
            "message_id": "im-old",
        },
    }
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"

    result = asyncio.run(adapter.send(
        "contact-123",
        "reply",
        metadata={"mode": "imessage", "thread_id": "imessage:imconv-456"},
    ))

    assert result.success is True
    assert identity.sent_imessages == [{"conversation_id": "imconv-456", "text": "reply"}]


def test_inbound_imessage_builds_marker_and_stashes_state(monkeypatch):
    identity = FakeIdentity()

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _resolve_contact_full(**_kwargs):
        return {"id": "contact-123", "name": "Alex"}

    events = []

    async def _enqueue_sms_text_event(event):
        events.append(event)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_imessage = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue_sms_text_event = _enqueue_sms_text_event

    response = asyncio.run(adapter._on_imessage_received({
        "event_type": "imessage.received",
        "data": {
            "message": {
                "id": "im-in",
                "direction": "inbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-123",
                "content": "Dinner moved to 7.",
            },
        },
    }))

    assert response.status == 200
    assert len(events) == 1
    assert events[0].text.startswith("[inkbox:imessage from=+15555550101 conversation_id=imconv-123")
    assert "Dinner moved to 7." in events[0].text
    assert events[0].source.thread_id == "imessage:imconv-123"
    assert adapter._last_inbound_modality["contact-123"] == "imessage"
    assert adapter._last_inbound_imessage["contact-123"]["conversation_id"] == "imconv-123"
    assert adapter._last_inbound_imessage["contact-123|imessage:imconv-123"]["remote_number"] == "+15555550101"


def test_unknown_inbound_imessage_uses_conversation_session_key(monkeypatch):
    identity = FakeIdentity()

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _resolve_contact_full(**_kwargs):
        return None

    events = []

    async def _enqueue_sms_text_event(event):
        events.append(event)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_imessage = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue_sms_text_event = _enqueue_sms_text_event

    response = asyncio.run(adapter._on_imessage_received({
        "event_type": "imessage.received",
        "data": {
            "message": {
                "id": "im-in",
                "direction": "inbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-unknown",
                "content": "Dinner moved to 7.",
            },
        },
    }))

    assert response.status == 200
    assert events[0].source.chat_id == "imessage:imconv-unknown"
    assert events[0].source.thread_id == "imessage:imconv-unknown"
    assert adapter._last_inbound_modality["imessage:imconv-unknown"] == "imessage"
    assert (
        adapter._last_inbound_imessage["imessage:imconv-unknown"]["conversation_id"]
        == "imconv-unknown"
    )


def test_duplicate_inbound_imessage_event_id_does_not_enqueue_twice(monkeypatch):
    identity = FakeIdentity()

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _resolve_contact_full(**_kwargs):
        return None

    events = []

    async def _enqueue_sms_text_event(event):
        events.append(event)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_imessage = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue_sms_text_event = _enqueue_sms_text_event
    envelope = {
        "event_type": "imessage.received",
        "data": {
            "message": {
                "id": "im-in",
                "direction": "inbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-unknown",
                "content": "Dinner moved to 7.",
            },
        },
    }

    first = asyncio.run(adapter._on_imessage_received(envelope))
    second = asyncio.run(adapter._on_imessage_received(envelope))

    assert first.status == 200
    assert second.text == "duplicate"
    assert len(events) == 1


def test_inbound_imessage_ignores_outbound_echo(monkeypatch):
    events = []

    async def _enqueue_sms_text_event(event):
        events.append(event)

    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._seen_request_ids = {}
    adapter._enqueue_sms_text_event = _enqueue_sms_text_event

    response = asyncio.run(adapter._on_imessage_received({
        "event_type": "imessage.received",
        "data": {
            "message": {
                "id": "im-out",
                "direction": "outbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-123",
                "content": "agent reply",
            },
        },
    }))

    assert response.status == 200
    assert events == []


def test_direct_send_accepts_imessage_conversation_target(monkeypatch):
    identity = FakeIdentity()

    def _fake_inkbox(**_kwargs):
        return FakeInkboxClient(identity)

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    monkeypatch.setattr("inkbox_plugin.adapter.Inkbox", _fake_inkbox)
    monkeypatch.setattr("inkbox_plugin.adapter.INKBOX_AVAILABLE", True)

    out = asyncio.run(send_inkbox_direct(
        {"api_key": "ApiKey_test", "identity": "agent"},
        "imessage:conversation:imconv-123",
        "cron says hi",
    ))

    assert out["success"] is True
    assert out["mode"] == "imessage"
    assert identity.sent_imessages == [{"conversation_id": "imconv-123", "text": "cron says hi"}]


def test_list_imessage_assignments_tool(monkeypatch):
    identity = FakeIdentity()
    identity.list_imessage_assignments = lambda **kwargs: (
        identity.calls.append(("list_imessage_assignments", kwargs))
        or [{"id": "assign-1", "remote_number": "+15555550101", "status": "active"}]
    )
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))

    out = json.loads(tools.inkbox_list_imessage_assignments({}))

    assert out["ok"] is True
    assert out["count"] == 1
    assert out["assignments"][0]["remote_number"] == "+15555550101"
    assert identity.calls == [("list_imessage_assignments", {"limit": 25, "offset": 0})]


def test_imessage_lifecycle_event_logs_without_agent_turn(monkeypatch):
    enqueued = []

    async def _enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._enqueue = _enqueue

    response = asyncio.run(adapter._on_imessage_lifecycle({
        "event_type": "imessage.delivered",
        "data": {
            "message": {
                "id": "im-1",
                "direction": "outbound",
                "remote_number": "+15555550101",
                "status": "delivered",
            },
        },
    }))

    assert response.status == 200
    assert enqueued == []


def test_reaction_received_is_subscribed():
    # The agent must be woken for inbound tapbacks so it can decide whether
    # to reply or stay [SILENT].
    assert "imessage.reaction_received" in adapter_mod._DESIRED_IMESSAGE_EVENTS


def test_inbound_imessage_starts_typing_pulse(monkeypatch):
    identity = FakeIdentity()

    async def _resolve_contact_full(**_kwargs):
        return {"id": "contact-123", "name": "Alex"}

    async def _enqueue_sms_text_event(event):
        # The pulse is started right before enqueue in the real handler.
        pass

    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )

    async def _run():
        adapter = object.__new__(InkboxAdapter)
        adapter._inkbox = FakeInkboxClient(identity)
        adapter._identity_handle = "agent"
        adapter._seen_request_ids = {}
        adapter._last_inbound_modality = {}
        adapter._last_inbound_imessage = {}
        adapter._resolve_contact_full = _resolve_contact_full
        adapter._enqueue_sms_text_event = _enqueue_sms_text_event

        response = await adapter._on_imessage_received({
            "event_type": "imessage.received",
            "data": {
                "message": {
                    "id": "im-in",
                    "direction": "inbound",
                    "remote_number": "+15555550101",
                    "conversation_id": "imconv-123",
                    "content": "ping",
                },
            },
        })
        # A typing pulse task is now running for this conversation.
        assert "imconv-123" in adapter._typing_tasks()
        # Cancel it the way send() would, so the test loop exits cleanly.
        adapter._stop_imessage_typing("imconv-123")
        assert "imconv-123" not in adapter._typing_tasks()
        return response

    response = asyncio.run(_run())
    assert response.status == 200


def test_imessage_typing_safety_cap_is_ten_minutes():
    assert InkboxAdapter.IMESSAGE_TYPING_MAX_SECONDS == 600.0


def test_inbound_reaction_enqueues_turn_with_silent_policy(monkeypatch):
    identity = FakeIdentity()

    async def _resolve_contact_full(**_kwargs):
        return {"id": "contact-123", "name": "Alex"}

    enqueued = []

    async def _enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_imessage = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue

    async def _run():
        response = await adapter._on_imessage_reaction({
            "event_type": "imessage.reaction_received",
            "data": {
                "reaction": {
                    "id": "react-in-1",
                    "direction": "inbound",
                    "remote_number": "+15555550101",
                    "conversation_id": "imconv-123",
                    "target_message_id": "im-target-9",
                    "reaction": "question",
                },
            },
        })
        # A "question" tapback usually warrants a reply — check the typing
        # pulse started before the run loop tears down (and cancel it).
        assert "imconv-123" in adapter._typing_tasks()
        adapter._stop_imessage_typing("imconv-123")
        return response

    response = asyncio.run(_run())

    assert response.status == 200
    assert len(enqueued) == 1
    text = enqueued[0].text
    assert text.startswith(
        "[inkbox:imessage_reaction from=+15555550101 reaction=question"
    )
    assert "conversation_id=imconv-123" in text
    assert "target_message_id=im-target-9" in text
    assert "[SILENT]" in text  # the agent is told it may stay silent
    # Reply target is stashed so a follow-up send lands in the right thread.
    assert adapter._last_inbound_imessage["contact-123"]["conversation_id"] == "imconv-123"
    assert adapter._last_inbound_modality["contact-123"] == "imessage"


def test_unknown_imessage_reaction_uses_conversation_session_key(monkeypatch):
    identity = FakeIdentity()

    async def _resolve_contact_full(**_kwargs):
        return None

    enqueued = []

    async def _enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_imessage = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue

    response = asyncio.run(adapter._on_imessage_reaction({
        "event_type": "imessage.reaction_received",
        "data": {
            "reaction": {
                "id": "react-in-2",
                "direction": "inbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-unknown",
                "target_message_id": "im-target-10",
                "reaction": "like",
            },
        },
    }))

    assert response.status == 200
    assert enqueued[0].source.chat_id == "imessage:imconv-unknown"
    assert enqueued[0].source.thread_id == "imessage:imconv-unknown"
    assert adapter._last_inbound_modality["imessage:imconv-unknown"] == "imessage"
    assert (
        adapter._last_inbound_imessage["imessage:imconv-unknown"]["conversation_id"]
        == "imconv-unknown"
    )


def test_duplicate_imessage_reaction_event_id_does_not_enqueue_twice(monkeypatch):
    identity = FakeIdentity()

    async def _resolve_contact_full(**_kwargs):
        return None

    enqueued = []

    async def _enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_imessage = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue
    envelope = {
        "event_type": "imessage.reaction_received",
        "data": {
            "reaction": {
                "id": "react-in-2",
                "direction": "inbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-unknown",
                "target_message_id": "im-target-10",
                "reaction": "like",
            },
        },
    }

    first = asyncio.run(adapter._on_imessage_reaction(envelope))
    second = asyncio.run(adapter._on_imessage_reaction(envelope))

    assert first.status == 200
    assert second.text == "duplicate"
    assert len(enqueued) == 1


def test_inbound_non_question_reaction_does_not_type(monkeypatch):
    identity = FakeIdentity()

    async def _resolve_contact_full(**_kwargs):
        return {"id": "contact-123", "name": "Alex"}

    enqueued = []

    async def _enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_imessage = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue

    response = asyncio.run(adapter._on_imessage_reaction({
        "event_type": "imessage.reaction_received",
        "data": {
            "reaction": {
                "id": "react-in-2",
                "direction": "inbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-123",
                "target_message_id": "im-target-9",
                "reaction": "love",
            },
        },
    }))

    assert response.status == 200
    assert len(enqueued) == 1
    # A 'love' tapback usually resolves to [SILENT]; don't promise a reply.
    assert "imconv-123" not in adapter._typing_tasks()


def test_inbound_reaction_ignores_outbound_echo(monkeypatch):
    # The agent's own tapbacks echo back as reaction_received webhooks.
    enqueued = []

    async def _enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    adapter = object.__new__(InkboxAdapter)
    adapter._seen_request_ids = {}
    adapter._enqueue = _enqueue

    response = asyncio.run(adapter._on_imessage_reaction({
        "event_type": "imessage.reaction_received",
        "data": {
            "reaction": {
                "id": "react-out-1",
                "direction": "outbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-123",
                "target_message_id": "im-target-9",
                "reaction": "like",
            },
        },
    }))

    assert response.status == 200
    assert enqueued == []
