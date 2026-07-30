"""Outbound-call line resolution: explicit choice, capability fallback, and
channel-aware defaulting when the identity has BOTH a dedicated number and
iMessage enabled.

Regression guard for the bug where an agent on an iMessage conversation asked
to "call me" and the call went out over the dedicated number instead of the
shared iMessage line.
"""

import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import tools
from inkbox_plugin.config import VoiceStack, set_runtime_config_extra


def _identity(has_number: bool, imessage: bool):
    return types.SimpleNamespace(
        phone_number=types.SimpleNamespace(number="+15550000000") if has_number else None,
        imessage_enabled=imessage,
    )


def _set_channel(monkeypatch, thread_id):
    # _current_channel_hint reads the host session var, falling back to
    # os.environ when the gateway isn't present (as in tests).
    if thread_id is None:
        monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_THREAD_ID", thread_id)


def test_single_line_resolves_unambiguously(monkeypatch):
    _set_channel(monkeypatch, None)
    assert tools._resolve_call_origination(_identity(True, False), "") == "dedicated_number"
    assert tools._resolve_call_origination(_identity(False, True), "") == "shared_imessage_number"
    assert tools._resolve_call_origination(_identity(False, False), "") is None


def test_explicit_choice_wins_over_channel(monkeypatch):
    _set_channel(monkeypatch, "imessage:conv")
    assert tools._resolve_call_origination(_identity(True, True), "dedicated_number") == "dedicated_number"
    _set_channel(monkeypatch, "sms:conv")
    assert tools._resolve_call_origination(_identity(True, True), "shared_imessage_number") == "shared_imessage_number"


def test_both_lines_follow_conversation_channel(monkeypatch):
    both = _identity(True, True)
    _set_channel(monkeypatch, "imessage:conv-1")
    assert tools._resolve_call_origination(both, "") == "shared_imessage_number"
    _set_channel(monkeypatch, "sms:conv-1")
    assert tools._resolve_call_origination(both, "") == "dedicated_number"
    _set_channel(monkeypatch, "phone:conv-1")
    assert tools._resolve_call_origination(both, "") == "dedicated_number"


def test_both_lines_unknown_channel_defaults_dedicated(monkeypatch):
    _set_channel(monkeypatch, None)
    assert tools._resolve_call_origination(_identity(True, True), "") == "dedicated_number"


def test_channel_only_breaks_ties(monkeypatch):
    # An iMessage-only identity stays shared even on an SMS-looking thread.
    _set_channel(monkeypatch, "sms:conv")
    assert tools._resolve_call_origination(_identity(False, True), "") == "shared_imessage_number"


def test_place_call_schema_is_mode_aware():
    hosted = tools.place_call_schema(VoiceStack.INKBOX_VOICE_AI)
    local = tools.place_call_schema(VoiceStack.INKBOX_TTS_STT)
    unresolved = tools.place_call_schema(None)

    assert "client_websocket_url" not in hosted["parameters"]["properties"]
    assert "notified after the call ends" in hosted["description"]
    assert "client_websocket_url" in local["parameters"]["properties"]
    assert "client_websocket_url" not in unresolved["parameters"]["properties"]


def test_hosted_reason_is_always_bounded():
    reason = tools._hosted_call_reason({
        "purpose": "p" * 1_500,
        "context": "c" * 1_500,
    })

    assert len(reason) == 2_000
    assert reason.endswith("…")


