#!/usr/bin/env python3
"""Mock a signed external event into a running Inkbox plugin agent.

External systems (e.g. a GitHub Actions workflow) reach the agent through the
same signed ``POST /webhook`` chokepoint as Inkbox traffic. Any event the
adapter doesn't recognize as a known Inkbox type (mail/text/imessage/call)
falls through to the external path and wakes the agent on a fresh thread.

This script reproduces the exact request the ``yc-product-showcase`` workflow
sends — a flat ``agent_escalation_demo`` JSON body signed with HMAC-SHA256 over
``{request_id}.{timestamp}.{payload}`` (the scheme ``inkbox.verify_webhook``
expects) — so you can wake the agent locally without a real upstream webhook.

Usage:
    python scripts/send_external_event.py \
        --url http://127.0.0.1:8765/webhook \
        --requested-action "Call Dima and explain what happened."

The signing key is read from ``--signing-key`` or ``$INKBOX_SIGNING_KEY``. Pass
``--no-sign`` to omit the signature (the demo logs "sending unsigned").
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.request
import uuid


def _sign(payload: bytes, *, request_id: str, timestamp: str, secret: str) -> str:
    """Reproduce Inkbox's webhook HMAC over ``{request_id}.{timestamp}.`` + body."""
    key = secret.removeprefix("whsec_")  # the key is stored with or without the prefix
    message = f"{request_id}.{timestamp}.".encode() + payload
    digest = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/webhook")
    parser.add_argument("--repository", default="inkbox-ai/servers")
    parser.add_argument("--workflow", default="YC product showcase")
    parser.add_argument("--title", default="Agent escalation demo")
    parser.add_argument("--severity", default="demo", help="e.g. demo / prod / beta")
    parser.add_argument("--summary", default="A demo workflow requested human follow-up.")
    parser.add_argument(
        "--requested-action", default="Call Dima and explain what happened."
    )
    parser.add_argument("--run-id", default="", help="GitHub run id (defaults to random)")
    parser.add_argument(
        "--signing-key",
        default=os.getenv("INKBOX_SIGNING_KEY", ""),
        help="Inkbox signing key (defaults to $INKBOX_SIGNING_KEY)",
    )
    parser.add_argument("--no-sign", action="store_true", help="Omit the signature")
    args = parser.parse_args()

    if not args.no_sign and not args.signing_key:
        parser.error("no signing key: pass --signing-key, set INKBOX_SIGNING_KEY, or --no-sign")

    run_id = args.run_id or str(uuid.uuid4().int % 10**17)
    # The exact body shape the yc-product-showcase workflow posts.
    envelope = {
        "event": "agent_escalation_demo",
        "title": args.title,
        "severity": args.severity,
        "summary": args.summary,
        "requested_action": args.requested_action,
        "github": {
            "repository": args.repository,
            "workflow": args.workflow,
            "run_id": run_id,
            "run_url": f"https://github.com/{args.repository}/actions/runs/{run_id}",
        },
    }
    payload = json.dumps(envelope).encode()

    request_id = str(uuid.uuid4())
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Inkbox-Demo": "yc-showcase",
        "X-Inkbox-Request-Id": request_id,
        "X-Inkbox-Timestamp": timestamp,
    }
    if not args.no_sign:
        # Sign exactly like Inkbox: request-id + timestamp + raw body.
        headers["X-Inkbox-Signature"] = _sign(
            payload, request_id=request_id, timestamp=timestamp, secret=args.signing_key
        )

    request = urllib.request.Request(args.url, data=payload, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request) as resp:  # noqa: S310 — local dev tool
            print(f"{resp.status} {resp.read().decode()}")
    except urllib.error.HTTPError as exc:  # surface 401/4xx bodies for the wrong-payload test
        print(f"{exc.code} {exc.read().decode()}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
