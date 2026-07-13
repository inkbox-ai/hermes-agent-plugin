"""Inbound sender labelling from backend-resolved agent identities.

When a 1:1 sender has no address-book contact but the webhook carries
exactly one resolved ``agent_identities`` entry, the modality marker names
that identity (id, handle, display name) instead of
``contact=unknown_in_inkbox``. An address-book contact always wins; zero
or several identities keep the unknown fallback (never guess); mail only
trusts a ``from``-bucket entry matching the normalized sender address.
"""

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


IDENTITY = {
    "id": "agent-42",
    "agent_handle": "atlas-agent",
    "display_name": "Atlas",
}


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )


def _adapter(*, contact=None):
    """Bare adapter with just the state the inbound paths touch."""
    adapter = object.__new__(InkboxAdapter)
    adapter.platform = "inkbox"
    adapter._inkbox = None
    adapter._identity_handle = "smoke-agent"
    adapter._identity_id = None
    # Pre-loaded self-mail state so the mail path never hits the network.
    adapter._identity_email_addresses_loaded = True
    adapter._identity_email_addresses = {"smoke-agent@inkboxmail.com"}
    adapter._seen_request_ids = {}
    adapter._inflight_request_ids = {}
    adapter._outbound_failure_state = {}
    adapter._last_inbound_modality = {}
    adapter._last_inbound_sms = {}
    adapter._last_inbound_imessage = {}
    adapter._last_inbound_email = {}
    adapter._start_imessage_typing = lambda *_a, **_k: None
    adapter._resolve_channel_overrides = lambda *_a, **_k: (None, None)

    async def _resolve_contact_full(**_kwargs):
        return contact

    adapter._resolve_contact_full = _resolve_contact_full
    adapter._enqueued = []

    async def _capture(event):
        adapter._enqueued.append(event)

    adapter._enqueue = _capture
    adapter._enqueue_sms_text_event = _capture
    return adapter


def _text_envelope(agent_identities, text_id="txt-in-1"):
    return {
        "id": f"evt-{text_id}",
        "event_type": "text.received",
        "data": {
            "contacts": [],
            "agent_identities": agent_identities,
            "text_message": {
                "id": text_id,
                "direction": "inbound",
                "local_phone_number": "+15555550100",
                "remote_phone_number": "+15555550101",
                "conversation_id": "conv-123",
                "text": "Hey from another agent.",
            },
        },
    }


def _imessage_envelope(agent_identities):
    return {
        "id": "evt-im-1",
        "event_type": "imessage.received",
        "data": {
            "contacts": [],
            "agent_identities": agent_identities,
            "message": {
                "id": "im-in-1",
                "direction": "inbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-1",
                "content": "iMessage from another agent.",
            },
        },
    }


def _reaction_envelope(agent_identities):
    return {
        "id": "evt-react-1",
        "event_type": "imessage.reaction_received",
        "data": {
            "contacts": [],
            "agent_identities": agent_identities,
            "reaction": {
                "id": "react-1",
                "direction": "inbound",
                "remote_number": "+15555550101",
                "conversation_id": "imconv-1",
                "target_message_id": "im-out-9",
                "reaction": "like",
            },
        },
    }


def _mail_envelope(agent_identities, from_address="atlas@inkboxmail.com"):
    return {
        "id": "evt-mail-1",
        "event_type": "message.received",
        "data": {
            "contacts": [],
            "agent_identities": agent_identities,
            "message": {
                "id": "mail-in-1",
                "thread_id": "thread-1",
                "message_id": "<mail-in-1@inkboxmail.com>",
                "from_address": from_address,
                "to_addresses": ["smoke-agent@inkboxmail.com"],
                "subject": "Coordinating",
                "snippet": "Email from another agent.",
                "direction": "inbound",
            },
        },
    }


def _only_event(adapter):
    assert len(adapter._enqueued) == 1
    return adapter._enqueued[0]


# ── SMS ──────────────────────────────────────────────────────────────────


def test_sms_single_agent_identity_labels_sender():
    adapter = _adapter(contact=None)

    asyncio.run(adapter._on_text_received(_text_envelope([IDENTITY])))

    event = _only_event(adapter)
    assert "contact=unknown_in_inkbox" not in event.text
    assert "contact_agent_identity_id=agent-42" in event.text
    assert "contact_agent_handle='atlas-agent'" in event.text
    assert "contact_name='Atlas'" in event.text
    # The display name also labels the session, not just the marker.
    assert event.source.user_name == "Atlas"


