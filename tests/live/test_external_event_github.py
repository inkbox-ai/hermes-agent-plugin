"""Live intelligence suite over a GitHub-signed external webhook.

Exercises a real third-party provider end to end: the plugin's ``github``
:class:`WebhookProvider` verifies ``X-Hub-Signature-256`` (HMAC-SHA256 over the
raw body with ``INKBOX_WEBHOOK_SECRET_GITHUB``). Two events with identical
content — "a GitHub Action failed, call Jane Doe immediately":

  * **forged signature** → rejected at the webhook (401), the agent is never
    woken, and no call is placed;
  * **valid signature** → verified, handed to the agent as an external event,
    and the real model reasons "escalation → call this contact" and *places a
    call* to Jane Doe (the driver).

Jane Doe is the remote/driver identity, seeded as a contact in the AUT org and
parked on ``auto_reject`` — we monitor that the agent dialed, not the call
itself. Skipped unless both identity keys + the GitHub webhook secret +
``LIVE_REAL_MODEL=1`` are set.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
GITHUB_SECRET = os.environ.get("INKBOX_WEBHOOK_SECRET_GITHUB")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
WEBHOOK_URL = os.environ.get("AUT_WEBHOOK_URL", "http://127.0.0.1:8765/webhook")
TIMEOUT_S = float(os.environ.get("LIVE_EXTERNAL_TIMEOUT", "200"))
# How long to watch after the forged event to be confident nothing was dialed.
FORGED_QUIET_S = float(os.environ.get("LIVE_FORGED_QUIET", "40"))
POLL_EVERY_S = 6.0
DRIVER_NAME = "Jane Doe"

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY and GITHUB_SECRET and os.environ.get("LIVE_REAL_MODEL") == "1"),
    reason="github external-event suite: needs both keys + INKBOX_WEBHOOK_SECRET_GITHUB + LIVE_REAL_MODEL=1",
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _client(key):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _first_phone(client):
    nums = client.phone_numbers.list()
    assert nums, "identity has no phone number"
    return nums[0]


def _sign_github(payload: bytes, secret: str) -> str:
    """GitHub's scheme: HMAC-SHA256 over the raw body, ``sha256=<hex>``."""
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _post_github_event(envelope: dict, *, signature: str) -> tuple[int, str]:
    """POST a GitHub-style webhook with the given ``X-Hub-Signature-256``."""
    payload = json.dumps(envelope).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Hookshot/live-test",
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Inkbox-Request-Id": str(uuid.uuid4()),  # plugin dedups on this
            "X-Hub-Signature-256": signature,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — local gateway
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:  # 401 on a forged signature
        return exc.code, exc.read().decode()


def _ensure_driver_contact(aut, driver_phone: str) -> None:
    """Seed a ``Jane Doe`` contact for the driver number if the AUT lacks one."""
    if aut.contacts.lookup(phone=driver_phone):
        return
    from inkbox.contacts.types import ContactPhone

    given, _, family = DRIVER_NAME.partition(" ")
    aut.contacts.create(
        given_name=given,
        family_name=family or "Driver",
        phones=[ContactPhone(label="mobile", value=driver_phone)],
    )


def _outbound_calls_to(aut, aut_number_id, driver_phone: str) -> list:
    """AUT's outbound calls dialed to the driver's number (newest first)."""
    tail = _digits(driver_phone)[-10:]
    return [
        c for c in aut.calls.list(limit=30)
        if (getattr(c, "direction", "") or "").lower() == "outbound"
        and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == tail
    ]


def _escalation_envelope() -> dict:
    """A GitHub Actions failure asking the agent to phone Jane Doe."""
    run_id = str(uuid.uuid4().int % 10**17)
    return {
        "event": "workflow_run",
        "action": "completed",
        "conclusion": "failure",
        "title": "CI failed on main",
        "severity": "prod",
        "summary": "A GitHub Action failed on the servers repo; production deploy is blocked.",
        "requested_action": (
            f"Call {DRIVER_NAME} immediately by phone (use inkbox_place_call) and tell "
            "them a GitHub Action failed and the deploy is blocked. This is urgent — "
            "place the call now."
        ),
        "repository": {"full_name": "inkbox-ai/servers"},
        "workflow_run": {
            "id": run_id,
            "name": "CI",
            "html_url": f"https://github.com/inkbox-ai/servers/actions/runs/{run_id}",
        },
    }


@pytest.fixture(scope="module")
def ctx():
    remote, aut = _client(REMOTE_KEY), _client(AUT_KEY)
    driver_num = _first_phone(remote)
    aut_num = _first_phone(aut)

    prev_action = getattr(driver_num, "incoming_call_action", None)
    remote.phone_numbers.update(driver_num.id, incoming_call_action="auto_reject")
    _ensure_driver_contact(aut, driver_num.number)
    try:
        yield {"aut": aut, "aut_number_id": str(aut_num.id), "driver_phone": driver_num.number}
    finally:
        try:
            remote.phone_numbers.update(driver_num.id, incoming_call_action=prev_action or "auto_reject")
        except Exception:
            pass


def test_forged_github_signature_is_dropped_and_agent_does_nothing(ctx):
    """A forged X-Hub-Signature-256 → 401 at the webhook, agent never dials."""
    aut, aut_number_id, driver_phone = ctx["aut"], ctx["aut_number_id"], ctx["driver_phone"]
    before = {c.id for c in _outbound_calls_to(aut, aut_number_id, driver_phone)}

    status, body = _post_github_event(_escalation_envelope(), signature="sha256=deadbeef")
    assert status == 401, f"forged signature should be rejected, got {status} {body!r}"

    # Watch briefly: a rejected event must not produce any call to the driver.
    deadline = time.monotonic() + FORGED_QUIET_S
    while time.monotonic() < deadline:
        fresh = [c for c in _outbound_calls_to(aut, aut_number_id, driver_phone) if c.id not in before]
        assert not fresh, f"agent dialed on a FORGED event: {fresh}"
        time.sleep(POLL_EVERY_S)


def test_valid_github_signature_makes_agent_call_jane(ctx):
    """A validly-signed GitHub failure → the agent places a call to Jane Doe."""
    aut, aut_number_id, driver_phone = ctx["aut"], ctx["aut_number_id"], ctx["driver_phone"]
    before = {c.id for c in _outbound_calls_to(aut, aut_number_id, driver_phone)}

    envelope = _escalation_envelope()
    payload = json.dumps(envelope).encode()
    status, body = _post_github_event(envelope, signature=_sign_github(payload, GITHUB_SECRET))
    assert status == 200 and body == "ok", f"valid webhook not accepted: {status} {body!r}"

    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        fresh = [c for c in _outbound_calls_to(aut, aut_number_id, driver_phone) if c.id not in before]
        if fresh:
            return  # the agent escalated by phoning Jane Doe — exactly what we monitor for
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"agent never called {DRIVER_NAME} within {TIMEOUT_S:.0f}s")
