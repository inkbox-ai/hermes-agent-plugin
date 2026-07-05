"""Send-tool spin-off spawn path — the A/B that proves the feature is additive.

Every case drives a real send tool (``inkbox_send_email`` / ``inkbox_place_call``)
with a fake identity, against a tmp-dir ledger. The through-line:

* absent ``spinoff`` → today's behavior, byte-for-byte: the send happens and NO
  edge is ever written (baseline preserved);
* present ``spinoff`` → exactly one durable edge whose ``brief.facts`` /
  ``recipientBinding`` / ``parentRoute`` are captured from the send args and the
  parent-turn contextvar;
* two asks to the same recipient in one turn get distinct ``callIndex`` values →
  two edges, while a retried identical send (same ``callIndex``) dedups to one;
* a spin-off ``place_call`` stamps the edge id into the voice seed capsule.

All assertions are deterministic and run fully offline (no host, no real model):
the ledger's ``_hermes_home`` seam and the call-capsule's ``get_hermes_home`` are
both redirected at ``tmp_path``, and the per-turn route is set directly on the
plugin-owned contextvar.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

import inkbox_lineage as lineage
import tools
import turn_context


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------
class FakeEmailIdentity:
    """Minimal identity whose ``send_email`` records the call and returns a msg."""

    def __init__(self, message_id="msg-out-1"):
        self.message_id = message_id
        self.sent = []

    def send_email(self, **kwargs):
        # Record the outbound so a test can assert the baseline send still fired.
        self.sent.append(kwargs)
        return types.SimpleNamespace(id=self.message_id)


class FakeCallIdentity:
    """Identity whose ``place_call`` returns a call object with an id/status."""

    def __init__(self, call_id="call-xyz"):
        self.call_id = call_id
        self.calls = []

    def place_call(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(id=self.call_id, status="ringing", rate_limit=None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the durable ledger at an isolated tmp hermes-home."""
    # The whole edge ledger keys off this seam (edges/, locks/, by_* indexes),
    # so pointing it at tmp_path isolates every test from disk and each other.
    monkeypatch.setattr(lineage, "_hermes_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_turn():
    """Reset the parent-turn contextvar after each test so routes don't leak."""
    yield
    turn_context._CURRENT_TURN.set(None)


def _route(**overrides):
    """A representative parent-turn route the adapter would stamp in ``_enqueue``."""
    route = {
        "sessionThreadId": "email:thread-A",
        "chatId": "chat-A",
        "threadId": "email:thread-A",
        "modality": "email",
        "contactId": "contact-A",
        "messageId": "turn-A-1",
        "replyTo": "person-a@example.com",
    }
    route.update(overrides)
    return route


def _use_email_identity(monkeypatch, identity):
    """Point ``_client_and_identity`` at a fake so no real SDK client is built."""
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))


