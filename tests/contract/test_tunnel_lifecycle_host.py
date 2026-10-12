"""A terminal tunnel must not trigger the host's last-platform process exit."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway import status as status_mod
from gateway.run import _write_runtime_status_quiet
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.run_startup import GatewayStartupMixin
from gateway.platforms.base import BasePlatformAdapter
from gateway.platform_registry import PlatformEntry, platform_registry

from inkbox_plugin import adapter as adapter_mod
from tests import test_tunnel_lifecycle as lifecycle_tests

tunnel_runtime = lifecycle_tests.runtime


@pytest.fixture(autouse=True)
def register_platform():
    platform_registry.register(PlatformEntry(
        name="inkbox", label="Inkbox", adapter_factory=adapter_mod.InkboxAdapter, check_fn=lambda: True,
    ))


class Host(GatewayStartupMixin, GatewayAdapterLifecycleMixin, GatewayShutdownMixin):
    def __init__(self):
        self.adapters = {}
        self._failed_platforms = {}
        self._running = True
        self.stop = AsyncMock()
        self._sync_voice_mode_state_to_adapter = Mock()
        self._bind_voice_input_callback = Mock()


async def channel_status():
    assert await status_mod.flush_runtime_status_async()
    return status_mod.read_runtime_status()["platforms"]["inkbox"]


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


@pytest.mark.parametrize("publication", ["startup", "reconnect"])
@pytest.mark.parametrize("terminal", ["closed", "superseded"])
def test_delayed_host_publication_preserves_terminal_tunnel(tunnel_runtime, monkeypatch, tmp_path, publication, terminal):
    monkeypatch.setattr(status_mod, "_get_runtime_status_path", lambda: tmp_path / "gateway-state.json")
    adapter, listener = tunnel_runtime.adapter, tunnel_runtime.listener
    monkeypatch.setattr(adapter, "_write_runtime_status_safe", BasePlatformAdapter._write_runtime_status_safe.__get__(adapter))
    host = Host()
    adapter.gateway_runner = host
    host._abort_startup_if_shutdown_requested = AsyncMock(return_value=False)
    host._startup_should_abort = lambda: False
    host._platform_lock_takeover_on_start = False
    host._platform_connect_timeout_secs = lambda *args, **kwargs: 0

    async def scenario():
        pending = None
        sibling_gate, connected = asyncio.Event(), asyncio.Event()
        original_connect = adapter.connect

        async def observed_connect(**kwargs):
            result = await original_connect(**kwargs)
            connected.set()
            return result

        monkeypatch.setattr(adapter, "connect", observed_connect)
        try:
            if publication == "startup":
                async def sibling_connect(**kwargs):
                    await sibling_gate.wait()
                    return True

                sibling = SimpleNamespace(platform=adapter_mod.Platform.DISCORD, config=adapter.config,
                                          connect=sibling_connect, send_path_degraded=False)
                pending = asyncio.create_task(host._start_connect_pending([
                    (adapter.platform, adapter.config, adapter), (sibling.platform, sibling.config, sibling),
                ]))
                await asyncio.wait_for(connected.wait(), 3)
                await lifecycle_tests.drain()
                assert not pending.done()
            else:
                assert await host._connect_adapter_with_timeout(adapter, adapter.platform, is_reconnect=True)

            await asyncio.to_thread(listener.finish, status=terminal)
            await asyncio.to_thread(adapter._tunnel_runtime_thread.join, 2)
            await lifecycle_tests.drain()
            assert (await channel_status())["state"] == "fatal"
            if publication == "startup":
                sibling_gate.set()
                raw = await pending
                count = await host._start_aggregate_connect_results(raw, [], [])
                assert count == 2  # Successful startup slots stay installed, not retried or torn down.
                assert host.adapters[sibling.platform] is sibling
            else:
                host._failed_platforms[adapter.platform] = {}
                host.delivery_router = SimpleNamespace(adapters={})
                host._schedule_planned_restart_replay = Mock()
                host._redeliver_failed_obligations_for_platform = AsyncMock()
                host._schedule_resume_pending_sessions = Mock()
                monkeypatch.setattr("gateway.channel_directory.build_channel_directory", AsyncMock())
                await host._install_reconnected_adapter(adapter.platform, adapter)

            snapshot = await channel_status()
            assert snapshot["state"] == "fatal"
            assert snapshot["error_code"] == ("tunnel_superseded" if terminal == "superseded" else "tunnel_stopped")
            assert snapshot["needs_attention"] is True and snapshot["retrying_since"] is None
            assert host.adapters[adapter.platform] is adapter
            assert adapter.has_fatal_error and not adapter.is_connected
            assert not host._failed_platforms and host._running
            host.stop.assert_not_called()
            assert not tunnel_runtime.notifications and len(tunnel_runtime.calls) == 1
            assert adapter._site is not None and (await adapter._handle_health(None)).status == 503
        finally:
            sibling_gate.set()
            if pending is not None:
                await pending
            await adapter.disconnect()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", [
    "transient", "no_fatal", "replacement", "shutdown", "disconnected", "other_platform", "other_profile", "explicit_fatal",
])
def test_status_guard_only_protects_current_primary_terminal_generation(monkeypatch, tmp_path, case):
    monkeypatch.setattr(adapter_mod, "_inkbox_state_path", lambda: tmp_path / "inkbox.json")
    adapter = adapter_mod.InkboxAdapter(adapter_mod.PlatformConfig(extra={
        "identity": "fixture", "api_key": "synthetic-key", "signing_key": "synthetic-signing-key",
    }))
    host = Host()
    host.adapters[adapter.platform] = adapter
    adapter.gateway_runner = host
    adapter._tunnel_startup_complete = True
    adapter._tunnel_generation = object()
    adapter._tunnel_terminal_reason = ("tunnel_stopped", "Tunnel stopped")
    adapter._fatal_error_message = "Tunnel stopped"
    adapter._fatal_error_code = "tunnel_stopped"
    adapter._fatal_error_retryable = False
    publish = Mock()
    host._update_platform_runtime_status = publish
    adapter._guard_host_tunnel_status()
    guarded = host._update_platform_runtime_status
    adapter._guard_host_tunnel_status()
    assert host._update_platform_runtime_status is guarded
    platform = "inkbox"
    status = {"platform_state": "retrying", "error_code": None, "error_message": "Recovering"}
    if case == "transient":
        adapter._tunnel_terminal_reason = None
    elif case == "no_fatal":
        adapter._fatal_error_message = None
    elif case == "replacement":
        import gc
        import weakref
        old = weakref.ref(adapter)
        adapter._tunnel_terminal_reason = ("tunnel_superseded", "Another client took over")
        host.adapters[adapter.platform] = SimpleNamespace(_tunnel_terminal_reason=None)
        del adapter
        gc.collect()
        assert old() is None  # The per-runner wrapper must not retain the replaced adapter.
    elif case == "shutdown":
        adapter._tunnel_generation = None
    elif case == "disconnected":
        status = {"platform_state": "disconnected", "error_code": None, "error_message": None}
    elif case == "other_platform":
        platform = "discord"
    elif case == "other_profile":
        platform = "secondary:inkbox"
    else:
        status = {"platform_state": "fatal", "error_code": "new_error", "error_message": "New error"}
    host._update_platform_runtime_status(platform, **status)
    publish.assert_called_once_with(platform, **status)
