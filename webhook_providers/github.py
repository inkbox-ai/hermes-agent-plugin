"""GitHub webhook events — verified via ``X-Hub-Signature-256`` (HMAC-SHA256)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Mapping

from .base import WebhookProvider, register_provider

_HEADER = "X-Hub-Signature-256"


@register_provider
class GithubProvider(WebhookProvider):
    """Verifier for GitHub webhooks (e.g. a workflow-run failure forwarded here).

    GitHub signs the raw request body as an HMAC-SHA256 keyed by the webhook
    secret and sends it as ``X-Hub-Signature-256: sha256=<hex>``. The secret is
    read from ``INKBOX_WEBHOOK_SECRET_GITHUB`` (see ``adapter._provider_secret``).
    """

    name = "github"
    provider_header = _HEADER

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        url: str,
        secret: str,
    ) -> bool:
        # No configured secret → we cannot verify → fail closed.
        if not secret:
            return False
        # Header names are case-insensitive; find our signature header.
        sent = ""
        for key, value in headers.items():
            if key.lower() == _HEADER.lower():
                sent = value
                break
        if not sent.startswith("sha256="):
            return False
        # GitHub signs the raw body; ``url`` is unused for this scheme.
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        # Constant-time compare so a bad signature can't be timing-probed.
        return hmac.compare_digest(expected, sent.removeprefix("sha256="))
