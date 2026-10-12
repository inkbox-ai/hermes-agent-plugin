"""A terminal tunnel must not trigger the host's last-platform process exit."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway import status as status_mod
from gateway.run import _write_runtime_status_quiet
from gateway.run_adapters import GatewayAdapterLifecycleMixin

from inkbox_plugin import adapter as adapter_mod


@pytest.mark.parametrize("independent_adapter", [False, True])
def test_terminal_tunnel_preserves_host_liveness_and_fatal_snapshot(monkeypatch, tmp_path, independent_adapter):
    monkeypatch.setattr(status_mod, "_get_runtime_status_path", lambda: tmp_path / "gateway-state.json")
    monkeypatch.setattr(adapter_mod, "_inkbox_state_path", lambda: tmp_path / "inkbox.json")
    adapter = adapter_mod.InkboxAdapter(adapter_mod.PlatformConfig(extra={
        "identity": "fixture-agent", "api_key": "synthetic-key", "signing_key": "synthetic-signing-key",
    }))
    adapter._running = True
    adapter._tunnel_startup_complete = True
    adapter._tunnel_generation = object()
    adapter._tunnel = SimpleNamespace(is_connected=False, status="closed")
    host = GatewayAdapterLifecycleMixin()
    host._running = True
    host.adapters = {adapter.platform: adapter}
    other = object()
    if independent_adapter:
        host.adapters["independent"] = other
    host._failed_platforms = {}
    host.config = SimpleNamespace(on_all_adapters_down="stay_alive")
    host.stop = AsyncMock()
    fatal_handler = AsyncMock(side_effect=host._handle_adapter_fatal_error)
    adapter.set_fatal_error_handler(fatal_handler)

    async def scenario():
        adapter._handle_tunnel_status(adapter._tunnel_generation, "closed")
        adapter._handle_tunnel_exit(adapter._tunnel_generation)
        await asyncio.sleep(0)
        assert not adapter.is_connected
        assert adapter.has_fatal_error and not adapter.fatal_error_retryable
        assert (await adapter._handle_health(None)).status == 503
        fatal_handler.assert_not_called()
        host.stop.assert_not_called()
        assert host._running and not host._failed_platforms
        assert host.adapters[adapter.platform] is adapter
        if independent_adapter:
            assert host.adapters["independent"] is other

        # The host heartbeat merges process status, not the retained adapter map.
        _write_runtime_status_quiet(gateway_state="running", active_agents=1)
        assert await status_mod.flush_runtime_status_async()
        snapshot = status_mod.read_runtime_status()
        assert snapshot["gateway_state"] == "running"
        assert snapshot["active_agents"] == 1
        channel = snapshot["platforms"][adapter.platform.value]
        assert channel["state"] == "fatal"
        assert channel["error_code"] == "tunnel_stopped"

    asyncio.run(scenario())
