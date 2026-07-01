"""Inbound-webhook source identification + signature verification.

Every request that reaches the plugin's ``/webhook`` endpoint is signed by
whoever sent it, but each source signs differently — a different header name,
different signed content, and a different algorithm — so there is no single
signature to check. This module turns that into a small registry:

* each source is a :class:`WebhookProvider` that knows how to (a) recognise
  its own requests from the headers and (b) verify their signature;
* :func:`match_provider` picks the provider for an incoming request by header
  presence, and the adapter then calls ``provider.verify(...)`` with that
  source's secret.

Only the Inkbox provider ships today. To onboard another source, add a
:class:`WebhookProvider` subclass and decorate it with
:func:`register_provider` — see ``skills/inkbox-webhook-providers``.
"""

from __future__ import annotations

from typing import List, Mapping, Optional, Type

try:
    # The SDK owns the canonical Inkbox HMAC scheme; the Inkbox provider reuses
    # it verbatim so the verification logic lives in exactly one place.
    from inkbox import verify_webhook
except ImportError:  # pragma: no cover - SDK is optional at import time
    verify_webhook = None  # type: ignore[assignment]


class WebhookProvider:
    """One inbound-webhook source (Inkbox, and future third parties).

    Subclasses set :attr:`name` + :attr:`provider_header` and implement
    :meth:`verify`. Register them with :func:`register_provider` so that
    :func:`match_provider` can route inbound requests to them.
    """

    #: Stable source id, surfaced to the agent as ``source=<name>``.
    name: str = ""
    #: Signature header that fingerprints this source. Sources that need more
    #: than one header to identify should override :meth:`matches` instead.
    provider_header: str = ""

    def matches(self, headers: Mapping[str, str]) -> bool:
        """Return whether an inbound request came from this source.

        Args:
            headers (Mapping[str, str]): The inbound request headers.

        Returns:
            bool: True when :attr:`provider_header` is present (compared
                case-insensitively, since HTTP header names are not case
                sensitive).
        """
        if not self.provider_header:
            return False
        wanted = self.provider_header.lower()
        return any(key.lower() == wanted for key in headers)

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        url: str,
        secret: str,
    ) -> bool:
        """Verify a request's signature against this source's scheme.

        Args:
            body (bytes): Raw request body, exactly as received (do not parse
                and re-serialize — most HMAC schemes sign the raw bytes).
            headers (Mapping[str, str]): The inbound request headers.
            url (str): The full request URL. Some schemes sign the URL and its
                params rather than the body, so it is always passed in.
            secret (str): This source's signing secret or verification key.

        Returns:
            bool: True iff the signature is present and authentic.
        """
        raise NotImplementedError


# Registered providers, checked in registration order by ``match_provider``.
_REGISTRY: List[WebhookProvider] = []


def register_provider(cls: Type[WebhookProvider]) -> Type[WebhookProvider]:
    """Class decorator that adds a provider to the match registry.

    Args:
        cls (Type[WebhookProvider]): The provider subclass to register. It is
            instantiated once (providers are stateless) and appended to the
            registry.

    Returns:
        Type[WebhookProvider]: The same class, unchanged, so the decorator is
            transparent to the class definition.
    """
    _REGISTRY.append(cls())
    return cls


def match_provider(headers: Mapping[str, str]) -> Optional[WebhookProvider]:
    """Return the first registered provider that recognises the request.

    Args:
        headers (Mapping[str, str]): The inbound request headers.

    Returns:
        Optional[WebhookProvider]: The matching provider, or None when no
            registered source claims the request (an unknown/unverifiable
            third party).
    """
    for provider in _REGISTRY:
        if provider.matches(headers):
            return provider
    return None


@register_provider
class InkboxProvider(WebhookProvider):
    """Inkbox's own events — inbound mail, text, iMessage, and calls.

    Inkbox stamps ``X-Inkbox-Signature`` as an HMAC-SHA256 over the request
    id, timestamp, and raw body using the org signing key. Verification is
    delegated to the SDK's ``verify_webhook`` so the scheme stays defined in
    one place.
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
