"""Live SMS suite — the same questions as the email suite, over real SMS.

SMS differs from email: the remote must opt in (agent-to-agent SMS skips the
START opt-in — servers bypasses it for inter-agent traffic), and outbound SMS is
subject to carrier + spam filtering — so prompts ask for SHORT replies and avoid
spammy content.

  * mock leg → reachability (deterministic ``REPLY_OK`` from the mock model).
  * real leg → intelligence (gpt-5.5): basic, own identity, sender, tools.

Skipped unless both keys are set. Replies are matched by *new* inbound message id
from the AUT's number (robust to clock skew).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
REAL = os.environ.get("LIVE_REAL_MODEL") == "1"
TIMEOUT_S = float(os.environ.get("LIVE_SMS_TIMEOUT", "180"))
POLL_EVERY_S = 6.0
ERROR_MARKERS = ("non-retryable error", "missing authentication", "http 401", "http 403", "traceback")
# Delivery-failure retry tests: the loop adds a full extra agent turn
# (wake → rewrite → resend), so they get a longer budget than one Q/A.
RETRY_TIMEOUT_S = TIMEOUT_S + 120
SPY_FILE = os.environ.get("INKBOX_SPY_FILE", "")
GATEWAY_LOG = os.environ.get("GATEWAY_LOG", "")
# The AUT's org signing key — same value the gateway verifies webhooks
# with — lets the test forge a valid delivery-failure webhook. Exported
# under a dedicated name on purpose: other live suites (external events)
# gate their skips on HERMES_INKBOX_SIGNING_KEY, and exporting THAT name
# here would un-skip them inside a workflow that isn't configured for
# them (no INKBOX_EXTERNAL_EVENTS_ENABLED on this gateway).
SIGNING_KEY = os.environ.get("AUT_INKBOX_SIGNING_KEY", "")
AUT_WEBHOOK_URL = os.environ.get("AUT_WEBHOOK_URL", "http://127.0.0.1:8765/webhook")

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY),
    reason="live SMS suite: needs REMOTE_INKBOX_API_KEY + HERMES_INKBOX_API_KEY",
)
real_only = pytest.mark.skipif(not REAL, reason="intelligence runs in the real-model leg")
mock_only = pytest.mark.skipif(REAL, reason="reachability runs in the mock-model leg")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _client(key):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _phone(client):
    nums = client.phone_numbers.list()
    assert nums, "identity has no phone number"
    return nums[0].number, str(nums[0].id)


@pytest.fixture(scope="module")
def sms():
    remote = _client(REMOTE_KEY)
    aut = _client(AUT_KEY)
    aut_phone, _aut_pid = _phone(aut)
    _remote_phone, remote_pid = _phone(remote)
    # No opt-in/START needed: servers bypasses the missing-opt-in gate for
    # inter-agent traffic (recipient is a Telnyx-owned Inkbox number) — see
    # send_text_service.py. Only an explicit STOP/opt-out would block.
    return {"remote": remote, "aut": aut, "aut_phone": aut_phone, "remote_pid": remote_pid}


def _inbound_from_aut(sms):
    """List the remote's inbound messages that came from the AUT's number."""
    remote, aut_phone, pid = sms["remote"], sms["aut_phone"], sms["remote_pid"]
    tail = _digits(aut_phone)[-10:]
    out = []
    for m in remote.texts.list(pid, limit=30):
        if (getattr(m, "direction", "") or "").lower() == "inbound" \
                and _digits(getattr(m, "remote_phone_number", "") or "")[-10:] == tail:
            out.append(m)
    return out


def _settle_inbound(sms) -> set:
    """Drain to a quiet state; return the settled inbound id-set.

    The agent sometimes emits a trailing *second* SMS for the PREVIOUS question
    (a duplicate "OK", or a masked + unmasked identity pair) that lands a few
    seconds late. Matching on "any new inbound id after I sent" would let that
    leftover leak into the next question's match, so we poll until the id-set
    stops growing — folding any in-flight trailing reply into the baseline.
    """
    before = {m.id for m in _inbound_from_aut(sms)}
    quiet_deadline = time.monotonic() + 2 * POLL_EVERY_S
    while time.monotonic() < quiet_deadline:
        time.sleep(POLL_EVERY_S)
        now_ids = {m.id for m in _inbound_from_aut(sms)}
        if now_ids == before:
            break
        before = now_ids
    return before


def _wait_new_inbound(sms, before: set, timeout_s: float, context: str) -> str:
    """Poll for the first inbound not in ``before``; return its body lowercased."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for m in _inbound_from_aut(sms):
            if m.id not in before:
                body = getattr(m, "text", "") or ""
                bad = [x for x in ERROR_MARKERS if x in body.lower()]
                assert not bad, f"SMS reply is an error, not a real answer: {bad}\n{body[:200]}"
                return body.lower()
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"no SMS reply within {timeout_s:.0f}s to: {context}")


