import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.config import (
    InkboxPluginConfig,
    VoiceStack,
    public_call_ws_url,
    read_config,
)
from inkbox_plugin.adapter import (
    _A2A_PROGRESS_INTERVAL_SECONDS,
    _bool_setting,
    _float_setting,
)
from inkbox_plugin.tools import _append_query_param


def test_public_call_ws_url_from_public_https_url():
    cfg = InkboxPluginConfig(public_url="https://agent.example.com")

    assert public_call_ws_url(cfg) == "wss://agent.example.com/phone/media/ws"


def test_read_config_uses_sdk_base_url_default(monkeypatch):
    monkeypatch.delenv("INKBOX_BASE_URL", raising=False)

    assert read_config().base_url == ""


def test_read_config_uses_base_url_override(monkeypatch):
    monkeypatch.setenv("INKBOX_BASE_URL", "https://proxy.example")

    assert read_config().base_url == "https://proxy.example"


def test_public_call_ws_url_falls_back_to_identity_tunnel_name():
    cfg = InkboxPluginConfig(identity="demo-agent")

    assert public_call_ws_url(cfg) == "wss://demo-agent.inkboxwire.com/phone/media/ws"


def test_append_query_param_preserves_existing_query():
    out = _append_query_param("wss://agent.example.com/ws?x=1", "context_token", "abc")

    assert out == "wss://agent.example.com/ws?x=1&context_token=abc"


def test_contact_memories_platform_config_overrides_environment(monkeypatch):
    monkeypatch.setenv("INKBOX_CONTACT_MEMORIES_ENABLED", "true")

    assert _bool_setting(
        {"contact_memories_enabled": False},
        "contact_memories_enabled",
        "INKBOX_CONTACT_MEMORIES_ENABLED",
        True,
    ) is False
    assert read_config().contact_memories_enabled is True


def test_a2a_progress_interval_defaults_to_three_minutes(monkeypatch):
    monkeypatch.delenv("INKBOX_A2A_PROGRESS_INTERVAL_SECONDS", raising=False)

    assert _A2A_PROGRESS_INTERVAL_SECONDS == 180.0
    assert _float_setting(
        {},
        "a2a_progress_interval_seconds",
        "INKBOX_A2A_PROGRESS_INTERVAL_SECONDS",
        _A2A_PROGRESS_INTERVAL_SECONDS,
    ) == 180.0


def test_a2a_progress_interval_supports_config_and_disable(monkeypatch):
    monkeypatch.setenv("INKBOX_A2A_PROGRESS_INTERVAL_SECONDS", "90")

    assert _float_setting(
        {},
        "a2a_progress_interval_seconds",
        "INKBOX_A2A_PROGRESS_INTERVAL_SECONDS",
        _A2A_PROGRESS_INTERVAL_SECONDS,
    ) == 90.0
    assert _float_setting(
        {"a2a_progress_interval_seconds": 0},
        "a2a_progress_interval_seconds",
        "INKBOX_A2A_PROGRESS_INTERVAL_SECONDS",
        _A2A_PROGRESS_INTERVAL_SECONDS,
    ) == 0.0


def test_voice_stack_preserves_legacy_realtime_auto_detection(monkeypatch):
    monkeypatch.delenv("INKBOX_VOICE_STACK", raising=False)
    monkeypatch.delenv("INKBOX_REALTIME_ENABLED", raising=False)
    monkeypatch.setenv("INKBOX_REALTIME_API_KEY", "sk-existing")

    assert read_config().voice_stack is VoiceStack.OPENAI_REALTIME

    monkeypatch.setenv("INKBOX_REALTIME_ENABLED", "false")
    assert read_config().voice_stack is VoiceStack.INKBOX_TTS_STT


def test_explicit_voice_stack_is_canonical(monkeypatch):
    monkeypatch.setenv("INKBOX_REALTIME_API_KEY", "sk-existing")
    monkeypatch.setenv("INKBOX_REALTIME_ENABLED", "true")
    monkeypatch.setenv("INKBOX_VOICE_STACK", "inkbox_voice_ai")
    monkeypatch.setenv("INKBOX_VOICE_AI_AUTHORITY_MODE", "yolo")
    monkeypatch.setenv("INKBOX_VOICEMAIL_DETECTION", "disabled")

    cfg = read_config()

    assert cfg.voice_stack is VoiceStack.INKBOX_VOICE_AI
    assert cfg.voice_ai_authority_mode == "yolo"
    assert cfg.voicemail_detection == "disabled"
    assert cfg.voice_stack_invalid_value == ""


def test_invalid_voice_stack_fails_closed_to_tts(monkeypatch):
    monkeypatch.setenv("INKBOX_VOICE_STACK", "unknown")
    monkeypatch.setenv("INKBOX_REALTIME_API_KEY", "sk-existing")

    cfg = read_config()

    assert cfg.voice_stack is VoiceStack.INKBOX_TTS_STT
    assert cfg.voice_stack_invalid_value == "unknown"
