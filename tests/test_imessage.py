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
