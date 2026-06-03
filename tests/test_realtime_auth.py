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
    REALTIME_CLIENT_SECRETS_URL,
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


def test_realtime_auto_enables_on_codex_oauth(monkeypatch):
    _clear_realtime_env(monkeypatch)
    monkeypatch.setattr(adapter_mod, "_pool_codex_oauth_token", lambda: "codex-token")

    cfg = adapter_mod._resolve_realtime_config({})

    assert cfg.enabled is True
    assert cfg.api_key == ""
    assert cfg.oauth_token == "codex-token"


def test_realtime_api_key_wins_over_codex_oauth(monkeypatch):
    _clear_realtime_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wins")
    monkeypatch.setattr(adapter_mod, "_pool_codex_oauth_token", lambda: "codex-token")

    cfg = adapter_mod._resolve_realtime_config({})

    assert cfg.enabled is True
    assert cfg.api_key == "sk-wins"
    assert cfg.oauth_token == ""


def test_realtime_explicit_enable_without_any_credential_stays_off(monkeypatch):
    _clear_realtime_env(monkeypatch)
    monkeypatch.setattr(adapter_mod, "_pool_codex_oauth_token", lambda: "")

    cfg = adapter_mod._resolve_realtime_config({"realtime": {"enabled": True}})

    assert cfg.enabled is False
    assert cfg.api_key == ""
    assert cfg.oauth_token == ""


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


def test_realtime_ws_headers_include_beta_flag():
    assert _openai_realtime_ws_headers("ek-secret") == {
        "Authorization": "Bearer ek-secret",
        "OpenAI-Beta": "realtime=v1",
    }


def test_realtime_oauth_mints_client_secret():
    session = _FakeSession(_FakeResponse(payload={"client_secret": {"value": "ek-ephemeral"}}))

    bearer = asyncio.run(
        _resolve_realtime_bearer(
            session,
            RealtimeConfig(oauth_token="oauth-token", model="gpt-realtime-2", voice="cedar"),
        ),
    )

    assert bearer == "ek-ephemeral"
    assert session.calls == [
        {
            "url": REALTIME_CLIENT_SECRETS_URL,
            "headers": {
                "Authorization": "Bearer oauth-token",
                "Content-Type": "application/json",
            },
            "json": {
                "session": {
                    "type": "realtime",
                    "model": "gpt-realtime-2",
                    "audio": {"output": {"voice": "cedar"}},
                },
            },
        },
    ]
