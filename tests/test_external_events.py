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
    def __init__(self, body, headers=None, *, request_id="req-ext-1"):
        self._body = body
        self.headers = {"X-Inkbox-Request-Id": request_id, **(headers or {})}
        self.url = "https://agent.example/webhook"

    async def read(self):
        return self._body


# An Inkbox-signed request carries this header, so the inkbox provider matches
# and routing treats it as an Inkbox event. Value is irrelevant when signature
# verification is off (these tests build adapters with _require_signature=False).
_INKBOX_SIGNED = {"X-Inkbox-Signature": "sha256=unchecked"}


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
    adapter._external_events_enabled = True  # pass-through on for these tests
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


# The exact payload the yc-product-showcase workflow sends (flat object, the
# key is "event", no "event_type", no "data" wrapper).
DEMO_BODY = (
    b'{"event":"agent_escalation_demo","title":"Agent escalation demo",'
    b'"severity":"demo","summary":"A demo workflow requested human follow-up.",'
    b'"requested_action":"Call Dima and explain what happened.",'
    b'"github":{"repository":"inkbox-ai/servers","workflow":"YC product showcase",'
    b'"run_id":"16012345678",'
    b'"run_url":"https://github.com/inkbox-ai/servers/actions/runs/16012345678"}}'
)


def test_demo_payload_wakes_agent_on_new_thread():
    adapter = _adapter()

    resp = asyncio.run(adapter._handle_webhook(_FakeRequest(DEMO_BODY)))

    assert resp.status == 200
    assert resp.text == "ok"
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert isinstance(event, MessageEvent)
    # Synthetic event must bypass user authorization.
    assert event.internal is True
    # Source = the GitHub repo; thread keyed by the run id.
    assert event.source.chat_id == "external:inkbox-ai/servers"
    assert event.source.thread_id == "external:inkbox-ai/servers:16012345678"
    # Marker + the human fields + the requested action are all surfaced.
    assert "[inkbox:external source=inkbox-ai/servers event=agent_escalation_demo" in event.text
    assert "Requested action: Call Dima and explain what happened." in event.text
    assert "16012345678" in event.text  # raw payload included


def test_external_event_skips_signature_even_when_required():
    # External events are signed by the source, not us — so even with signature
    # verification ON, the external path must NOT run verify_webhook (it would
    # 401 every third-party webhook). It passes straight through when enabled.
    adapter = _adapter()
    adapter._require_signature = True
    adapter._signing_key = "whsec_unused"  # never consulted for external events

    resp = asyncio.run(adapter._handle_webhook(_FakeRequest(DEMO_BODY)))

    assert resp.status == 200
    assert resp.text == "ok"
    assert len(adapter._enqueued) == 1


def test_external_events_disabled_by_default_drops_event():
    adapter = _adapter()
    adapter._external_events_enabled = False  # the default

    resp = asyncio.run(adapter._handle_webhook(_FakeRequest(DEMO_BODY)))

    assert resp.status == 200
    assert resp.text == "ignored"
    assert adapter._enqueued == []  # agent NOT woken when pass-through is off


def test_unknown_event_type_hits_external_path():
    adapter = _adapter()
    # An event type we don't recognize and never will — must still wake.
    body = b'{"event_type":"something.brand_new","title":"hi"}'

    asyncio.run(adapter._handle_webhook(_FakeRequest(body, request_id="r-unknown")))

    assert len(adapter._enqueued) == 1
    # No id/run_id in the payload, so the thread falls back to the request id.
    assert adapter._enqueued[0].source.thread_id == "external:external:r-unknown"


def test_known_lifecycle_events_are_skipped_not_externalized(monkeypatch):
    """message./text./imessage. lifecycle events must NOT hit the external path."""
    adapter = _adapter()

    called = {"lifecycle": 0}

    async def _fake_text_lifecycle(_envelope):
        called["lifecycle"] += 1
        return types.SimpleNamespace(status=200, text="ok")

    monkeypatch.setattr(adapter, "_on_text_lifecycle", _fake_text_lifecycle)

    # A delivery lifecycle event — known, must be handled (logged), not woken.
    # It's Inkbox-signed, so it routes to the Inkbox lifecycle handler.
    body = b'{"event_type":"text.delivered","data":{"text_message":{"id":"t1"}}}'
    resp = asyncio.run(adapter._handle_webhook(_FakeRequest(body, _INKBOX_SIGNED)))

    assert resp.text == "ok"
    assert called["lifecycle"] == 1
    assert adapter._enqueued == []  # agent was NOT woken


def test_distinct_events_get_distinct_threads():
    adapter = _adapter()

    def _post(run_id, rid):
        body = (
            b'{"event":"agent_escalation_demo","github":{"repository":"inkbox-ai/servers",'
            b'"run_id":"' + run_id.encode() + b'"}}'
        )
        return asyncio.run(adapter._handle_webhook(_FakeRequest(body, request_id=rid)))

    _post("100", "r1")
    _post("200", "r2")

    threads = {e.source.thread_id for e in adapter._enqueued}
    assert threads == {
        "external:inkbox-ai/servers:100",
        "external:inkbox-ai/servers:200",
    }


def test_github_native_payload_extracts_source_and_run(caplog):
    # Real GitHub webhooks nest repository.full_name and workflow_run.id/html_url
    # (not our demo `github` block) — the routing fields must still resolve.
    adapter = _adapter()
    body = {
        "action": "completed",
        "conclusion": "failure",
        "repository": {"full_name": "inkbox-ai/servers"},
        "workflow_run": {
            "id": "991",
            "html_url": "https://github.com/inkbox-ai/servers/actions/runs/991",
        },
    }
    caplog.set_level("INFO", logger=adapter_mod.logger.name)
    asyncio.run(adapter._on_external_event(body, "req-gh", verified=True))
    event = adapter._enqueued[0]
    assert event.source.chat_id == "external:inkbox-ai/servers"
    assert event.source.thread_id == "external:inkbox-ai/servers:991"
    assert "runs/991" in event.text
    assert "External event enqueued: external:inkbox-ai/servers:991" in caplog.text


def test_external_source_name_sanitized_in_marker():
    # A crafted source can't break the [inkbox:external ...] marker or the
    # external:<source> chat id (brackets stripped, newline → space).
    adapter = _adapter()
    asyncio.run(adapter._on_external_event({"source": "evil]\ninjected", "title": "x"}, "req-s"))
    event = adapter._enqueued[0]
    marker = event.text.splitlines()[0]
    assert marker.startswith("[inkbox:external ") and marker.endswith("]")
    assert marker.count("]") == 1  # injected ']' stripped; only the closer remains
    assert "source=evil injected" in marker  # newline became a space, no line break
    assert event.source.chat_id == "external:evil injected"
