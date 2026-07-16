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
from inkbox_plugin.adapter import InkboxAdapter, send_inkbox_direct


def _new_test_adapter():
    adapter = object.__new__(InkboxAdapter)
    adapter.platform = types.SimpleNamespace(value="inkbox")
    return adapter


class FakeText:
    id = "txt-1"
    delivery_status = "queued"
    conversation_id = "conv-123"


class FakeIdentity:
    def __init__(self):
        self.sent_texts = []
        self.calls = []

    def send_text(self, **kwargs):
        self.sent_texts.append(kwargs)
        return FakeText()

    def list_text_conversations(self, **kwargs):
        self.calls.append(("list_text_conversations", kwargs))
        return [{"id": "conv-123", "is_group": True, "participants": ["+15555550101", "+15555550102"]}]

    def get_text_conversation(self, key, **kwargs):
        self.calls.append(("get_text_conversation", key, kwargs))
        return [{"id": "txt-1", "conversation_id": key}]

    def mark_text_conversation_read(self, key):
        self.calls.append(("mark_text_conversation_read", key))
        return types.SimpleNamespace(updated_count=2)


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


def test_send_sms_tool_prefers_conversation_id(monkeypatch):
    identity = FakeIdentity()
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))

    out = json.loads(tools.inkbox_send_sms({
        "conversationId": "conv-123",
        "text": "hello",
    }))

    assert out["ok"] is True
    assert identity.sent_texts == [{"text": "hello", "conversation_id": "conv-123"}]


def test_send_sms_tool_rejects_text_over_limit(monkeypatch):
    identity = FakeIdentity()
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))

    out = json.loads(tools.inkbox_send_sms({
        "conversationId": "conv-123",
        "text": "x" * (tools.SMS_MAX_LENGTH + 1),
    }))

    assert out["error_code"] == "sms_too_long"
    assert out["char_count"] == tools.SMS_MAX_LENGTH + 1
    assert identity.sent_texts == []


def test_sms_conversation_read_tools_use_conversation_id(monkeypatch):
    identity = FakeIdentity()
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))

    conversations = json.loads(tools.inkbox_list_text_conversations({"includeGroups": True}))
    history = json.loads(tools.inkbox_get_text_conversation({"conversationId": "conv-123"}))
    marked = json.loads(tools.inkbox_mark_text_conversation_read({"conversationId": "conv-123"}))

    assert conversations["count"] == 1
    assert history["texts"][0]["conversation_id"] == "conv-123"
    assert marked["updated_count"] == 2
    assert identity.calls == [
        ("list_text_conversations", {"limit": 25, "offset": 0, "include_groups": True}),
        ("get_text_conversation", "conv-123", {"limit": 50, "offset": 0}),
        ("mark_text_conversation_read", "conv-123"),
    ]


def test_adapter_sms_reply_uses_last_inbound_conversation_id(monkeypatch):
    identity = FakeIdentity()

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    adapter = _new_test_adapter()
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"contact-123": "sms"}
    adapter._last_inbound_sms = {
        "contact-123": {
            "conversation_id": "conv-123",
            "remote_phone_number": "+15555550101",
            "text_id": "txt-in",
        },
    }
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"

    result = asyncio.run(adapter.send("contact-123", "reply", metadata={"mode": "sms"}))

    assert result.success is True
    assert identity.sent_texts == [{"conversation_id": "conv-123", "text": "reply"}]


def test_adapter_sms_reply_rejects_text_over_limit():
    identity = FakeIdentity()
    adapter = _new_test_adapter()
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"contact-123": "sms"}
    adapter._last_inbound_sms = {
        "contact-123": {
            "conversation_id": "conv-123",
            "remote_phone_number": "+15555550101",
            "text_id": "txt-in",
        },
    }
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"

    result = asyncio.run(adapter.send("contact-123", "x" * (adapter_mod.SMS_MAX_LENGTH + 1), metadata={"mode": "sms"}))

    assert result.success is False
    assert result.raw_response["error_code"] == "sms_too_long"
    assert result.raw_response["char_count"] == adapter_mod.SMS_MAX_LENGTH + 1
    assert identity.sent_texts == []


