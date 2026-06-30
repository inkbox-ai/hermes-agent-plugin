#!/usr/bin/env python3
"""Mock a signed external event into a running Inkbox plugin agent.

External systems (e.g. a GitHub Actions failure) reach the agent through the
same signed ``POST /webhook`` chokepoint as Inkbox traffic. This script signs a
``external.event`` envelope with the agent's ``INKBOX_SIGNING_KEY`` exactly the
way Inkbox does (see ``inkbox.signing_keys.verify_webhook``) and posts it, so
you can wake the agent on a fresh thread without a real upstream webhook.

Usage:
    python scripts/send_external_event.py \
        --url http://127.0.0.1:8765/webhook \
        --source github \
        --environment prod \
        --title "giblins lit prod server aflame" \
        --body "Workflow deploy.yml failed on main (run #4821)" \
        --link https://github.com/inkbox-ai/servers/actions/runs/4821

The signing key is read from ``--signing-key`` or ``$INKBOX_SIGNING_KEY``.
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
    parser.add_argument("--source", default="github", help="Where the event came from")
    parser.add_argument("--environment", default="", help="prod / beta / dev")
    parser.add_argument("--title", default="giblins lit prod server aflame")
    parser.add_argument("--body", default="")
    parser.add_argument("--link", default="", help="Optional URL for the event")
    parser.add_argument("--event-id", default="", help="Stable id (defaults to a uuid)")
    parser.add_argument(
        "--signing-key",
        default=os.getenv("INKBOX_SIGNING_KEY", ""),
        help="Inkbox signing key (defaults to $INKBOX_SIGNING_KEY)",
    )
    args = parser.parse_args()

    if not args.signing_key:
        parser.error("no signing key: pass --signing-key or set INKBOX_SIGNING_KEY")

    # Build the same envelope shape the adapter's _on_external_event expects.
    data = {
        "source": args.source,
        "title": args.title,
        "body": args.body,
        "url": args.link,
        "environment": args.environment,
        "id": args.event_id or str(uuid.uuid4()),
    }
    envelope = {"event_type": "external.event", "data": data}
    payload = json.dumps(envelope).encode()

    # Sign exactly like Inkbox: request-id + timestamp + raw body.
    request_id = str(uuid.uuid4())
    timestamp = str(int(time.time()))
    signature = _sign(
        payload, request_id=request_id, timestamp=timestamp, secret=args.signing_key
    )

    request = urllib.request.Request(
        args.url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Inkbox-Signature": signature,
            "X-Inkbox-Request-Id": request_id,
            "X-Inkbox-Timestamp": timestamp,
        },
    )
    with urllib.request.urlopen(request) as resp:  # noqa: S310 — local dev tool
        print(f"{resp.status} {resp.read().decode()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
