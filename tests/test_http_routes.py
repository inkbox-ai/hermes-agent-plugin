import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import http_routes


@pytest.fixture(autouse=True)
def clear_routes():
    previous = list(http_routes._ROUTES)
    http_routes._ROUTES.clear()
    yield
    http_routes._ROUTES[:] = previous


def test_register_http_route_normalizes_and_lists():
    async def handler(request):
        return request

    http_routes.register_http_route("get", "/oauth/callback", handler)
    route = http_routes.registered_http_routes()[0]
    assert (route.method, route.path, route.handler) == ("GET", "/oauth/callback", handler)


def test_register_http_route_rejects_collisions():
    async def first(request):
        return request

    async def second(request):
        return request

    http_routes.register_http_route("GET", "/oauth/callback", first)
    with pytest.raises(ValueError, match="route collision"):
        http_routes.register_http_route("GET", "/oauth/callback", second)


def test_register_http_route_requires_absolute_path():
    with pytest.raises(ValueError, match="start with"):
        http_routes.register_http_route("GET", "oauth/callback", lambda request: request)