def _ask_sms(sms, text: str, timeout_s: float = TIMEOUT_S) -> str:
    """Text the agent; return the reply body (lowercased), matched by new message id."""
    remote, aut_phone, pid = sms["remote"], sms["aut_phone"], sms["remote_pid"]
    before = _settle_inbound(sms)
    remote.texts.send(pid, to=aut_phone, text=text)
    return _wait_new_inbound(sms, before, timeout_s, repr(text))


def _spy_text_sends() -> list:
    """Parse the gateway send-spy file down to the SMS send records."""
    if not SPY_FILE or not os.path.exists(SPY_FILE):
        return []
    out = []
    for line in open(SPY_FILE, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "Texts" in rec.get("method", ""):
            out.append(rec)
    return out


def _gateway_log_since(offset: int) -> str:
    """Return gateway log text past ``offset`` ('' when the log isn't wired)."""
    if not GATEWAY_LOG or not os.path.exists(GATEWAY_LOG):
        return ""
    with open(GATEWAY_LOG, encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return fh.read()


def _gateway_log_size() -> int:
    if not GATEWAY_LOG or not os.path.exists(GATEWAY_LOG):
        return 0
    return os.path.getsize(GATEWAY_LOG)


@mock_only
def test_sms_reachability(sms):
    body = _ask_sms(sms, "ping")
    assert "reply_ok" in body, f"mock reachability: missing REPLY_OK marker\n{body[:200]}"


@real_only
def test_sms_basic_reply(sms):
    body = _ask_sms(sms, "Please reply OK to confirm you got this text.")
    assert len(body.strip()) > 0, "empty reply"


@real_only
def test_sms_reports_own_identity(sms):
    aut_email = sms["aut"].mailboxes.list()[0].email_address
    body = _ask_sms(sms, "Reply with just your Inkbox email address and phone number — short.")
    assert aut_email in body, f"reply missing email {aut_email!r}\n{body[:200]}"


@real_only
def test_sms_reports_sender_details(sms):
    aut, remote = sms["aut"], sms["remote"]
    remote_email = remote.mailboxes.list()[0].email_address
    matches = aut.contacts.lookup(email=remote_email)
    if not matches:
        pytest.skip("no contact card for the sender to report")
    name = (getattr(matches[0], "preferred_name", None) or getattr(matches[0], "given_name", None) or "")
    body = _ask_sms(sms, "Who am I to you? Tell me what you have on file about me.")
    if name:
        assert name.lower() in body, f"reply missing sender name {name!r}\n{body[:200]}"


@real_only
def test_sms_aware_of_inkbox_tools(sms):
    import yaml

    manifest = yaml.safe_load(Path(__file__).resolve().parents[2].joinpath("plugin.yaml").read_text())
    tool_names = [t for t in manifest.get("provides_tools", []) if isinstance(t, str)]
    body = _ask_sms(sms, "Name three of your Inkbox tools (exact names).")
    hits = [t for t in tool_names if t.lower() in body]
    assert len(hits) >= 2, f"agent named only {hits} of its tools\n{body[:300]}"


# ── Outbound delivery-failure retry loop ────────────────────────────────
#
# Ordering matters: the carrier-failure test runs FIRST. The spam-block
# test deliberately creates blocked_spam_filter rows on the AUT's number,
# and listing a conversation containing such rows crashes SDK clients
# whose SmsDeliveryStatus enum predates that status — the carrier test's
# conversation-id lookup must run against a clean history.
#
# Loop evidence comes from the gateway log (the plugin's wake-up lines),
# which is authoritative. The send-spy is reported as a note only — it is
# known not to load reliably inside the gateway process (see the same
# caveat in test_email_reply.py).


def _sign_inkbox_webhook(payload: bytes, request_id: str, timestamp: str, secret: str) -> str:
    """Forge the Inkbox webhook signature: HMAC-SHA256 over id.ts.body."""
    key = secret.removeprefix("whsec_")
    message = f"{request_id}.{timestamp}.".encode() + payload
    return "sha256=" + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def _inject_inkbox_webhook(envelope: dict) -> int:
    """POST a signed Inkbox-style webhook to the gateway's local listener."""
    payload = json.dumps(envelope).encode()
    request_id = str(uuid.uuid4())
    timestamp = str(int(time.time()))
    req = urllib.request.Request(
        AUT_WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Inkbox-Request-Id": request_id,
            "X-Inkbox-Timestamp": timestamp,
            "X-Inkbox-Signature": _sign_inkbox_webhook(
                payload, request_id, timestamp, SIGNING_KEY,
            ),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def _assert_wake_logged(log_offset: int, stage: str) -> None:
    """Require the retry loop's gateway-log fingerprint past ``log_offset``."""
    log = _gateway_log_since(log_offset)
    assert "Woke agent about failed outbound sms" in log, (
        "no delivery-failure wake-up in the gateway log — retry loop did not run"
    )
    assert f"stage={stage}" in log


@real_only
@pytest.mark.skipif(not SIGNING_KEY, reason="needs AUT_INKBOX_SIGNING_KEY to sign the fake webhook")
def test_sms_retry_after_carrier_delivery_failure(sms):
    """Inject a fake carrier delivery-failure webhook; expect a real follow-up.

    Simulates the async failure surface: the send was accepted, then the
    carrier flagged it (error 40002) and the server reported it via a
    ``text.delivery_failed`` webhook. The webhook is forged with the AUT's
    own signing key and posted to the gateway's local webhook listener —
    exactly how a real delivery would arrive through the tunnel. The
    plugin must wake the agent, and the agent must send a real follow-up
    SMS that reaches the remote.
    """
    aut, remote = sms["aut"], sms["remote"]
    aut_phone = sms["aut_phone"]
    remote_phone, _remote_pid = _phone(remote)
    _aut_number, aut_pid = _phone(aut)

    # Prime the conversation so the agent has live routing state and the
    # wake-up lands in an existing session.
    _ask_sms(sms, "Please reply OK to confirm you got this text.")

    # The AUT-side conversation id for this thread, read from its own API.
    # Best-effort: a history row the installed SDK cannot hydrate (e.g. a
    # delivery status newer than its enum) must not kill the test — the
    # injected failure also routes fine by remote number alone.
    conversation_id = ""
    remote_tail = _digits(remote_phone)[-10:]
    try:
        for m in aut.texts.list(aut_pid, limit=30):
            if _digits(getattr(m, "remote_phone_number", "") or "")[-10:] == remote_tail:
                conversation_id = str(getattr(m, "conversation_id", "") or "")
                if conversation_id:
                    break
    except Exception as exc:
        print(f"note: conversation-id lookup failed ({exc!r}); injecting without one")

    spy_before = len(_spy_text_sends())
    log_offset = _gateway_log_size()
    before = _settle_inbound(sms)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    envelope = {
        "id": f"evt_{uuid.uuid4()}",
        "event_type": "text.delivery_failed",
        "timestamp": now,
        "data": {
            "text_message": {
                "id": str(uuid.uuid4()),
                "direction": "outbound",
                "local_phone_number": aut_phone,
                "remote_phone_number": remote_phone,
                "conversation_id": conversation_id or None,
                "text": "Quick update: everything is on track for tomorrow.",
                "type": "sms",
                "media": None,
                "is_read": True,
                "delivery_status": "delivery_failed",
                "error_code": "40002",
                "error_detail": (
                    "The message was flagged by a SPAM filter and was not "
                    "delivered. This is a temporary condition."
                ),
                "created_at": now,
                "updated_at": now,
            },
            "contacts": [],
            "agent_identities": [],
            "recipient_phone_number": None,
        },
    }
    status = _inject_inkbox_webhook(envelope)
    assert status == 200, f"gateway rejected the forged delivery-failure webhook: {status}"

    # The agent must react with a real, delivered follow-up SMS.
    body = _wait_new_inbound(
        sms, before, RETRY_TIMEOUT_S, "follow-up after injected delivery failure",
    )
    assert body.strip(), "agent sent an empty follow-up"

    # The loop itself must have fired for the carrier failure.
    if GATEWAY_LOG:
        _assert_wake_logged(log_offset, "delivery_failed")

    # Spy is informational only — it is known not to load in the gateway
    # process on some installs.
    if SPY_FILE and len(_spy_text_sends()) - spy_before < 1:
        print("note: follow-up delivered, but the in-gateway send-spy recorded no send")


@real_only
def test_sms_retry_after_internal_spam_block(sms):
    """Bait the server's outbound content filter, then watch the retry loop.

    Asking for three emojis makes the agent's first reply trip the server's
    one-emoji SMS budget (a synchronous 422 block — no carrier send). The
    plugin must wake the agent with the block reason, and the agent must
    come back with a reply that actually delivers. NOTE: the *ask* itself
    must contain no emojis — the remote driver's outbound rides the same
    filter, and an emoji-laden question would be blocked before the AUT
    ever saw it.
    """
    spy_before = len(_spy_text_sends())
    log_offset = _gateway_log_size()

    body = _ask_sms(
        sms,
        "Fun formatting test: reply with ONE short message that contains at "
        "least three different emojis of your choice. Just send it, no questions.",
        timeout_s=RETRY_TIMEOUT_S,
    )

    # A reply arrived at all — so whatever the agent ended up sending
    # cleared the filter (an apology without emojis also counts).
    assert body.strip(), "agent never got a compliant reply through"

    # The loop itself must have fired: the plugin logs a wake-up for the
    # synchronous send rejection. This is the authoritative loop evidence.
    if GATEWAY_LOG:
        _assert_wake_logged(log_offset, "send_rejected")

    # Spy is informational only — it is known not to load in the gateway
    # process on some installs.
    if SPY_FILE and len(_spy_text_sends()) - spy_before < 2:
        print("note: reply delivered, but the in-gateway send-spy saw fewer than 2 sends")
