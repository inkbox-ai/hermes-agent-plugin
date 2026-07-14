"""Extension registry for routes served on the Inkbox agent tunnel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List


@dataclass(frozen=True)
class HttpRoute:
    method: str
    path: str
    handler: Callable[[Any], Any]


_ROUTES: List[HttpRoute] = []


def register_http_route(method: str, path: str, handler: Callable[[Any], Any]) -> None:
    """Register a callback route on the Inkbox adapter's aiohttp application."""
    normalized_method = str(method or "").strip().upper()
    normalized_path = str(path or "").strip()
    if not normalized_method:
        raise ValueError("HTTP route method is required")
    if not normalized_path.startswith("/"):
        raise ValueError("HTTP route path must start with '/'")
    if not callable(handler):
        raise TypeError("HTTP route handler must be callable")
    for route in _ROUTES:
        if route.method == normalized_method and route.path == normalized_path:
            if route.handler is handler:
                return
            raise ValueError(
                f"HTTP route collision for {normalized_method} {normalized_path}"
            )
    _ROUTES.append(HttpRoute(normalized_method, normalized_path, handler))


def registered_http_routes() -> tuple[HttpRoute, ...]:
    """Return an immutable snapshot of companion-plugin routes."""
    return tuple(_ROUTES)
