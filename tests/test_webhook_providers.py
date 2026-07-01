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
from inkbox_plugin import webhook_providers as wp
from inkbox_plugin.adapter import InkboxAdapter


class _FakeRequest:
    def __init__(self, body, headers=None, *, request_id="req-wp-1"):
        self._body = body
        self.headers = {"X-Inkbox-Request-Id": request_id, **(headers or {})}
        self.url = "https://agent.example/webhook"

    async def read(self):
        return self._body


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )


def _adapter(*, require_signature, external_events_enabled):
    # Build an adapter without __init__ (no network/SDK), wiring only the state
    # the webhook auth path + external handler touch.
    adapter = object.__new__(InkboxAdapter)
    adapter._require_signature = require_signature
    adapter._external_events_enabled = external_events_enabled
    adapter._signing_key = "whsec_test"
    adapter._seen_request_ids = {}
    adapter._inflight_request_ids = {}
    adapter.platform = "inkbox"
    adapter._enqueued = []

    async def _capture(event):
        adapter._enqueued.append(event)

    adapter._enqueue = _capture
    adapter._resolve_channel_overrides = lambda *a, **k: (None, None)
    return adapter


# --- registry ------------------------------------------------------------

def test_match_provider_identifies_inkbox_by_header():
    provider = wp.match_provider({"X-Inkbox-Signature": "sha256=abc"})
    assert provider is not None and provider.name == "inkbox"


def test_match_provider_is_case_insensitive():
    provider = wp.match_provider({"x-inkbox-signature": "sha256=abc"})
    assert provider is not None and provider.name == "inkbox"


def test_match_provider_returns_none_for_unknown_source():
    # A third-party source we have not onboarded a verifier for.
    assert wp.match_provider({"X-Hub-Signature-256": "sha256=abc"}) is None


def test_inkbox_provider_delegates_to_sdk(monkeypatch):
    seen = {}

    def _fake_verify(*, payload, headers, secret):
        seen.update(payload=payload, secret=secret)
        return True

    monkeypatch.setattr(wp, "verify_webhook", _fake_verify)
    provider = wp.InkboxProvider()
    ok = provider.verify(body=b"raw", headers={}, url="u", secret="whsec_test")
    assert ok is True
    assert seen == {"payload": b"raw", "secret": "whsec_test"}


# --- adapter integration -------------------------------------------------

def test_inkbox_event_without_signature_is_rejected():
    # Claims an Inkbox event type but carries no Inkbox signature header.
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    resp = asyncio.run(
        adapter._handle_webhook(_FakeRequest(b'{"event_type":"message.delivered"}'))
    )
    assert resp.status == 401


def test_inkbox_event_with_valid_signature_passes(monkeypatch):
    monkeypatch.setattr(wp, "verify_webhook", lambda **k: True)
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(
                b'{"event_type":"message.delivered"}',
                headers={"X-Inkbox-Signature": "sha256=good"},
            )
        )
    )
    # message.* lifecycle is a log-only 200 "ok" — proves it passed auth.
    assert resp.status == 200 and resp.text == "ok"


def test_inkbox_event_with_bad_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(wp, "verify_webhook", lambda **k: False)
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(
                b'{"event_type":"message.delivered"}',
                headers={"X-Inkbox-Signature": "sha256=bad"},
            )
        )
    )
    assert resp.status == 401


def test_unknown_source_passthrough_is_unverified_when_enabled():
    # No registered verifier + pass-through on → wake the agent even with
    # require_signature True (we cannot verify an unknown source).
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    resp = asyncio.run(
        adapter._handle_webhook(_FakeRequest(b'{"event":"prod_on_fire"}'))
    )
    assert resp.status == 200
    assert len(adapter._enqueued) == 1


def test_unknown_source_dropped_when_passthrough_disabled():
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(_FakeRequest(b'{"event":"prod_on_fire"}'))
    )
    assert resp.status == 200 and resp.text == "ignored"
    assert adapter._enqueued == []


def test_registered_third_party_is_verified(monkeypatch):
    # Simulate a future onboarded third-party verifier that rejects the request.
    fake = types.SimpleNamespace(name="acme", verify=lambda **k: False)
    monkeypatch.setattr(adapter_mod, "match_provider", lambda headers: fake)
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_ACME", "s3cret")
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(b'{"event":"charge"}', headers={"X-Acme-Signature": "bad"})
        )
    )
    assert resp.status == 401
