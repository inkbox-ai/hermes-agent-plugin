"""Read-only spin-off query tools — the traceability surface.

Exercises the three query tools at the tool boundary against a tmp-dir ledger,
proving the observability channel stays metadata-only and correctly scoped:

* ``inkbox_spinoff_list`` — the parent side lists the spin-offs it started,
  terminal edges filtered out by default (surfaced only with includeTerminal),
  the recipient address masked so a logged prompt never leaks B's full contact;
* ``inkbox_lineage_status`` — one edge's detail by id (including its distilled
  answer), or the parent's open spin-offs when no id / "open" is passed;
* ``inkbox_spinoff_origin`` — the child side lists every OPEN edge it still owes
  an answer on (terminal edges filtered), with the intent/success it must meet.

All assertions run fully offline: the ledger's ``_hermes_home`` seam is redirected
at ``tmp_path``, the parent route is set on the plugin-owned contextvar, and the
child session id is injected via the ``_current_session_thread_id`` seam — so no
host, session env, or real model is ever consulted.
"""

from __future__ import annotations

import json

import pytest

import inkbox_lineage as lineage
import tools
import turn_context


# ---------------------------------------------------------------------------
# Fixtures + seeding helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the durable ledger at an isolated tmp hermes-home."""
    monkeypatch.setattr(lineage, "_hermes_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_turn():
    """Reset the parent-turn contextvar after each test so routes don't leak."""
    yield
    turn_context._CURRENT_TURN.set(None)


def _seed_edge(
    parent_session_key,
    *,
    status,
    channel="email",
    address="alex@example.com",
    conversation_id=None,
    child=None,
    intent="ask about the quote",
    end_state="have a price and lead time",
    disclose_identity=False,
    result=None,
):
    """Persist one edge the way a spawned send + bind would leave it on disk.

    Args:
        parent_session_key (str): the originating (parent) session key.
        status (str): the edge status to stamp.
        channel (str): the child channel (``email``/``sms``/``imessage``).
        address (Optional[str]): the child address (masked in list readouts).
        conversation_id (Optional[str]): the child conversation id, if any.
        child (Optional[str]): the bound child session key (feeds the by_child
            index that ``inkbox_spinoff_origin`` reads).
        intent (str): the delegated intent shown to both sides.
        end_state (str): the success condition the child owes.
        disclose_identity (bool): whether the child may name the originator.
        result (Optional[dict]): a distilled result payload, for answered edges.

    Returns:
        Dict[str, Any]: the persisted edge dict.
    """
    recipient = {"channel": channel, "address": address, "conversationId": conversation_id}
    edge = lineage.derive_edge(None, parent_session_key, recipient, 0)
    edge["status"] = status
    edge["childSessionKey"] = child
    edge["brief"]["intent"] = intent
    edge["brief"]["endState"] = end_state
    edge["brief"]["disclose_identity"] = disclose_identity
    edge["result"] = result
    lineage._persist(edge)  # writes the edge file + (re)builds its side-indexes
    return edge


# ---------------------------------------------------------------------------
# inkbox_spinoff_list — parent side, terminal filtering + masking + scoping
# ---------------------------------------------------------------------------
def test_spinoff_list_filters_terminal_edges_by_default(home):
    psk = "email:thread-A"
    turn_context.set_current_turn({"sessionThreadId": psk})
    # One in-flight edge and one closed edge under the same parent.
    open_edge = _seed_edge(psk, status=lineage.STATUS_AWAITING_REPLY)
    _seed_edge(psk, status=lineage.STATUS_RELAYED, address="bob@example.com")

    result = json.loads(tools.inkbox_spinoff_list({}))

    assert result["ok"] is True
    # Only the still-open edge is surfaced; the terminal one is hidden.
    assert result["count"] == 1
    assert [row["edge_id"] for row in result["spinoffs"]] == [open_edge["edgeId"]]


def test_spinoff_list_include_terminal_shows_closed_edges(home):
    psk = "email:thread-A"
    turn_context.set_current_turn({"sessionThreadId": psk})
    open_edge = _seed_edge(psk, status=lineage.STATUS_AWAITING_REPLY)
    closed_edge = _seed_edge(psk, status=lineage.STATUS_RELAYED, address="bob@example.com")

    result = json.loads(tools.inkbox_spinoff_list({"includeTerminal": True}))

    assert result["ok"] is True
    assert result["count"] == 2
    ids = {row["edge_id"] for row in result["spinoffs"]}
    assert ids == {open_edge["edgeId"], closed_edge["edgeId"]}


def test_spinoff_list_masks_recipient_email_never_full(home):
    psk = "email:thread-A"
    turn_context.set_current_turn({"sessionThreadId": psk})
    _seed_edge(psk, status=lineage.STATUS_AWAITING_REPLY, address="alex@example.com")

    raw = tools.inkbox_spinoff_list({})
    result = json.loads(raw)

    row = result["spinoffs"][0]
    # Recipient is masked to first-initial + domain — never the full local part.
    assert row["recipient"] == "a…@example.com"
    # And the full address appears nowhere in the serialized readout.
    assert "alex@example.com" not in raw


def test_spinoff_list_masks_recipient_phone_never_full(home):
    psk = "sms:thread-A"
    turn_context.set_current_turn({"sessionThreadId": psk})
    _seed_edge(
        psk,
        status=lineage.STATUS_AWAITING_REPLY,
        channel="sms",
        address="+15551234231",
    )

    raw = tools.inkbox_spinoff_list({})
    result = json.loads(raw)

    row = result["spinoffs"][0]
    # A number is masked to a short prefix + last four; the middle is hidden.
    assert row["recipient"] == "+1…4231"
    assert "+15551234231" not in raw


def test_spinoff_list_scoped_to_current_parent(home):
    mine = "email:thread-A"
    turn_context.set_current_turn({"sessionThreadId": mine})
    my_edge = _seed_edge(mine, status=lineage.STATUS_AWAITING_REPLY)
    # An edge owned by an unrelated conversation must not leak into my list.
    _seed_edge("email:thread-Z", status=lineage.STATUS_AWAITING_REPLY, address="zoe@example.com")

    result = json.loads(tools.inkbox_spinoff_list({}))

    assert result["count"] == 1
    assert result["spinoffs"][0]["edge_id"] == my_edge["edgeId"]


# ---------------------------------------------------------------------------
# inkbox_lineage_status — one-edge detail vs. open list
# ---------------------------------------------------------------------------
def test_lineage_status_by_edge_id_returns_answer_and_success(home):
    psk = "email:thread-A"
    turn_context.set_current_turn({"sessionThreadId": psk})
    edge = _seed_edge(
        psk,
        status=lineage.STATUS_RELAYED,  # terminal, yet an explicit id still resolves it
        end_state="have a firm price",
        result={
            "summary": "The vendor quoted $480, ships in 3 days.",
            "status": "answered",
            "attribution": "per the vendor",
        },
    )

    result = json.loads(tools.inkbox_lineage_status({"edgeId": edge["edgeId"]}))

    assert result["ok"] is True
    detail = result["spinoff"]
    assert detail["edge_id"] == edge["edgeId"]
    assert detail["status"] == lineage.STATUS_RELAYED
    # Per-edge detail carries the success condition and the distilled answer.
    assert detail["success"] == "have a firm price"
    assert detail["answer"] == "The vendor quoted $480, ships in 3 days."
    # Even in detail view the recipient stays masked.
    assert detail["recipient"] == "a…@example.com"


def test_lineage_status_unknown_edge_errors(home):
    turn_context.set_current_turn({"sessionThreadId": "email:thread-A"})
    result = json.loads(tools.inkbox_lineage_status({"edgeId": "no-such-edge"}))
    assert "error" in result


def test_lineage_status_open_lists_only_open_edges(home):
    psk = "email:thread-A"
    turn_context.set_current_turn({"sessionThreadId": psk})
    open_edge = _seed_edge(psk, status=lineage.STATUS_AWAITING_REPLY)
    _seed_edge(psk, status=lineage.STATUS_ABANDONED, address="bob@example.com")

    # Explicit "open" and the omitted-arg form both mean "list open spin-offs".
    for args in ({"edgeId": "open"}, {}):
        result = json.loads(tools.inkbox_lineage_status(args))
        assert result["ok"] is True
        assert result["count"] == 1
        assert result["spinoffs"][0]["edge_id"] == open_edge["edgeId"]


# ---------------------------------------------------------------------------
# inkbox_spinoff_origin — child side, lists ALL open edges the child owes
# ---------------------------------------------------------------------------
def test_spinoff_origin_lists_all_open_edges_child_owes(home, monkeypatch):
    child = "sms:thread-B"
    # The child conversation authorizes/queries by its own session thread id.
    monkeypatch.setattr(tools, "_current_session_thread_id", lambda: child)

    # Two open edges the same child owes (SMS candidate-set case), one bearing an
    # identity-disclosure grant, plus a terminal edge that must NOT be listed.
    owed_a = _seed_edge(
        "email:thread-A",
        status=lineage.STATUS_AWAITING_REPLY,
        child=child,
        intent="ask about pricing",
        end_state="have a price",
        disclose_identity=True,
    )
    owed_b = _seed_edge(
        "email:thread-C",
        status=lineage.STATUS_DELIVERED,
        child=child,
        intent="ask about lead time",
        end_state="have a lead time",
        disclose_identity=False,
    )
    _seed_edge(
        "email:thread-D",
        status=lineage.STATUS_RELAYED,  # already closed — filtered out
        child=child,
        intent="old ask",
    )

    result = json.loads(tools.inkbox_spinoff_origin({}))

    assert result["ok"] is True
    assert result["count"] == 2
    owes = {row["edge_id"]: row for row in result["owes"]}
    assert set(owes) == {owed_a["edgeId"], owed_b["edgeId"]}
    # Each row frames the delegated task (intent + success) and the identity gate.
    assert owes[owed_a["edgeId"]]["intent"] == "ask about pricing"
    assert owes[owed_a["edgeId"]]["success"] == "have a price"
    assert owes[owed_a["edgeId"]]["may_name_originator"] is True
    assert owes[owed_b["edgeId"]]["may_name_originator"] is False
    # Origin is metadata-only: it never echoes the recipient address.
    assert "recipient" not in owes[owed_a["edgeId"]]


def test_spinoff_origin_without_session_returns_empty(home, monkeypatch):
    # No child session id in context (CLI/unbound) → nothing owed, no error.
    monkeypatch.setattr(tools, "_current_session_thread_id", lambda: "")
    result = json.loads(tools.inkbox_spinoff_origin({}))
    assert result == {"ok": True, "count": 0, "owes": []}


def test_spinoff_origin_scoped_to_this_child(home, monkeypatch):
    mine = "sms:thread-B"
    monkeypatch.setattr(tools, "_current_session_thread_id", lambda: mine)
    owed = _seed_edge("email:thread-A", status=lineage.STATUS_AWAITING_REPLY, child=mine)
    # An open edge owed by a DIFFERENT child must not surface for me.
    _seed_edge("email:thread-A", status=lineage.STATUS_AWAITING_REPLY, child="sms:thread-OTHER")

    result = json.loads(tools.inkbox_spinoff_origin({}))

    assert result["count"] == 1
    assert result["owes"][0]["edge_id"] == owed["edgeId"]