def test_hosted_call_inherits_authority_and_skips_context_file(monkeypatch):
    calls = []

    class Identity:
        phone_number = types.SimpleNamespace(number="+15550000000")
        imessage_enabled = False

        def place_call(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(
                id="call-hosted",
                status="initiated",
                mode="hosted_agent",
                hosted_agent_authority_mode="yolo",
                voicemail_detection="disabled",
            )

    monkeypatch.setenv("INKBOX_VOICE_STACK", "inkbox_voice_ai")
    monkeypatch.setenv("INKBOX_VOICEMAIL_DETECTION", "disabled")
    monkeypatch.setattr(
        tools,
        "_client_and_identity",
        lambda: (tools.read_config(), object(), Identity()),
    )
    monkeypatch.setattr(
        tools,
        "_write_outbound_call_context",
        lambda _payload: (_ for _ in ()).throw(AssertionError("must not write")),
    )

    result = json.loads(tools.inkbox_place_call(
        {
            "to_number": "+15551112222",
            "purpose": "Confirm the release date",
            "opening_message": "Hello from Hermes",
            "context": "The release is planned for Friday.",
        },
        _registered_voice_stack=VoiceStack.INKBOX_VOICE_AI,
    ))

    assert result["ok"] is True
    assert result["mode"] == "hosted_agent"
    assert result["hosted_agent_authority_mode"] == "yolo"
    assert "context_token" not in result
    assert calls == [{
        "to_number": "+15551112222",
        "origination": "dedicated_number",
        "mode": "hosted_agent",
        "reason": (
            "Purpose: Confirm the release date\n"
            "Opening message: Hello from Hermes\n"
            "Context: The release is planned for Friday."
        ),
        "voicemail_detection": "disabled",
    }]
    assert "hosted_agent_authority_mode" not in calls[0]
    assert "client_websocket_url" not in calls[0]


def test_local_call_forwards_mode_voicemail_and_context_token(monkeypatch):
    calls = []

    class Identity:
        phone_number = types.SimpleNamespace(number="+15550000000")
        imessage_enabled = False

        def place_call(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(
                id="call-local",
                status="initiated",
                mode="client_websocket",
                voicemail_detection="disabled",
            )

    monkeypatch.setenv("INKBOX_VOICE_STACK", "inkbox_tts_stt")
    monkeypatch.setenv("INKBOX_VOICEMAIL_DETECTION", "disabled")
    monkeypatch.setattr(
        tools,
        "_client_and_identity",
        lambda: (tools.read_config(), object(), Identity()),
    )
    monkeypatch.setattr(
        tools, "_write_outbound_call_context", lambda _payload: "context-1",
    )

    result = json.loads(tools.inkbox_place_call(
        {
            "to_number": "+15551112222",
            "purpose": "Check in",
            "client_websocket_url": "wss://agent.example/ws",
        },
        _registered_voice_stack=VoiceStack.INKBOX_TTS_STT,
    ))

    assert result["context_token"] == "context-1"
    assert calls == [{
        "to_number": "+15551112222",
        "origination": "dedicated_number",
        "client_websocket_url": (
            "wss://agent.example/ws?context_token=context-1"
        ),
        "mode": "client_websocket",
        "voicemail_detection": "disabled",
    }]


def test_call_fails_when_registered_stack_is_stale(monkeypatch):
    monkeypatch.setenv("INKBOX_VOICE_STACK", "inkbox_voice_ai")
    monkeypatch.setattr(
        tools,
        "_client_and_identity",
        lambda: (tools.read_config(), object(), object()),
    )

    result = json.loads(tools.inkbox_place_call(
        {"to_number": "+15551112222", "purpose": "Check in"},
        _registered_voice_stack=VoiceStack.INKBOX_TTS_STT,
    ))

    assert "restart the hermes gateway" in result["error"].lower()


def test_yaml_voice_stack_controls_runtime_when_env_is_unset(monkeypatch):
    calls = []

    class Identity:
        phone_number = types.SimpleNamespace(number="+15550000000")
        imessage_enabled = False

        def place_call(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(id="call-yaml", status="initiated")

    monkeypatch.delenv("INKBOX_VOICE_STACK", raising=False)
    set_runtime_config_extra({
        "voice_stack": "inkbox_voice_ai",
        "voicemail_detection": "disabled",
    })
    monkeypatch.setattr(
        tools,
        "_client_and_identity",
        lambda: (tools.read_config(), object(), Identity()),
    )
    try:
        result = json.loads(tools.inkbox_place_call(
            {"to_number": "+15551112222", "purpose": "Check in"},
            _registered_voice_stack=None,
        ))
    finally:
        set_runtime_config_extra({})

    assert result["ok"] is True
    assert calls[0]["mode"] == "hosted_agent"
    assert calls[0]["voicemail_detection"] == "disabled"