def _edge_files(home):
    """Return the persisted edge JSON paths under the tmp ledger (may be empty)."""
    edges_dir = home / "inkbox_lineage" / "edges"
    if not edges_dir.exists():
        return []
    return sorted(edges_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# BASELINE — absent spinoff must be byte-for-byte today's behavior
# ---------------------------------------------------------------------------
def test_email_without_spinoff_creates_no_edge(home, monkeypatch):
    identity = FakeEmailIdentity()
    _use_email_identity(monkeypatch, identity)
    # A parent turn is in flight, but with no `spinoff` arg none of it should
    # matter — the send stays fire-and-forget.
    turn_context.set_current_turn(_route())

    result = json.loads(
        tools.inkbox_send_email({"to": ["alex@example.com"], "subject": "Hi", "body_text": "hello"})
    )

    # The ordinary send still happened and reports success.
    assert result["ok"] is True
    assert result["message_id"] == "msg-out-1"
    assert len(identity.sent) == 1
    # No spin-off surface leaks into the result, and NO edge was written.
    assert "spinoff_edge_id" not in result
    assert "spinoff_warning" not in result
    assert _edge_files(home) == []


# ---------------------------------------------------------------------------
# PRESENT spinoff — exactly one edge with the right facts/binding/route
# ---------------------------------------------------------------------------
def test_email_with_spinoff_creates_one_edge_with_captured_context(home, monkeypatch):
    identity = FakeEmailIdentity(message_id="msg-out-42")
    _use_email_identity(monkeypatch, identity)
    turn_context.set_current_turn(_route())

    result = json.loads(
        tools.inkbox_send_email(
            {
                "to": ["vendor@example.com"],
                "subject": "Quote?",
                "body_text": "Can you quote this?",
                "spinoff": {
                    "purpose": "get a quote",
                    "success": "have a price and lead time",
                    # Mix an explicit "label: value" with a bare fact so both
                    # label-derivation branches are exercised.
                    "disclose": ["Budget: $500", "prefers mornings"],
                },
            }
        )
    )

    # The send succeeded and bookkeeping did not fall back to a warning.
    assert result["ok"] is True, result
    assert "spinoff_warning" not in result, result
    edge_id = result["spinoff_edge_id"]

    # Exactly one edge on disk, and it is the one the tool reported.
    files = _edge_files(home)
    assert len(files) == 1
    edge = lineage._read_edge(edge_id)
    assert edge is not None and edge["edgeId"] == edge_id

    # Post-send edges land in `delivered`; the recipient binding carries the id
    # of the message we just sent (the inbound-side match key).
    assert edge["status"] == lineage.STATUS_DELIVERED
    assert edge["recipientBinding"]["outboundMessageId"] == "msg-out-42"
    assert edge["recipientBinding"]["channel"] == "email"
    assert edge["recipientBinding"]["address"] == "vendor@example.com"

    # brief carries the delegated intent + the disclose[] allowlist transformed
    # into owner-tagged facts (owner = the parent principal from the route).
    brief = edge["brief"]
    assert brief["intent"] == "get a quote"
    assert brief["endState"] == "have a price and lead time"
    assert brief["facts"] == [
        {"label": "Budget", "value": "Budget: $500", "owner": "contact-A"},
        {"label": "prefers mornings", "value": "prefers mornings", "owner": "contact-A"},
    ]

    # parentRoute + lineage keys come straight from the stamped contextvar, so
    # the relay can later rebuild A's source without a host stamp.
    assert edge["parentSessionKey"] == "email:thread-A"
    assert edge["originTurnId"] == "turn-A-1"
    assert edge["parentRoute"] == {
        "chatId": "chat-A",
        "threadId": "email:thread-A",
        "modality": "email",
    }


# ---------------------------------------------------------------------------
# Within-turn callIndex disambiguation vs redelivery idempotency
# ---------------------------------------------------------------------------
def test_two_asks_same_recipient_same_turn_are_distinct_edges(home, monkeypatch):
    identity = FakeEmailIdentity()
    _use_email_identity(monkeypatch, identity)
    turn_context.set_current_turn(_route())

    args = {
        "to": ["vendor@example.com"],
        "subject": "Quote?",
        "body_text": "first ask",
        "spinoff": {"purpose": "get a quote"},
    }
    first = json.loads(tools.inkbox_send_email(dict(args)))
    # Second, genuinely separate ask to the same recipient in the same turn:
    # _next_call_index counts the first edge → callIndex 1 → new spawnKey.
    second = json.loads(tools.inkbox_send_email(dict(args, body_text="second ask")))

    assert first["spinoff_edge_id"] != second["spinoff_edge_id"]
    assert len(_edge_files(home)) == 2

    e1 = lineage._read_edge(first["spinoff_edge_id"])
    e2 = lineage._read_edge(second["spinoff_edge_id"])
    # Distinct within-turn indices → distinct idempotency keys.
    assert {e1["callIndex"], e2["callIndex"]} == {0, 1}
    assert e1["spawnKey"] != e2["spawnKey"]


def test_redelivered_identical_send_dedups_to_one_edge(home, monkeypatch):
    identity = FakeEmailIdentity()
    _use_email_identity(monkeypatch, identity)
    turn_context.set_current_turn(_route())
    # A redelivery is the same parent/recipient/turn AT THE SAME within-turn
    # index — pin the index so both sends compute the identical spawnKey and
    # create_edge_cas collapses them to a single edge.
    monkeypatch.setattr(tools, "_next_call_index", lambda *a, **k: 0)

    args = {
        "to": ["vendor@example.com"],
        "subject": "Quote?",
        "body_text": "same ask",
        "spinoff": {"purpose": "get a quote"},
    }
    first = json.loads(tools.inkbox_send_email(dict(args)))
    second = json.loads(tools.inkbox_send_email(dict(args)))

    # Same spawnKey → the second call reuses the first edge, one file on disk.
    assert first["spinoff_edge_id"] == second["spinoff_edge_id"]
    assert len(_edge_files(home)) == 1


# ---------------------------------------------------------------------------
# place_call — the spin-off edge id must ride the voice seed capsule
# ---------------------------------------------------------------------------
def _install_hermes_home(monkeypatch, tmp_path):
    """Stub ``hermes_cli.config.get_hermes_home`` so the capsule lands in tmp."""
    # The call-context capsule imports get_hermes_home lazily at call time; the
    # host package isn't installed offline, so inject a tiny fake module pair.
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []  # mark as a package so the submodule import resolves
    cfg = types.ModuleType("hermes_cli.config")
    cfg.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cfg)