def test_sms_contact_match_wins_over_agent_identity():
    adapter = _adapter(contact={"id": "contact-9", "name": "Kim"})

    asyncio.run(adapter._on_text_received(_text_envelope([IDENTITY])))

    event = _only_event(adapter)
    assert "contact_id=contact-9" in event.text
    assert "contact_name='Kim'" in event.text
    assert "contact_agent" not in event.text


def test_sms_no_identities_keeps_unknown_fallback():
    adapter = _adapter(contact=None)

    asyncio.run(adapter._on_text_received(_text_envelope([])))

    assert "contact=unknown_in_inkbox" in _only_event(adapter).text


def test_sms_two_identities_keep_unknown_fallback():
    # Several identities means a group — no single sender to label.
    adapter = _adapter(contact=None)
    two = [
        IDENTITY,
        {"id": "agent-43", "agent_handle": "nova-agent", "display_name": "Nova"},
    ]

    asyncio.run(adapter._on_text_received(_text_envelope(two)))

    event = _only_event(adapter)
    assert "contact_agent" not in event.text
    assert "contact=unknown_in_inkbox" in event.text


def test_sms_identity_without_id_keeps_unknown_fallback():
    # No id — the backend did not resolve this entry, so it never labels.
    adapter = _adapter(contact=None)

    asyncio.run(adapter._on_text_received(
        _text_envelope([{"agent_handle": "no-id-agent", "display_name": "No Id"}])
    ))

    assert "contact=unknown_in_inkbox" in _only_event(adapter).text


# ── iMessage ─────────────────────────────────────────────────────────────


def test_imessage_single_agent_identity_labels_sender():
    adapter = _adapter(contact=None)

    asyncio.run(adapter._on_imessage_received(_imessage_envelope([IDENTITY])))

    event = _only_event(adapter)
    assert "contact=unknown_in_inkbox" not in event.text
    assert "contact_agent_identity_id=agent-42" in event.text
    assert "contact_agent_handle='atlas-agent'" in event.text
    assert event.source.user_name == "Atlas"


def test_imessage_two_identities_keep_unknown_fallback():
    # iMessage has no group split, so this directly exercises the
    # exactly-one rule: two entries must not collapse to the first.
    adapter = _adapter(contact=None)
    two = [
        IDENTITY,
        {"id": "agent-43", "agent_handle": "nova-agent", "display_name": "Nova"},
    ]

    asyncio.run(adapter._on_imessage_received(_imessage_envelope(two)))

    event = _only_event(adapter)
    assert "contact_agent" not in event.text
    assert "contact=unknown_in_inkbox" in event.text


def test_imessage_reaction_labels_sender():
    adapter = _adapter(contact=None)

    asyncio.run(adapter._on_imessage_reaction(_reaction_envelope([IDENTITY])))

    event = _only_event(adapter)
    assert "contact_agent_identity_id=agent-42" in event.text
    assert "Atlas reacted with a 'like' tapback" in event.text


# ── Mail ─────────────────────────────────────────────────────────────────


def test_mail_from_bucket_identity_labels_sender():
    adapter = _adapter(contact=None)
    identity = dict(IDENTITY, bucket="from", address="atlas@inkboxmail.com")

    asyncio.run(adapter._on_mail_received(_mail_envelope([identity])))

    event = _only_event(adapter)
    assert "contact=unknown_in_inkbox" not in event.text
    assert "contact_agent_identity_id=agent-42" in event.text
    assert "contact_agent_handle='atlas-agent'" in event.text
    assert event.source.user_name == "Atlas"


def test_mail_non_sender_bucket_identity_is_ignored():
    # The identity resolved for a recipient (``to``), not the sender — it
    # must not be surfaced as who wrote the mail.
    adapter = _adapter(contact=None)
    identity = dict(IDENTITY, bucket="to", address="smoke-agent@inkboxmail.com")

    asyncio.run(adapter._on_mail_received(_mail_envelope([identity])))

    event = _only_event(adapter)
    assert "contact_agent" not in event.text
    assert "contact=unknown_in_inkbox" in event.text


def test_mail_contact_match_wins_over_agent_identity():
    adapter = _adapter(contact={"id": "contact-9", "name": "Kim"})
    identity = dict(IDENTITY, bucket="from", address="atlas@inkboxmail.com")

    asyncio.run(adapter._on_mail_received(_mail_envelope([identity])))

    event = _only_event(adapter)
    assert "contact_id=contact-9" in event.text
    assert "contact_agent" not in event.text
