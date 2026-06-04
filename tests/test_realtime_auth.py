import asyncio
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.realtime import (
    RealtimeConfig,
    _openai_realtime_ws_headers,
    _resolve_realtime_bearer,
)


def _clear_realtime_env(monkeypatch):
    monkeypatch.delenv("INKBOX_REALTIME_ENABLED", raising=False)
    monkeypatch.delenv("INKBOX_REALTIME_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("INKBOX_REALTIME_MODEL", raising=False)
    monkeypatch.delenv("INKBOX_REALTIME_VOICE", raising=False)
    monkeypatch.delenv("INKBOX_REALTIME_CONSULT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("INKBOX_REALTIME_CONNECT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS", raising=False)


def test_realtime_auto_stays_off_without_api_key(monkeypatch):
    _clear_realtime_env(monkeypatch)

    cfg = adapter_mod._resolve_realtime_config({})

    assert cfg.enabled is False
    assert cfg.api_key == ""


def test_realtime_api_key_enables_realtime(monkeypatch):
    _clear_realtime_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wins")

    cfg = adapter_mod._resolve_realtime_config({})

    assert cfg.enabled is True
    assert cfg.api_key == "sk-wins"


def test_realtime_explicit_enable_without_any_credential_stays_off(monkeypatch):
    _clear_realtime_env(monkeypatch)

    cfg = adapter_mod._resolve_realtime_config({"realtime": {"enabled": True}})

    assert cfg.enabled is False
    assert cfg.api_key == ""


def test_realtime_fallback_defaults_on_and_can_be_disabled(monkeypatch):
    _clear_realtime_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wins")

    cfg = adapter_mod._resolve_realtime_config({})
    assert cfg.fallback_to_inkbox_stt_tts is True

    cfg = adapter_mod._resolve_realtime_config({
        "realtime": {
            "enabled": True,
            "fallback_to_inkbox_stt_tts": False,
        }
    })
    assert cfg.fallback_to_inkbox_stt_tts is False

    monkeypatch.setenv("INKBOX_REALTIME_FALLBACK_TO_INKBOX_STT_TTS", "false")
    cfg = adapter_mod._resolve_realtime_config({})
    assert cfg.fallback_to_inkbox_stt_tts is False


class _FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self.payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def test_realtime_api_key_used_directly():
    session = _FakeSession(_FakeResponse())

    bearer = asyncio.run(
        _resolve_realtime_bearer(session, RealtimeConfig(api_key="sk-direct")),
    )

    assert bearer == "sk-direct"
    assert session.calls == []


def test_realtime_ws_headers_use_ga_shape():
    assert _openai_realtime_ws_headers("ek-secret") == {
        "Authorization": "Bearer ek-secret",
    }


def test_realtime_no_api_key_has_no_bearer():
    session = _FakeSession(_FakeResponse())

    bearer = asyncio.run(
        _resolve_realtime_bearer(session, RealtimeConfig()),
    )

    assert bearer == ""
    assert session.calls == []