def test_adapter_sms_reply_uses_thread_conversation_id(monkeypatch):
    identity = FakeIdentity()

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    adapter = _new_test_adapter()
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"contact-123": "sms"}
    adapter._last_inbound_sms = {
        "contact-123": {
            "conversation_id": "stale-conv",
            "remote_phone_number": "+15555550101",
            "text_id": "txt-old",
        },
    }
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"

    result = asyncio.run(adapter.send(
        "contact-123",
        "reply",
        metadata={"mode": "sms", "thread_id": "sms:conv-456"},
    ))

    assert result.success is True
    assert identity.sent_texts == [{"conversation_id": "conv-456", "text": "reply"}]


def test_inbound_group_sms_injects_silence_policy(monkeypatch):
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
    adapter = _new_test_adapter()
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_sms = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue_sms_text_event = _enqueue_sms_text_event

    response = asyncio.run(adapter._on_text_received({
        "event_type": "text.received",
        "data": {
            "text_message": {
                "id": "txt-in",
                "direction": "inbound",
                "remote_phone_number": "+15555550101",
                "local_phone_number": "+15555550100",
                "conversation_id": "conv-123",
                "text": "Dinner moved to 7.",
            },
        },
    }))

    assert response.status == 200
    assert len(events) == 1
    assert events[0].text.startswith("[inkbox:group_sms conversation_id=conv-123")
    assert "reply_mode=conversation_id" in events[0].text
    assert "participants=+15555550101,+15555550102" in events[0].text
    assert "Group SMS response policy" in events[0].text
    assert "return exactly [SILENT]" in events[0].text
    assert events[0].source.thread_id == "sms:conv-123"
    assert adapter._last_inbound_sms["contact-123"]["conversation_id"] == "conv-123"
    assert adapter._last_inbound_sms["contact-123|sms:conv-123"]["conversation_kind"] == "group"


def test_unknown_inbound_sms_uses_conversation_session_key(monkeypatch):
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
    adapter = _new_test_adapter()
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_sms = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue_sms_text_event = _enqueue_sms_text_event

    response = asyncio.run(adapter._on_text_received({
        "event_type": "text.received",
        "data": {
            "text_message": {
                "id": "txt-direct",
                "direction": "inbound",
                "remote_phone_number": "+15555550101",
                "conversation_id": "conv-direct",
                "text": "Hello.",
            },
        },
    }))

    assert response.status == 200
    assert events[0].source.chat_id == "sms:conv-direct"
    assert events[0].source.thread_id == "sms:conv-direct"
    assert adapter._last_inbound_modality["sms:conv-direct"] == "sms"
    assert adapter._last_inbound_sms["sms:conv-direct"]["conversation_id"] == "conv-direct"


def test_duplicate_inbound_sms_event_id_does_not_enqueue_twice(monkeypatch):
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
    adapter = _new_test_adapter()
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"
    adapter._seen_request_ids = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_sms = {}
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue_sms_text_event = _enqueue_sms_text_event
    envelope = {
        "event_type": "text.received",
        "data": {
            "text_message": {
                "id": "txt-direct",
                "direction": "inbound",
                "remote_phone_number": "+15555550101",
                "conversation_id": "conv-direct",
                "text": "Hello.",
            },
        },
    }

    first = asyncio.run(adapter._on_text_received(envelope))
    second = asyncio.run(adapter._on_text_received(envelope))

    assert first.status == 200
    assert second.text == "duplicate"
    assert len(events) == 1


def test_direct_send_accepts_sms_conversation_target(monkeypatch):
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
        "sms:conversation:conv-123",
        "hello",
    ))

    assert out["success"] is True
    assert out["conversation_id"] == "conv-123"
    assert identity.sent_texts == [{"conversation_id": "conv-123", "text": "hello"}]
