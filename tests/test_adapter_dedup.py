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


class _FakeRequest:
    def __init__(self, body, headers=None, *, request_id="req-1"):
        self._body = body
        self.headers = {"X-Inkbox-Request-Id": request_id, **(headers or {})}
        self.url = "https://agent.example/webhook"

    async def read(self):
        return self._body


# Marks a request as Inkbox-signed so it routes to the Inkbox handlers (value
# unchecked here: _adapter() builds with _require_signature=False).
_INKBOX_SIGNED = {"X-Inkbox-Signature": "sha256=unchecked"}


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )


def _adapter():
    adapter = object.__new__(InkboxAdapter)
    adapter._require_signature = False
    adapter._external_events_enabled = True  # let unknown events reach the external path
    adapter._seen_request_ids = {}
    adapter._inflight_request_ids = {}
    return adapter


def test_request_id_commits_after_success(monkeypatch):
    adapter = _adapter()
    # Unknown event types now fall through to the external-event path; stub it
    # so this test stays focused on the dedup commit/duplicate behavior.
    async def _ok(_envelope, _request_id="", verified=False):
        return types.SimpleNamespace(text="ok")

    monkeypatch.setattr(adapter, "_on_external_event", _ok)
    body = b'{"event_type":"unknown.event"}'

    first = asyncio.run(adapter._handle_webhook(_FakeRequest(body)))
    second = asyncio.run(adapter._handle_webhook(_FakeRequest(body)))

    assert first.text == "ok"
    assert second.text == "duplicate"


def test_request_id_rolls_back_after_dispatch_failure(monkeypatch):
    adapter = _adapter()
    calls = {"count": 0}

    async def fail_once(_envelope):
        calls["count"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(adapter, "_on_text_received", fail_once)
    body = b'{"event_type":"text.received","data":{"text_message":{"id":"t1"}}}'

    with pytest.raises(RuntimeError):
        asyncio.run(adapter._handle_webhook(_FakeRequest(body, _INKBOX_SIGNED)))
    with pytest.raises(RuntimeError):
        asyncio.run(adapter._handle_webhook(_FakeRequest(body, _INKBOX_SIGNED)))

    assert calls["count"] == 2
