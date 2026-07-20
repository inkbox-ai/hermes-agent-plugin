"""Core webhook-provider machinery: the base class and the registry.

Provider modules import :class:`WebhookProvider` and :func:`register_provider`
from here; the package ``__init__`` auto-imports every provider module at
startup so their registration runs. See ``skills/inkbox-webhook-providers``.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Type


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
    #: Optional skill auto-loaded whenever this verified provider wakes Hermes.
    skill: str | list[str] | None = None

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

    def event_key(
        self,
        *,
        envelope: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> str:
        """Return a stable delivery id for providers without a request-id header.

        The adapter uses ``X-Inkbox-Request-Id`` for native Inkbox deliveries.
        Third-party providers can override this hook to expose their equivalent
        idempotency key. Returning an empty string leaves deduplication to the
        event handler.
        """
        del envelope, headers
        return ""


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

    Raises:
        ValueError: If another registered provider already claims the same
            ``provider_header`` — match order is first-match-wins, so an
            overlapping header would be ambiguous. Fail fast at import.
    """
    provider = cls()
    header = (provider.provider_header or "").lower()
    if header:
        for existing in _REGISTRY:
            if (existing.provider_header or "").lower() == header:
                raise ValueError(
                    f"Webhook provider header collision: {cls.__name__} and "
                    f"{type(existing).__name__} both claim {provider.provider_header!r}."
                )
    _REGISTRY.append(provider)
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
