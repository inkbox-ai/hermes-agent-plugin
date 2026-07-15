import asyncio
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import InkboxAdapter


def _mail_envelope(from_address, *, agent_identities=None, headers=None):
    return {
        "event_type": "message.received",
        "timestamp": "2026-05-21T00:00:00Z",
        "data": {
            "message": {
                "id": "mail-in-1",
                "mailbox_id": "mailbox-1",
                "thread_id": "thread-1",
                "message_id": "<mail-in-1@example.com>",
                "from_address": from_address,
                "to_addresses": ["agent@inkboxmail.com"],
                "cc_addresses": None,
                "bcc_addresses": None,
                "subject": "Loop test",
                "snippet": "Please reply to yourself.",
                "headers": headers,
                "direction": "inbound",
                "status": "received",
                "has_attachments": False,
                "created_at": "2026-05-21T00:00:00Z",
            },
            "contacts": [],
            "agent_identities": agent_identities or [],
        },
    }


def _adapter_for_self_mail_check():
    adapter = object.__new__(InkboxAdapter)
    adapter.platform = "inkbox"
    adapter._identity_handle = "agent"
    adapter._identity_id = None
    adapter._identity_email_addresses = {"agent@inkboxmail.com"}
    adapter._identity_email_addresses_loaded = True
    adapter._inkbox = None
    return adapter


