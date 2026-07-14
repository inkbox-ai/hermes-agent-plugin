"""Live provider-boundary coverage for realistic GitHub workflow_run hooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid

import pytest

GITHUB_SECRET = os.environ.get("INKBOX_WEBHOOK_SECRET_GITHUB")
WEBHOOK_URL = os.environ.get("AUT_WEBHOOK_URL", "http://127.0.0.1:8765/webhook")
GATEWAY_LOG = (
    os.path.join(os.environ["HERMES_HOME"], "logs", "gateway.log")
    if os.environ.get("HERMES_HOME")
    else os.environ.get("GATEWAY_LOG", "")
)
TIMEOUT_S = float(os.environ.get("LIVE_GITHUB_SESSION_TIMEOUT", "45"))
POLL_EVERY_S = 0.5

pytestmark = pytest.mark.skipif(
    not (GITHUB_SECRET and GATEWAY_LOG and os.environ.get("LIVE_REAL_MODEL") == "1"),
    reason="github external-event suite needs secret + gateway log + LIVE_REAL_MODEL=1",
)


def _post(envelope: dict, signature: str) -> tuple[int, str]:
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
            "X-Inkbox-Request-Id": str(uuid.uuid4()),
            "X-Hub-Signature-256": signature,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 -- local gateway
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _envelope() -> dict:
    repository = os.environ.get("GITHUB_REPOSITORY", "inkbox-ai/hermes-agent-plugin")
    run_id = str(uuid.uuid4().int % 10**17)
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "name": "CI",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "html_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        },
        "repository": {"full_name": repository},
    }


def _marker(envelope: dict) -> str:
    return (
        "[Inkbox] External event enqueued: external:"
        f"{envelope['repository']['full_name']}:{envelope['workflow_run']['id']}"
    )


def _log() -> str:
    try:
        with open(GATEWAY_LOG, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def _wait(marker: str) -> bool:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        if marker in _log():
            return True
        time.sleep(POLL_EVERY_S)
    return False


def test_forged_github_signature_is_rejected_before_agent_wakes():
    envelope = _envelope()
    status, body = _post(envelope, "sha256=deadbeef")
    assert status == 401, f"forged signature should be rejected, got {status} {body!r}"
    time.sleep(2)
    assert _marker(envelope) not in _log()


def test_valid_github_signature_reaches_hermes_session_queue():
    envelope = _envelope()
    payload = json.dumps(envelope).encode()
    signature = "sha256=" + hmac.new(GITHUB_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    status, body = _post(envelope, signature)
    assert status == 200 and body == "ok", f"valid webhook not accepted: {status} {body!r}"
    assert _wait(_marker(envelope)), "valid GitHub event never reached Hermes"