def _capsule_payloads(tmp_path):
    """Load every outbound-call context capsule written under the tmp home."""
    root = tmp_path / "inkbox_call_contexts"
    if not root.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))]


def test_place_call_without_spinoff_capsule_has_no_edge_id(home, monkeypatch, tmp_path):
    identity = FakeCallIdentity()
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))
    _install_hermes_home(monkeypatch, tmp_path)

    result = json.loads(
        tools.inkbox_place_call(
            {
                "to_number": "+15551230000",
                "purpose": "confirm the appointment",
                "origination": "dedicated_number",
                "client_websocket_url": "wss://agent.example.com/ws",
            }
        )
    )

    assert result["ok"] is True
    assert "spinoff_edge_id" not in result
    # No spin-off → the capsule is byte-for-byte the old shape (no edge_id key).
    payloads = _capsule_payloads(tmp_path)
    assert len(payloads) == 1
    assert "edge_id" not in payloads[0]
    assert _edge_files(home) == []


def test_place_call_spinoff_writes_edge_id_into_capsule(home, monkeypatch, tmp_path):
    identity = FakeCallIdentity(call_id="call-777")
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))
    _install_hermes_home(monkeypatch, tmp_path)
    turn_context.set_current_turn(_route(modality="voice"))

    result = json.loads(
        tools.inkbox_place_call(
            {
                "to_number": "+15551230000",
                "purpose": "ask about availability",
                "origination": "dedicated_number",
                "client_websocket_url": "wss://agent.example.com/ws",
                "spinoff": {"purpose": "ask about availability"},
            }
        )
    )

    assert result["ok"] is True, result
    edge_id = result["spinoff_edge_id"]

    # The seed capsule carries the edge id so the live call can bind back to it.
    payloads = _capsule_payloads(tmp_path)
    assert len(payloads) == 1
    assert payloads[0]["edge_id"] == edge_id

    # The edge was promoted spawning→delivered once the call was placed, and it
    # bound to the returned call id.
    edge = lineage._read_edge(edge_id)
    assert edge["status"] == lineage.STATUS_DELIVERED
    assert edge["channelChild"] == "voice"
    assert edge["recipientBinding"]["outboundMessageId"] == "call-777"
