import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter
from inkbox_plugin.config import VoiceStack


class _Subscriptions:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.deleted = []

    def list(self, **_owner):
        return list(self.rows)

    def create(self, **kwargs):
        desired_families = {
            event_type.split(".", 1)[0] for event_type in kwargs["event_types"]
        }
        assert all(
            row.url != kwargs["url"]
            or not desired_families
            & {
                event_type.split(".", 1)[0]
                for event_type in row.event_types
            }
            for row in self.rows
        )
        row = SimpleNamespace(
            id=f"sub-{len(self.rows) + 1}",
            url=kwargs["url"],
            event_types=list(kwargs["event_types"]),
        )
        self.rows.append(row)
        return row

    def update(self, sub_id, *, event_types, url=None):
        row = next(row for row in self.rows if row.id == sub_id)
        if url is not None:
            row.url = url
        row.event_types = list(event_types)
        return row

    def delete(self, sub_id):
        self.deleted.append(sub_id)
        self.rows = [row for row in self.rows if row.id != sub_id]


def _client(rows=()):
    subscriptions = _Subscriptions(rows)
    return (
        SimpleNamespace(
            webhooks=SimpleNamespace(subscriptions=subscriptions),
        ),
        subscriptions,
    )


def _reconcile(client, url, events, previous=None):
    return adapter._reconcile_imessage_subscription(
        client,
        "identity-1",
        desired_url=url,
        previous_webhook_url=previous,
        desired_events=events,
    )


def test_a2a_and_imessage_share_the_canonical_url():
    client, subscriptions = _client()
    base = "https://agent.example/webhook"

    _reconcile(client, base, adapter._DESIRED_A2A_EVENTS)
    _reconcile(client, base, adapter._DESIRED_IMESSAGE_EVENTS)

    assert [(row.url, tuple(row.event_types)) for row in subscriptions.rows] == [
        (base, adapter._DESIRED_A2A_EVENTS),
        (base, adapter._DESIRED_IMESSAGE_EVENTS),
    ]


def test_reconcile_keeps_one_row_per_channel_and_receiver():
    base = "https://agent.example/webhook"
    extra_a2a = SimpleNamespace(
        id="sub-a2a-extra",
        url=f"{base}?unused=true",
        event_types=list(adapter._DESIRED_A2A_EVENTS),
    )
    imessage = SimpleNamespace(
        id="sub-imessage",
        url=base,
        event_types=list(adapter._DESIRED_IMESSAGE_EVENTS),
    )
    client, subscriptions = _client([extra_a2a, imessage])

    _reconcile(client, base, adapter._DESIRED_A2A_EVENTS)

    assert subscriptions.deleted == []
    assert [(row.url, tuple(row.event_types)) for row in subscriptions.rows] == [
        (base, adapter._DESIRED_A2A_EVENTS),
        (base, adapter._DESIRED_IMESSAGE_EVENTS),
    ]
    assert "sub-imessage" not in subscriptions.deleted
    assert any(row.id == "sub-imessage" for row in subscriptions.rows)


def _patchable_adapter(monkeypatch, voice_stack):
    incoming_updates = []
    identity = SimpleNamespace(
        id="identity-1",
        mailbox=None,
        phone_number=SimpleNamespace(
            id="phone-1",
            number="+15551234567",
        ),
        imessage_enabled=False,
        set_incoming_call_action=lambda **kwargs: incoming_updates.append(kwargs),
    )
    instance = adapter.InkboxAdapter.__new__(adapter.InkboxAdapter)
    instance._public_url = "https://voice-agent.inkboxwire.com"
    instance._public_host = "voice-agent.inkboxwire.com"
    instance._webhook_path = "/webhook"
    instance._ws_path = "/phone/media/ws"
    instance._identity_handle = "voice-agent"
    instance._voice_stack = voice_stack
    instance._inkbox = SimpleNamespace(get_identity=lambda _handle: identity)
    instance._write_identity_state = lambda *_args: None

    monkeypatch.setattr(adapter, "_read_previous_webhook_url", lambda: None)
    monkeypatch.setattr(adapter, "_reconcile_text_subscription", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "_reconcile_imessage_subscription", lambda *_args, **_kwargs: None)
    return instance, incoming_updates


def test_voice_ai_startup_reconciles_hosted_incoming_calls(monkeypatch):
    instance, incoming_updates = _patchable_adapter(
        monkeypatch,
        VoiceStack.INKBOX_VOICE_AI,
    )

    instance._patch_identity_objects()

    assert incoming_updates == [
        {
            "incoming_call_action": "hosted_agent",
            "client_websocket_url": None,
            "incoming_call_webhook_url": None,
        }
    ]


@pytest.mark.parametrize(
    "voice_stack",
    [VoiceStack.INKBOX_TTS_STT, VoiceStack.OPENAI_REALTIME],
)
def test_local_voice_stack_startup_reconciles_media_websocket(
    monkeypatch, voice_stack,
):
    instance, incoming_updates = _patchable_adapter(
        monkeypatch,
        voice_stack,
    )

    instance._patch_identity_objects()

    assert incoming_updates == [
        {
            "incoming_call_action": "auto_accept",
            "client_websocket_url": "wss://voice-agent.inkboxwire.com/phone/media/ws",
            "incoming_call_webhook_url": "https://voice-agent.inkboxwire.com/webhook",
        }
    ]


def test_identity_subscribes_to_call_ended_without_imessage(monkeypatch):
    instance, _incoming_updates = _patchable_adapter(
        monkeypatch,
        VoiceStack.INKBOX_VOICE_AI,
    )
    reconciled = []
    monkeypatch.setattr(
        adapter,
        "_reconcile_imessage_subscription",
        lambda *_args, **kwargs: reconciled.append(kwargs),
    )

    instance._patch_identity_objects()

    base_rows = [
        row for row in reconciled
        if row["desired_url"] == "https://voice-agent.inkboxwire.com/webhook"
        and row["desired_events"] == adapter._DESIRED_CALL_EVENTS
    ]
    assert len(base_rows) == 1
    assert base_rows[0]["desired_events"] == ("call.ended",)
