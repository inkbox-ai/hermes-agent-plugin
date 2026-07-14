"""Live end-to-end coverage for an Inkbox-signed external event.

The provider boundary is deterministic: accept a correctly signed event and
prove it reaches Hermes' session queue. Real calls and model/tool behavior are
covered by the voice suite; a webhook's free-form requested_action is data, not
an instruction whose exact obedience this transport test should depend on.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.request
import uuid

import pytest

SIGNING_KEY = os.environ.get("HERMES_INKBOX_SIGNING_KEY") or os.environ.get("INKBOX_SIGNING_KEY")
WEBHOOK_URL = os.environ.get("AUT_WEBHOOK_URL", "http://127.0.0.1:8765/webhook")
GATEWAY_LOG = (
    os.path.join(os.environ["HERMES_HOME"], "logs", "gateway.log")
    if os.environ.get("HERMES_HOME")
    else os.environ.get("GATEWAY_LOG", "")
)
TIMEOUT_S = float(os.environ.get("LIVE_EXTERNAL_TIMEOUT", "45"))
POLL_EVERY_S = 0.5

pytestmark = pytest.mark.skipif(
    not (SIGNING_KEY and GATEWAY_LOG and os.environ.get("LIVE_REAL_MODEL") == "1"),
    reason="external-event suite needs signing key + gateway log + LIVE_REAL_MODEL=1",
)


def _sign(payload: bytes, *, request_id: str, timestamp: str, secret: str) -> str:
    key = secret.removeprefix("whsec_")
    message = f"{request_id}.{timestamp}.".encode() + payload
    return "sha256=" + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def _post(envelope: dict) -> tuple[int, str]:
    payload = json.dumps(envelope).encode()
    request_id = str(uuid.uuid4())
    timestamp = str(int(time.time()))
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Inkbox-Request-Id": request_id,
            "X-Inkbox-Timestamp": timestamp,
            "X-Inkbox-Signature": _sign(
                payload, request_id=request_id, timestamp=timestamp, secret=SIGNING_KEY
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 -- local gateway
        return resp.status, resp.read().decode()


def _wait_for_log(marker: str) -> bool:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            with open(GATEWAY_LOG, encoding="utf-8") as handle:
                if marker in handle.read():
                    return True
        except FileNotFoundError:
            pass
        time.sleep(POLL_EVERY_S)
    return False


def test_signed_external_event_reaches_hermes_session_queue():
    event_id = str(uuid.uuid4().int % 10**17)
    envelope = {
        "id": event_id,
        "source": "live-e2e",
        "event": "deployment_completed",
        "title": "Live external-event delivery probe",
        "summary": "The synthetic deployment completed successfully.",
        "severity": "informational",
    }
    marker = f"[Inkbox] External event enqueued: external:live-e2e:{event_id}"
    status, body = _post(envelope)
    assert status == 200 and body == "ok", f"webhook not accepted: {status} {body!r}"
    assert _wait_for_log(marker), f"signed event never reached Hermes: {marker}"
