"""Inbound-webhook source identification + signature verification.

Every request that reaches the plugin's ``/webhook`` endpoint is signed by
whoever sent it, but each source signs differently — a different header name,
different signed content, and a different algorithm — so there is no single
signature to check. This package turns that into a small registry:

* each source is a :class:`~.base.WebhookProvider` in its own module that knows
  how to (a) recognise its own requests from the headers and (b) verify their
  signature;
* :func:`~.base.match_provider` picks the provider for an incoming request by
  header presence, and the adapter then calls ``provider.verify(...)`` with
  that source's secret.

**Adding a source is drop-in:** put a new ``<name>.py`` in this package with a
``@register_provider`` class — :func:`_discover_providers` imports every module
here at startup, so its registration runs automatically with no central file to
edit. See ``skills/inkbox-webhook-providers``.
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import WebhookProvider, match_provider, register_provider

__all__ = ["WebhookProvider", "match_provider", "register_provider"]


def _discover_providers() -> None:
    """Import every provider module so its ``@register_provider`` runs.

    Walks this package's directory and imports each submodule except the core
    ``base`` module and private ``_``-prefixed helpers. Importing a provider
    module is what appends it to the registry.

    Returns:
        None
    """
    for info in pkgutil.iter_modules(__path__):
        if info.name == "base" or info.name.startswith("_"):
            continue
        # Fully-qualified name works in every import context (installed plugin
        # package or the flat local/test fallback).
        importlib.import_module(f"{__name__}.{info.name}")


_discover_providers()
