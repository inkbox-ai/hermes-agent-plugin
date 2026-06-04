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


def _mail_envelope(from_address, *, agent_identities=None):
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
    adapter._identity_handle = "agent"
    adapter._identity_id = None
    adapter._identity_email_addresses = {"agent@inkboxmail.com"}
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