def test_inbound_email_from_own_mailbox_does_not_wake_agent(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    calls = {"resolve": 0, "enqueue": 0}

    async def _resolve_contact_full(**_kwargs):
        calls["resolve"] += 1
        return None

    async def _enqueue(_event):
        calls["enqueue"] += 1

    adapter = _adapter_for_self_mail_check()
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue

    response = asyncio.run(
        adapter._on_mail_received(_mail_envelope("Agent <agent@inkboxmail.com>"))
    )

    assert response.status == 200
    assert calls == {"resolve": 0, "enqueue": 0}


def test_self_mail_check_caches_identity_with_no_mailbox_address(monkeypatch):
    calls = {"get_identity": 0}

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    class FakeInkbox:
        def get_identity(self, _handle):
            calls["get_identity"] += 1
            return types.SimpleNamespace(id="identity-1", mailbox=None, email_address=None)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    adapter = object.__new__(InkboxAdapter)
    adapter._identity_handle = "agent"
    adapter._identity_id = None
    adapter._identity_email_addresses = set()
    adapter._identity_email_addresses_loaded = False
    adapter._inkbox = FakeInkbox()

    envelope = _mail_envelope("person@example.com")

    assert asyncio.run(adapter._is_self_mail_received(envelope, "person@example.com")) is False
    assert asyncio.run(adapter._is_self_mail_received(envelope, "person@example.com")) is False
    assert calls["get_identity"] == 1


def test_inbound_email_from_same_agent_identity_marker_does_not_wake_agent(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    calls = {"resolve": 0, "enqueue": 0}

    async def _resolve_contact_full(**_kwargs):
        calls["resolve"] += 1
        return None

    async def _enqueue(_event):
        calls["enqueue"] += 1

    adapter = _adapter_for_self_mail_check()
    adapter._identity_email_addresses = set()
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue

    response = asyncio.run(
        adapter._on_mail_received(
            _mail_envelope(
                "alias@inkboxmail.com",
                agent_identities=[
                    {
                        "bucket": "from",
                        "address": "alias@inkboxmail.com",
                        "id": "identity-1",
                        "agent_handle": "agent",
                        "display_name": "Agent",
                    },
                ],
            )
        )
    )

    assert response.status == 200
    assert calls == {"resolve": 0, "enqueue": 0}


def test_unknown_inbound_email_uses_thread_session_key(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    events = []

    async def _resolve_contact_full(**_kwargs):
        return None

    async def _enqueue(event):
        events.append(event)

    adapter = _adapter_for_self_mail_check()
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue
    adapter._last_inbound_email = {}
    adapter._last_inbound_modality = {}
    adapter._resolve_channel_overrides = lambda *_args, **_kwargs: (None, None)

    response = asyncio.run(adapter._on_mail_received(_mail_envelope("person@example.com")))

    assert response.status == 200
    assert events[0].source.chat_id == "email:thread-1"
    assert events[0].source.thread_id == "email:thread-1"
    assert adapter._last_inbound_modality["email:thread-1"] == "email"
    assert adapter._last_inbound_email["email:thread-1"]["from_address"] == "person@example.com"
    assert adapter._last_inbound_email["email:thread-1"]["sender_is_automated"] is False


def test_automated_inbound_email_marks_thread_non_replyable(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )
    events = []

    async def _resolve_contact_full(**_kwargs):
        return None

    async def _enqueue(event):
        events.append(event)

    adapter = _adapter_for_self_mail_check()
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueue = _enqueue
    adapter._last_inbound_email = {}
    adapter._last_inbound_modality = {}
    adapter._resolve_channel_overrides = lambda *_args, **_kwargs: (None, None)

    response = asyncio.run(
        adapter._on_mail_received(
            _mail_envelope(
                "updates@example.com",
                headers=[{"name": "Auto-Submitted", "value": "auto-generated"}],
            )
        )
    )

    assert response.status == 200
    assert len(events) == 1
    assert adapter._last_inbound_email["email:thread-1"]["sender_is_automated"] is True


def test_email_thread_session_reply_uses_stashed_sender(monkeypatch):
    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    class FakeIdentity:
        def __init__(self):
            self.sent = []

        def send_email(self, **kwargs):
            self.sent.append(kwargs)
            return types.SimpleNamespace(id="msg-out")

    class FakeInkbox:
        def __init__(self, identity):
            self.identity = identity

        def get_identity(self, _handle):
            return self.identity

    identity = FakeIdentity()
    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)

    adapter = object.__new__(InkboxAdapter)
    adapter.platform = "inkbox"
    adapter._identity_handle = "agent"
    adapter._inkbox = FakeInkbox(identity)
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"email:thread-1": "email"}
    adapter._last_inbound_email = {
        "email:thread-1": {
            "subject": "Loop test",
            "rfc_message_id": "<mail-in-1@example.com>",
            "from_address": "person@example.com",
        },
    }

    result = asyncio.run(adapter.send("email:thread-1", "Reply body"))

    assert result.success is True
    assert identity.sent == [{
        "to": ["person@example.com"],
        "subject": "Re: Loop test",
        "body_text": "Reply body",
        "in_reply_to_message_id": "<mail-in-1@example.com>",
    }]


def test_email_thread_reply_to_automated_sender_is_suppressed(monkeypatch):
    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    class FakeIdentity:
        def __init__(self):
            self.sent = []

        def send_email(self, **kwargs):
            self.sent.append(kwargs)
            return types.SimpleNamespace(id="msg-out")

    class FakeInkbox:
        def __init__(self, identity):
            self.identity = identity

        def get_identity(self, _handle):
            return self.identity

    identity = FakeIdentity()
    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)

    adapter = object.__new__(InkboxAdapter)
    adapter.platform = "inkbox"
    adapter._identity_handle = "agent"
    adapter._inkbox = FakeInkbox(identity)
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"email:thread-1": "email"}
    adapter._last_inbound_email = {
        "email:thread-1": {
            "subject": "Loop test",
            "rfc_message_id": "<mail-in-1@example.com>",
            "from_address": "notifications@github.com",
            "sender_is_automated": True,
        },
    }
    adapter._stop_imessage_typing_for_chat = lambda *_args, **_kwargs: None

    result = asyncio.run(adapter.send("email:thread-1", "Reply body"))

    assert result.success is True
    assert result.message_id == "suppressed-automated-email-recipient"
    assert identity.sent == []


def test_explicit_automated_email_recipient_is_suppressed(monkeypatch):
    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    class FakeIdentity:
        def __init__(self):
            self.sent = []

        def send_email(self, **kwargs):
            self.sent.append(kwargs)
            return types.SimpleNamespace(id="msg-out")

    class FakeInkbox:
        def __init__(self, identity):
            self.identity = identity

        def get_identity(self, _handle):
            return self.identity

    identity = FakeIdentity()
    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)

    adapter = object.__new__(InkboxAdapter)
    adapter.platform = "inkbox"
    adapter._identity_handle = "agent"
    adapter._inkbox = FakeInkbox(identity)
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_email = {}
    adapter._stop_imessage_typing_for_chat = lambda *_args, **_kwargs: None

    result = asyncio.run(
        adapter.send(
            "contact-123",
            "Reply body",
            metadata={"mode": "email", "to_email": "no-reply@mail.haft.sh"},
        )
    )

    assert result.success is True
    assert result.message_id == "suppressed-automated-email-recipient"
    assert identity.sent == []
