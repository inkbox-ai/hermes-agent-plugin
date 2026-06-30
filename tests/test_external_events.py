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
from inkbox_plugin.adapter import InkboxAdapter, MessageEvent


class _FakeRequest:
    def __init__(self, body, *, request_id="req-ext-1"):
        self._body = body
        self.headers = {"X-Inkbox-Request-Id": request_id}

    async def read(self):
        return self._body


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )


def _adapter():
    # Build an adapter without running __init__ (no network/SDK), then wire the
    # minimum state the webhook + external handler touch.
    adapter = object.__new__(InkboxAdapter)
    adapter._require_signature = False
    adapter._seen_request_ids = {}
    adapter._inflight_request_ids = {}
    adapter.platform = "inkbox"
    # Capture whatever gets enqueued instead of waking a real agent.
    adapter._enqueued = []

    async def _capture(event):
        adapter._enqueued.append(event)

    adapter._enqueue = _capture
    # External events have no per-channel overrides in these tests.
    adapter._resolve_channel_overrides = lambda *a, **k: (None, None)
    return adapter


def test_external_event_wakes_agent_on_new_thread():
    adapter = _adapter()
    body = (
        b'{"event_type":"external.event","data":{'
        b'"source":"github","environment":"prod",'
        b'"title":"giblins lit prod server aflame",'
        b'"body":"Workflow deploy.yml failed on main","id":"evt-1"}}'
    )

    resp = asyncio.run(adapter._handle_webhook(_FakeRequest(body)))

    assert resp.status == 200
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert isinstance(event, MessageEvent)
    # Synthetic event must bypass user authorization.
    assert event.internal is True
    # Fresh thread keyed by source + event id.
    assert event.source.chat_id == "external:github"
    assert event.source.thread_id == "external:github:evt-1"
    # Marker carries source + environment so the agent can branch on it.
    assert "[inkbox:external source=github environment=prod" in event.text
    assert "giblins lit prod server aflame" in event.text


def test_external_event_distinct_ids_get_distinct_threads():
    adapter = _adapter()

    def _post(event_id, rid):
        body = (
            b'{"event_type":"external.event","data":{"source":"github",'
            b'"title":"x","id":"' + event_id.encode() + b'"}}'
        )
        return asyncio.run(adapter._handle_webhook(_FakeRequest(body, request_id=rid)))

    _post("evt-1", "r1")
    _post("evt-2", "r2")

    threads = {e.source.thread_id for e in adapter._enqueued}
    assert threads == {"external:github:evt-1", "external:github:evt-2"}


def test_external_event_falls_back_to_request_id_for_thread():
    adapter = _adapter()
    # No "id" in the payload — the webhook request id becomes the thread key.
    body = b'{"event_type":"external.event","data":{"source":"github","title":"x"}}'

    asyncio.run(adapter._handle_webhook(_FakeRequest(body, request_id="req-xyz")))

    assert adapter._enqueued[0].source.thread_id == "external:github:req-xyz"
