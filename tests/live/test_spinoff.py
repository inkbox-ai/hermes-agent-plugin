"""Live spin-off lineage suite — the marquee A -> agent -> B -> A delegation.

The headline proof that a *briefed + relayed* spin-off beats an amnesiac
fire-and-forget send. Both legs hold the outbound-to-B send CONSTANT and vary
only the spin-off + relay layer, so any observed delta is attributable to the
feature.

  * BASELINE  — A asks the agent to text B, with NO report-back requested. The
    agent texts B (spy proves it), but B's reply carries a token A never asked
    for: assert NO lineage edge is written and A receives NO follow-up carrying
    that token. This is the recorded gap — today's stateless behavior.
  * SPINOFF   — A delegates ("ask B for X and email me the answer"). The agent
    texts B with a spin-off, B replies, the bound child agent relays the
    distilled answer home. Assert the durable edge advances through its
    lifecycle on disk, the spy shows BOTH the A->B send and a later send to A,
    A's inbox gets exactly ONE inbound carrying B's answer token, and a
    redelivered B reply still yields exactly one relay.

Real-model only (the mock model emits plain text with no tool calls and cannot
drive a spin-off). A and B are the same remote principal reached on two
channels (email = A, SMS = B), so this exercises spawn/bind/relay/exactly-once
without a distinct-B secret. Success-path logging stays free of addresses and
message bodies — this repo (and its Action logs) is public.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
REAL = os.environ.get("LIVE_REAL_MODEL") == "1"

# The delegation flow chains several hops (email in -> SMS out -> SMS reply ->
# relay -> email out), so it needs a more generous window than a single leg.
TIMEOUT_S = float(os.environ.get("LIVE_SPINOFF_TIMEOUT", "240"))
# How long we watch for an *unwanted* baseline follow-up before declaring the gap.
BASELINE_WINDOW_S = float(os.environ.get("LIVE_SPINOFF_BASELINE_WINDOW", "90"))
# How long we watch for a *duplicate* relay after replaying B's answer.
REDELIVERY_WINDOW_S = float(os.environ.get("LIVE_SPINOFF_REDELIVERY_WINDOW", "45"))
POLL_EVERY_S = 6.0
ERROR_MARKERS = ("non-retryable error", "missing authentication", "http 401", "http 403", "traceback")

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY and REAL),
    reason="spin-off suite: needs both keys + LIVE_REAL_MODEL=1 (real model drives the tool calls)",
)


# ---------------------------------------------------------------------------
# Small shared helpers (mirrors the idioms in test_cross_channel / test_sms).
# ---------------------------------------------------------------------------
def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _client(key):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _token() -> str:
    return uuid.uuid4().hex[:6]


def _inbound_sms_from(remote, pid: str, from_phone: str):
    """Inbound texts on ``pid`` (B's number) originating from ``from_phone``."""
    tail = _digits(from_phone)[-10:]
    return [m for m in remote.texts.list(pid, limit=30)
            if (getattr(m, "direction", "") or "").lower() == "inbound"
            and _digits(getattr(m, "remote_phone_number", "") or "")[-10:] == tail]


def _inbound_email_from(remote, mailbox_email: str, from_email: str):
    """Inbound email in ``mailbox_email`` (A's inbox) sent by ``from_email``."""
    from inkbox.mail.types import MessageDirection

    return [m for m in remote.messages.list(mailbox_email, direction=MessageDirection.INBOUND)
            if from_email.lower() in (getattr(m, "from_address", "") or "").lower()]


def _email_carries_token(remote, mailbox_email: str, m, token: str) -> bool:
    """True if ``m``'s subject or body contains ``token`` (fetch body if needed)."""
    hay = (getattr(m, "subject", "") or "").lower()
    if token in hay:
        return True
    body = getattr(remote.messages.get(mailbox_email, m.id), "body_text", "") or ""
    return token in body.lower()


# ---------------------------------------------------------------------------
# On-disk ledger — the gateway runs on this same runner under HERMES_HOME, so
# the edge files hold only this test's data and are safe to read directly.
# ---------------------------------------------------------------------------
def _edges_dir() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "inkbox_lineage" / "edges"


def _edge_files() -> set:
    d = _edges_dir()
    return {p.name for p in d.glob("*.json")} if d.exists() else set()


def _read_edge_file(name: str):
    try:
        return json.loads((_edges_dir() / name).read_text())
    except Exception:
        return None  # tolerant: a torn/mid-write file reads as "no edge"


# ---------------------------------------------------------------------------
# Send-intent spy — the gateway process appends one JSON line per outbound send
# (see tests/live/spy_path/sitecustomize.py). We snapshot the line count before
# a trigger and inspect only the lines that appear after it.
# ---------------------------------------------------------------------------
def _spy_lines() -> list:
    f = os.environ.get("INKBOX_SPY_FILE")
    if not f or not os.path.exists(f):
        return []
    out = []
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _spy_haystack(rec: dict) -> str:
    """Flatten a spy record's kwargs to one searchable string."""
    return json.dumps(rec.get("kwargs") or {})


@pytest.fixture(scope="module")
def sx():
    """Seed the AUT contact card so the agent can resolve B, and hand back both
    principals' coordinates. A single remote identity plays A (email) and B
    (SMS); the card must carry both channels or the agent can't cross to B.
    """
    remote = _client(REMOTE_KEY)
    aut = _client(AUT_KEY)
    remote_email = remote.mailboxes.list()[0].email_address
    aut_email = aut.mailboxes.list()[0].email_address
    rnums = remote.phone_numbers.list()
    anums = aut.phone_numbers.list()
    assert rnums and anums, "both identities need a phone number for the spin-off flow"
    remote_phone, remote_pid = rnums[0].number, str(rnums[0].id)
    aut_phone = anums[0].number

    # Ensure the sender's card has BOTH an email and a phone (merge in whatever
    # is missing; never clobber existing data) — same discipline as the
    # cross-channel fixture.
    from inkbox.contacts.types import ContactEmail, ContactPhone
    matches = aut.contacts.lookup(email=remote_email)
    if not matches:
        aut.contacts.create(
            given_name="Penny", family_name="Tester",
            emails=[ContactEmail("work", remote_email)],
            phones=[ContactPhone("mobile", remote_phone)],
        )
    else:
        c = matches[0]
        emails = list(getattr(c, "emails", []))
        phones = list(getattr(c, "phones", []))
        changed = False
        if not any((e.value or "").lower() == remote_email.lower() for e in emails):
            emails.append(ContactEmail("work", remote_email))
            changed = True
        if not any(_digits(p.value)[-10:] == _digits(remote_phone)[-10:] for p in phones):
            phones.append(ContactPhone("mobile", remote_phone))
            changed = True
        if changed:
            aut.contacts.update(c.id, emails=emails, phones=phones)

    return {
        "remote": remote, "aut": aut,
        "remote_email": remote_email, "remote_phone": remote_phone, "remote_pid": remote_pid,
        "aut_email": aut_email, "aut_phone": aut_phone,
    }


def test_spinoff_baseline_no_edge_no_followup(sx):
    """BASELINE: a plain fire-and-forget send writes no edge and never relays.

    A asks the agent to text B with NO report-back. B's reply carries a token A
    never requested; assert (b2) no lineage edge appears and (b3) A gets no
    follow-up carrying B's token — the amnesiac gap the feature closes.
    """
    remote, remote_pid, aut_phone = sx["remote"], sx["remote_pid"], sx["aut_phone"]
    remote_email, aut_email, remote_phone = sx["remote_email"], sx["aut_email"], sx["remote_phone"]
    token = _token()

    # Snapshots taken right before the trigger so nothing pre-existing counts.
    edges_before = _edge_files()
    sms_before = {m.id for m in _inbound_sms_from(remote, remote_pid, aut_phone)}
    email_before = {m.id for m in _inbound_email_from(remote, remote_email, aut_email)}

    # A -> agent: fire-and-forget errand, explicitly no report-back.
    remote.messages.send(
        remote_email, to=[aut_email], subject=f"[{token}] quick favor",
        body_text=(f"Please send a text to {remote_phone} letting them know the plan is on. "
                   f"No need to report anything back to me."),
    )

    # Wait for the agent's outbound SMS to B (b1: the agent tried).
    got_sms = None
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        for m in _inbound_sms_from(remote, remote_pid, aut_phone):
            if m.id not in sms_before:
                got_sms = m
                break
        if got_sms:
            break
        time.sleep(POLL_EVERY_S)
    assert got_sms is not None, "agent never texted B in the baseline leg"

    # B replies with a token A never asked about.
    remote.texts.send(remote_pid, to=aut_phone, text=f"Got it. FYI my desk code is {token}.")

    # b3: over the settle window A must receive NO email carrying B's token.
    deadline = time.monotonic() + BASELINE_WINDOW_S
    while time.monotonic() < deadline:
        for m in _inbound_email_from(remote, remote_email, aut_email):
            if m.id in email_before:
                continue
            assert not _email_carries_token(remote, remote_email, m, token), (
                "baseline relayed B's token back to A — expected the amnesiac gap "
                f"(token {token})"
            )
        time.sleep(POLL_EVERY_S)

    # b2: a plain send (no spinoff arg) must create no lineage edge at all.
    assert _edge_files() == edges_before, "baseline created a spin-off edge without a spinoff arg"


def test_spinoff_delegation_relays_answer_once(sx):
    """SPINOFF: a delegation seeds a durable edge and relays B's answer home once.

    Asserts (n2) an edge advances delivered -> awaiting_reply -> relayed on
    disk with recipientBinding + parentRoute populated, (n1) the spy shows BOTH
    the A->B send and a later send to A, (n4) A's inbox gets exactly ONE inbound
    carrying B's answer token, and (n5) a redelivered B reply still yields one
    relay.
    """
    remote, remote_pid, aut_phone = sx["remote"], sx["remote_pid"], sx["aut_phone"]
    remote_email, aut_email, remote_phone = sx["remote_email"], sx["aut_email"], sx["remote_phone"]
    token = _token()

    # Snapshots so no pre-existing edge/send/inbound satisfies an assertion.
    edges_before = _edge_files()
    spy_before = len(_spy_lines())
    sms_before = {m.id for m in _inbound_sms_from(remote, remote_pid, aut_phone)}
    email_before = {m.id for m in _inbound_email_from(remote, remote_email, aut_email)}

    # Accumulate every status our spin-off edge is observed in, so we can prove
    # it progressed through its lifecycle (transient states are easy to miss on
    # a single read, so we collect across every poll).
    seen_status: set = set()

    def _accumulate_statuses() -> None:
        for name in _edge_files() - edges_before:
            edge = _read_edge_file(name)
            if edge and edge.get("channelChild") == "sms":
                st = edge.get("status")
                if st:
                    seen_status.add(st)

    # A -> agent: an explicit delegation with a report-back.
    remote.messages.send(
        remote_email, to=[aut_email], subject=f"[{token}] need the codeword",
        body_text=(f"Please send a text to {remote_phone} and ask them for today's secret "
                   f"codeword, then email me back exactly what they tell you."),
    )

    # 1) Wait for the agent's outbound SMS to B (the spawn fires).
    got_sms = None
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        _accumulate_statuses()  # catch the freshly-created 'delivered' edge
        for m in _inbound_sms_from(remote, remote_pid, aut_phone):
            if m.id not in sms_before:
                got_sms = m
                break
        if got_sms:
            break
        time.sleep(POLL_EVERY_S)
    assert got_sms is not None, "agent never texted B (the spawn did not fire)"

    # 2) B answers over SMS with the codeword token.
    remote.texts.send(remote_pid, to=aut_phone, text=f"Sure — today's secret codeword is {token}.")

    # 3) Wait for the relay: a NEW email to A carrying B's answer token.
    relay = None
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        _accumulate_statuses()  # catch 'awaiting_reply' then 'relayed'
        for m in _inbound_email_from(remote, remote_email, aut_email):
            if m.id in email_before:
                continue
            if _email_carries_token(remote, remote_email, m, token):
                body = (getattr(remote.messages.get(remote_email, m.id), "body_text", "") or "").lower()
                bad = [x for x in ERROR_MARKERS if x in body]
                assert not bad, f"relay email is an error, not a real answer: {bad}"
                relay = m
                break
        if relay:
            break
        time.sleep(POLL_EVERY_S)
    assert relay is not None, f"agent never relayed B's answer ({token}) back to A"
    _accumulate_statuses()

    # n2: the durable edge exists, is bound, and progressed to 'relayed'.
    new_edges = [_read_edge_file(n) for n in (_edge_files() - edges_before)]
    new_edges = [e for e in new_edges if e and e.get("channelChild") == "sms"]
    assert new_edges, "no spin-off edge was recorded for the delegation"
    # Prefer the edge that carries a distilled result (the one that got relayed).
    edge = next((e for e in new_edges if e.get("result")), new_edges[0])
    assert edge.get("recipientBinding", {}).get("outboundMessageId"), "edge missing recipientBinding"
    assert edge.get("parentRoute"), "edge missing parentRoute (relay could not target A)"
    assert edge.get("result"), "edge has no distilled result recorded"
    assert "relayed" in seen_status, f"edge never reached 'relayed' (saw {sorted(seen_status)})"
    assert len(seen_status) >= 2, f"edge did not progress through its lifecycle (saw {sorted(seen_status)})"

    # n1: the spy shows BOTH the A->B SMS send and a later email send to A.
    new_spy = _spy_lines()[spy_before:]
    b_tail = _digits(remote_phone)[-10:]
    sms_sends = [r for r in new_spy
                 if r.get("method", "").endswith("TextsResource.send")
                 and b_tail in _digits(_spy_haystack(r))]
    email_sends = [r for r in new_spy
                   if r.get("method", "").endswith("MessagesResource.send")
                   and remote_email.lower() in _spy_haystack(r).lower()]
    assert sms_sends, "spy shows no A->B SMS send"
    assert email_sends, "spy shows no later email send back to A"

    # n4: A got exactly ONE inbound carrying B's token.
    def _a_inbounds_with_token() -> set:
        hits = set()
        for m in _inbound_email_from(remote, remote_email, aut_email):
            if m.id in email_before:
                continue
            if _email_carries_token(remote, remote_email, m, token):
                hits.add(m.id)
        return hits

    assert len(_a_inbounds_with_token()) == 1, "expected exactly one relayed answer to A"

    # n5: replay B's answer; the answered->relayed CAS must keep it to ONE relay.
    remote.texts.send(remote_pid, to=aut_phone, text=f"(resending) the secret codeword is {token}.")
    deadline = time.monotonic() + REDELIVERY_WINDOW_S
    while time.monotonic() < deadline:
        time.sleep(POLL_EVERY_S)
        if len(_a_inbounds_with_token()) > 1:
            pytest.fail("redelivery produced a duplicate relay to A")
    assert len(_a_inbounds_with_token()) == 1, "redelivery must not add a second relay to A"
