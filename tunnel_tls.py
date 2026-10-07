"""Bounded compatibility for SDK tunnel TLS and native platform verifiers."""
from __future__ import annotations

import hashlib
import inspect
import logging
import ssl
import textwrap
import threading
from importlib.metadata import version

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_VERSIONS = frozenset({'0.7.11', '0.7.12', '0.7.13', '0.7.14', '0.7.15'})
# Exact published helper source, including its empty-store fallback contract.
_FACTORY_SHA256 = '07bde8ed2c82e51f22afb38dbfbd6f8195a160223499d4263a5c9d90054a4eb9'


def _verify_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        stats = ctx.cert_store_stats()
    except NotImplementedError:
        # Platform verifiers need not expose their roots to OpenSSL introspection.
        # Unknown is not empty: retain this exact verifier and all its trust roots.
        return ctx
    if stats.get('x509_ca', 0) == 0:
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass  # Preserve the published SDK's best-effort empty-store fallback.
    return ctx


def install_tunnel_tls_compatibility() -> bool:
    """Adapt only the inspected SDK factories, never SSL globals or foreign hooks.

    Remove when the minimum supported SDK handles unsupported cert_store_stats.
    Unknown SDK implementations retain their own behavior, not a guessed adapter.
    """
    with _LOCK:
        if version('inkbox') not in _VERSIONS:
            return _unchanged()
        try:
            from inkbox.tunnels.client import _runtime, _tls
            factory = _tls.create_default_verify_context
            runtime_factory = _runtime.create_default_verify_context
        except (ImportError, AttributeError):
            return _unchanged()
        if factory is _verify_context and runtime_factory is _verify_context:
            return True
        if factory is runtime_factory and inspect.isfunction(factory):
            try:
                source = textwrap.dedent(inspect.getsource(factory)).strip()
                supported = (
                    factory.__module__ == _tls.__name__
                    and hashlib.sha256(source.encode()).hexdigest() == _FACTORY_SHA256
                )
            except (OSError, TypeError):
                supported = False
        else:
            supported = False
        if not supported:
            return _unchanged()
        # The runtime imports by value; websocket upstreams import from _tls lazily.
        # Both still refer to the exact original helper; do not replace other hooks.
        _tls.create_default_verify_context = _verify_context
        _runtime.create_default_verify_context = _verify_context
        return True


def _unchanged() -> bool:
    logger.info('Tunnel TLS compatibility adapter not applied: unrecognized SDK factory; SDK TLS behavior unchanged')
    return False
