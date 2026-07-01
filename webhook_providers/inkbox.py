"""Inkbox's own events — inbound mail, text, iMessage, and calls."""

from __future__ import annotations

from typing import Mapping

from .base import WebhookProvider, register_provider

try:
    # Absolute import → the top-level Inkbox SDK, not this sibling module. The
    # SDK owns the canonical Inkbox HMAC scheme, so we reuse it verbatim and
    # keep the verification logic defined in exactly one place.
    from inkbox import verify_webhook
except ImportError:  # pragma: no cover - SDK is optional at import time
    verify_webhook = None  # type: ignore[assignment]


@register_provider
class InkboxProvider(WebhookProvider):
    """Verifier for events Inkbox itself emits.

    Inkbox stamps ``X-Inkbox-Signature`` as an HMAC-SHA256 over the request
    id, timestamp, and raw body using the org signing key.
    """

    name = "inkbox"
    provider_header = "X-Inkbox-Signature"

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        url: str,
        secret: str,
    ) -> bool:
        # No SDK installed means we cannot verify — fail closed.
        if verify_webhook is None:
            return False
        # Inkbox signs the raw body; ``url`` is unused for this scheme.
        return verify_webhook(payload=body, headers=headers, secret=secret)
