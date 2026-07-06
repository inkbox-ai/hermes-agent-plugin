"""Tests for the R8 call-lifecycle webhook consumer (call.ended).

Covers the three seams the plugin owns: the identity-owned call subscription
(a separate row from the iMessage sub, never churning it), the inbound
``call.ended`` dispatch that wakes the agent for post-call follow-up, and the
inline-transcript rendering.
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import (
    InkboxAdapter,
    MessageEvent,
    _DESIRED_CALL_EVENTS,
    _DESIRED_IMESSAGE_EVENTS,
    _call_webhook_url,
    _reconcile_call_subscription,
    _reconcile_imessage_subscription,
    _render_call_transcript,
)


# An Inkbox-signed request carries this header, so the inkbox provider matches
# and routing treats it as an Inkbox event. Value is irrelevant with signature
# verification off (these adapters set ``_require_signature=False``).
_INKBOX_SIGNED = {"X-Inkbox-Signature": "sha256=unchecked"}


class _FakeRequest:
    def __init__(self, body, headers=None, *, request_id="req-call-1"):
        self._body = body
        self.headers = {
            "X-Inkbox-Request-Id": request_id,
            **_INKBOX_SIGNED,
            **(headers or {}),
        }
        self.url = "https://agent.example/webhook?channel=call"

    async def read(self):
        return self._body


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(
            Response=lambda **kwargs: types.SimpleNamespace(**kwargs),
            json_response=lambda payload, **kwargs: types.SimpleNamespace(
                status=200, body=payload
            ),
        ),
    )


def _adapter():
    """Build an adapter without __init__, wiring only what the call path touches."""
    adapter = object.__new__(InkboxAdapter)
    adapter._require_signature = False
    adapter._external_events_enabled = False
    adapter._seen_request_ids = {}
    adapter._inflight_request_ids = {}
    adapter.platform = "inkbox"
    adapter._enqueued = []

    async def _capture(event):
        adapter._enqueued.append(event)

    async def _resolve_contact_full(**_kwargs):
        return None  # unknown caller by default; individual tests override

    adapter._enqueue = _capture
    adapter._resolve_contact_full = _resolve_contact_full
    adapter._resolve_channel_overrides = lambda *a, **k: (None, None)
    return adapter


# A hosted-realtime call.ended payload with an abridged inline transcript.
def _call_ended_body(*, call_id="call-1", use_inkbox_agent=True, transcript=True):
    data = {
        "call": {
            "id": call_id,
            "origin": "dedicated_number",
            "local_phone_number": "+15550000001",
            "remote_phone_number": "+15555550101",
            "direction": "inbound",
            "status": "completed",
            "hangup_reason": "remote",
            "started_at": "2026-07-06T18:20:00Z",
            "ended_at": "2026-07-06T18:22:03Z",
            "created_at": "2026-07-06T18:19:59Z",
            "updated_at": "2026-07-06T18:22:03Z",
            "use_inkbox_agent": use_inkbox_agent,
            "duration_seconds": 123,
        },
        "contacts": [],
        "agent_identities": [],
        "transcript_url": (
            "https://agent.example/api/v1/phone/calls/" + call_id + "/transcripts"
        ),
    }
    if transcript:
        data["transcript"] = {
            "entries": [
                {"party": "remote", "text": "Hi, when does my order ship?", "ts_ms": 0},
                {"marker": "abridged", "omitted_turns": 12, "omitted_ms": 40100},
                {"party": "local", "text": "It ships Thursday.", "ts_ms": 61200},
            ],
            "abridged": True,
            "url": data["transcript_url"],
        }
    return json.dumps({
        "id": "evt_call_1",
        "event_type": "call.ended",
        "timestamp": "2026-07-06T18:22:41Z",
        "data": data,
    }).encode()


# ── _render_call_transcript ─────────────────────────────────────────────────


def test_render_transcript_labels_parties_and_marks_omission():
    text, abridged = _render_call_transcript({
        "entries": [
            {"party": "remote", "text": "Hello?", "ts_ms": 0},
            {"marker": "abridged", "omitted_turns": 5, "omitted_ms": 12000},
            {"party": "local", "text": "Goodbye.", "ts_ms": 30000},
        ],
        "abridged": True,
        "url": "https://x/transcripts",
    })
    assert abridged is True
    assert "Caller: Hello?" in text  # remote → Caller
    assert "Agent: Goodbye." in text  # local → Agent
    assert "5 turns" in text and "12.0s" in text  # omission notice


def test_render_transcript_absent_is_empty():
    assert _render_call_transcript(None) == ("", False)
    assert _render_call_transcript({"entries": [], "abridged": False}) == ("", False)


# ── _call_webhook_url ───────────────────────────────────────────────────────


def test_call_webhook_url_appends_distinct_suffix():
    assert _call_webhook_url("https://a/webhook") == "https://a/webhook?channel=call"
    assert _call_webhook_url(None) is None


# ── dispatch: call.ended wakes the agent ────────────────────────────────────


def test_call_ended_wakes_agent_with_summary_and_transcript():
    adapter = _adapter()

    resp = asyncio.run(adapter._handle_webhook(_FakeRequest(_call_ended_body())))

    assert resp.status == 200 and resp.text == "ok"
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert isinstance(event, MessageEvent)
    assert event.internal is True  # synthetic wake, no reply delivered
    # Fresh per-call thread.
    assert event.source.thread_id == "call:call-1"
    # Marker + headline facts + inline transcript + authoritative url.
    assert "[inkbox:call event=call.ended direction=inbound status=completed" in event.text
    assert "duration=123s" in event.text
    assert "Caller: Hi, when does my order ship?" in event.text
    assert "Agent: It ships Thursday." in event.text
    assert "(abridged)" in event.text
    assert "/api/v1/phone/calls/call-1/transcripts" in event.text
    # The post-call directive is attached as the channel prompt.
    assert "post-call hand-off" in (event.channel_prompt or "")


def test_call_ended_without_hosting_omits_inline_transcript_but_keeps_url():
    adapter = _adapter()

    body = _call_ended_body(use_inkbox_agent=False, transcript=False)
    asyncio.run(adapter._handle_webhook(_FakeRequest(body)))

    event = adapter._enqueued[0]
    # No inline transcript block, but the fetchable url is always present.
    assert "Transcript:" not in event.text
    assert "Full transcript: https://agent.example/api/v1/phone/calls/call-1/transcripts" in event.text


def test_call_ended_dedups_on_call_id():
    adapter = _adapter()

    first = _FakeRequest(_call_ended_body(), request_id="r1")
    # A redelivery: same call, different request id, stable event.
    second = _FakeRequest(_call_ended_body(), request_id="r2")
    asyncio.run(adapter._handle_webhook(first))
    resp2 = asyncio.run(adapter._handle_webhook(second))

    assert resp2.text == "duplicate"
    assert len(adapter._enqueued) == 1  # woken exactly once for the call


def test_call_ended_does_not_fall_through_to_incoming_call():
    adapter = _adapter()
    # If it fell through to _on_incoming_call it would try to answer; stub it to
    # a sentinel so a regression is loud rather than silent.
    async def _boom(_envelope):
        raise AssertionError("call.ended must not hit the incoming-call handler")

    adapter._on_incoming_call = _boom
    resp = asyncio.run(adapter._handle_webhook(_FakeRequest(_call_ended_body())))
    assert resp.status == 200


def test_other_call_event_is_acknowledged_not_woken():
    adapter = _adapter()
    body = json.dumps({
        "event_type": "call.answered",
        "data": {"call": {"id": "call-9"}},
    }).encode()

    resp = asyncio.run(adapter._handle_webhook(_FakeRequest(body, request_id="r-ans")))

    assert resp.status == 200 and resp.text == "ok"
    assert adapter._enqueued == []  # no turn for non-terminal call lifecycle


# ── _is_known_inkbox_event ──────────────────────────────────────────────────


def test_is_known_event_matches_call_and_flat_incoming_call():
    known = InkboxAdapter._is_known_inkbox_event
    assert known("call.ended", {}) is True
    # The flat synchronous incoming-call callback (no event_type) still matches.
    assert known(None, {"phone_number_id": "p1", "remote_phone_number": "+1555"}) is True


# ── subscription reconcile: separate row, no churn ──────────────────────────


class _FakeSubs:
    """Minimal fake of ``client.webhooks.subscriptions`` with (owner,url) rows."""

    def __init__(self):
        self.rows = []  # list of SimpleNamespace(id, agent_identity_id, url, event_types)
        self._next = 0

    def list(self, **kwargs):
        owner = kwargs.get("agent_identity_id")
        return [r for r in self.rows if r.agent_identity_id == owner]

    def create(self, **kwargs):
        self._next += 1
        row = types.SimpleNamespace(
            id=f"sub-{self._next}",
            agent_identity_id=kwargs["agent_identity_id"],
            url=kwargs["url"],
            event_types=list(kwargs["event_types"]),
        )
        self.rows.append(row)
        return row

    def update(self, sub_id, **kwargs):
        row = next(r for r in self.rows if r.id == sub_id)
        if "event_types" in kwargs:
            row.event_types = list(kwargs["event_types"])
        return row

    def delete(self, sub_id):
        self.rows = [r for r in self.rows if r.id != sub_id]


class _FakeClient:
    def __init__(self):
        self.webhooks = types.SimpleNamespace(subscriptions=_FakeSubs())


def test_call_subscription_is_a_separate_row_from_imessage():
    client = _FakeClient()
    base = "https://a/webhook"
    identity = "id-1"

    # iMessage first, then call — both identity-owned, distinct URLs.
    _reconcile_imessage_subscription(client, identity, desired_url=base, previous_webhook_url=None)
    _reconcile_call_subscription(
        client, identity, desired_url=_call_webhook_url(base), previous_webhook_url=None,
    )

    rows = client.webhooks.subscriptions.rows
    assert len(rows) == 2  # two independent rows on the one identity
    by_url = {r.url: set(r.event_types) for r in rows}
    assert by_url[base] == set(_DESIRED_IMESSAGE_EVENTS)  # iMessage untouched
    assert by_url[_call_webhook_url(base)] == set(_DESIRED_CALL_EVENTS)


def test_reconcile_is_idempotent_and_does_not_churn_either_channel():
    client = _FakeClient()
    base = "https://a/webhook"
    identity = "id-1"

    _reconcile_imessage_subscription(client, identity, desired_url=base, previous_webhook_url=None)
    _reconcile_call_subscription(
        client, identity, desired_url=_call_webhook_url(base), previous_webhook_url=None,
    )
    ids_before = {r.url: r.id for r in client.webhooks.subscriptions.rows}

    # Re-run both (a restart / drift reconcile): adopt verbatim, no new rows.
    _reconcile_imessage_subscription(client, identity, desired_url=base, previous_webhook_url=base)
    _reconcile_call_subscription(
        client, identity,
        desired_url=_call_webhook_url(base),
        previous_webhook_url=_call_webhook_url(base),
    )

    rows = client.webhooks.subscriptions.rows
    assert len(rows) == 2  # no duplication, neither channel stripped
    ids_after = {r.url: r.id for r in rows}
    assert ids_after == ids_before  # same rows adopted, not recreated


def test_redeploy_to_new_host_migrates_both_channels_without_cross_churn():
    client = _FakeClient()
    old, new = "https://old/webhook", "https://new/webhook"
    identity = "id-1"

    # Original registration on the old host.
    _reconcile_imessage_subscription(client, identity, desired_url=old, previous_webhook_url=None)
    _reconcile_call_subscription(
        client, identity, desired_url=_call_webhook_url(old), previous_webhook_url=None,
    )
    # Redeploy: new host, prior URL cleaned up per channel.
    _reconcile_imessage_subscription(client, identity, desired_url=new, previous_webhook_url=old)
    _reconcile_call_subscription(
        client, identity,
        desired_url=_call_webhook_url(new),
        previous_webhook_url=_call_webhook_url(old),
    )

    by_url = {r.url: set(r.event_types) for r in client.webhooks.subscriptions.rows}
    # Exactly the two new-host rows survive; the old rows are gone, and neither
    # channel clobbered the other's row during the migration.
    assert set(by_url) == {new, _call_webhook_url(new)}
    assert by_url[new] == set(_DESIRED_IMESSAGE_EVENTS)
    assert by_url[_call_webhook_url(new)] == set(_DESIRED_CALL_EVENTS)
