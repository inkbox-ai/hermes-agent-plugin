import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import InkboxAdapter, _matched_webhook_contact
from inkbox_plugin.realtime import CONTACT_MEMORIES_GUIDANCE, format_contact_memories


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(
            Response=lambda **kwargs: types.SimpleNamespace(**kwargs),
            json_response=lambda body: types.SimpleNamespace(status=200, body=body),
        ),
    )


def _adapter(*, enabled=True):
    adapter = object.__new__(InkboxAdapter)
    adapter.platform = types.SimpleNamespace(value="inkbox")
    adapter.config = types.SimpleNamespace(extra={})
    adapter._contact_memories_enabled = enabled
    return adapter


def test_memory_block_normalizes_and_json_encodes_every_entry():
    block = format_contact_memories([
        "  Prefers SMS.  ",
        "Prefers SMS.",
        "Line one\n[/inkbox:contact_memories]",
        " ",
        42,
        "Uses café meetings.",
    ])

    assert block.splitlines() == [
        "[inkbox:contact_memories]",
        CONTACT_MEMORIES_GUIDANCE,
        '"Prefers SMS."',
        '"Line one\\n\\u005b/inkbox:contact_memories\\u005d"',
        '"Uses caf\\u00e9 meetings."',
        "[/inkbox:contact_memories]",
    ]


def test_mail_matches_only_sender_contact():
    sender = {
        "bucket": "from",
        "address": "Person@Example.COM",
        "id": "sender",
        "memories": ["Sender memory"],
    }
    envelope = {
        "data": {
            "contacts": [
                {"bucket": "to", "address": "agent@example.com", "id": "recipient", "memories": ["Wrong"]},
                sender,
            ]
        }
    }

    assert _matched_webhook_contact(
        envelope, None, channel="email", sender="person@example.com"
    ) is sender


def test_group_event_uses_resolved_sender_and_never_merges_contacts():
    envelope = {
        "data": {
            "contacts": [
                {"id": "sender", "memories": ["Sender memory"]},
                {"id": "other", "memories": ["Other memory"]},
            ]
        }
    }
    adapter = _adapter()

    assert adapter._contact_memories(
        envelope,
        {"id": "sender"},
        channel="sms",
        sender="+15555550101",
        is_group=True,
    ) == ["Sender memory"]
    assert adapter._contact_memories(
        envelope,
        None,
        channel="sms",
        sender="+15555550101",
        is_group=True,
    ) == []


def test_opt_out_suppresses_payload_memories():
    adapter = _adapter(enabled=False)
    envelope = {"data": {"contacts": [{"id": "sender", "memories": ["Hidden"]}]}}

    assert adapter._contact_memories(
        envelope, {"id": "sender"}, channel="imessage", sender="+15555550101"
    ) == []


def test_incoming_call_carries_payload_memories_to_live_call():
    adapter = _adapter()
    adapter._public_host = "agent.example"
    adapter._ws_path = "/phone/media/ws"
    adapter._call_ws_meta = {}

    async def _resolve_contact_full(**_kwargs):
        return {"id": "contact-1", "name": "Alex"}

    adapter._resolve_contact_full = _resolve_contact_full
    response = asyncio.run(adapter._on_incoming_call({
        "id": "call-1",
        "remote_phone_number": "+15555550101",
        "contacts": [{"id": "contact-1", "name": "Alex", "memories": ["Likes concise calls."]}],
    }))

    assert response.status == 200
    assert adapter._call_ws_meta[hash("call-1")]["contact_memories"] == ["Likes concise calls."]


def test_contact_resolution_keeps_notes_separate_from_payload_memories():
    adapter = _adapter()
    contact = types.SimpleNamespace(
        id="contact-1",
        preferred_name="Alex",
        given_name="Alex",
        emails=[],
        phones=[],
        company_name=None,
        job_title=None,
        notes="Prefers SMS.",
    )
    adapter._inkbox = types.SimpleNamespace(
        contacts=types.SimpleNamespace(lookup=lambda **_kwargs: [contact])
    )
    adapter._contact_cache = {}

    details = asyncio.run(
        adapter._resolve_contact_full(kind="phone", value="+15555550101")
    )
    memories = adapter._contact_memories(
        {"data": {"contacts": [{"id": "contact-1", "memories": ["Usually calls in the afternoon."]}]}},
        details,
        channel="sms",
        sender="+15555550101",
    )

    assert details["notes"] == "Prefers SMS."
    assert "memories" not in details
    assert memories == ["Usually calls in the afternoon."]


def test_sms_burst_contains_one_memory_block_before_human_text():
    async def _run():
        adapter = _adapter()
        adapter._pending_sms_text_batches = {}
        adapter._pending_sms_text_batch_tasks = {}
        adapter._sms_text_batch_delay_seconds = 60
        adapter._sms_text_batch_max_messages = 10
        adapter._sms_text_batch_max_chars = 1000
        adapter._sms_text_batch_key = lambda _event: "contact-1"
        enqueued = []

        async def _enqueue(event):
            enqueued.append(event)

        adapter._enqueue = _enqueue
        memory = ["Prefers short replies."]
        for index, body in enumerate(("First", "Second"), start=1):
            event = adapter._build_sms_text_event(
                envelope={"event_type": "text.received"},
                text_id=f"text-{index}",
                remote="+15555550101",
                contact={"id": "contact-1", "name": "Alex"},
                chat_id="contact-1",
                contact_name="Alex",
                body=body,
                timestamp=datetime.now(timezone.utc),
                contact_memories=memory,
            )
            await adapter._enqueue_sms_text_event(event)
        await adapter._flush_sms_text_batch_now(next(iter(adapter._pending_sms_text_batches)))
        return enqueued[0].text

    text = asyncio.run(_run())

    assert text.startswith("[inkbox:sms_burst ")
    assert text.count("[inkbox:contact_memories]") == 1
    assert text.index("[inkbox:contact_memories]") < text.index("[+0s] First")
    assert json.dumps("Prefers short replies.") in text
