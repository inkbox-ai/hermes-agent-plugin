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
    monkeypatch.setattr(adapter, "reconcile_identity_subscription", lambda *_args, **_kwargs: None)
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


@pytest.mark.parametrize("has_channels", [False, True])
def test_registers_one_identity_union_regardless_of_channels(monkeypatch, has_channels):
    instance, incoming = _patchable_adapter(monkeypatch, VoiceStack.INKBOX_VOICE_AI)
    identity = instance._inkbox.get_identity("voice-agent")
    identity.imessage_enabled = has_channels
    if not has_channels:
        identity.phone_number = None
    reconciled = []
    monkeypatch.setattr(adapter, "reconcile_identity_subscription", lambda *args: reconciled.append(args))
    instance._patch_identity_objects()
    assert len(reconciled) == 1
    assert reconciled[0][1] == identity.id
    assert set(reconciled[0][3]) == set(
        adapter._DESIRED_MAIL_EVENTS + adapter._DESIRED_TEXT_EVENTS + adapter._DESIRED_IMESSAGE_EVENTS
        + adapter._DESIRED_CALL_EVENTS + adapter._DESIRED_A2A_EVENTS
    )
    assert bool(incoming) is has_channels
