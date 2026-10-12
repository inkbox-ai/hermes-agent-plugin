"""Exercise threaded transport status, startup, and independent gateway liveness."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from inkbox.tunnels.client._runtime import _TunnelAuthError
from inkbox.tunnels.exceptions import TunnelSupersededError

from inkbox_plugin import adapter as adapter_mod


class Listener:
    public_url = "https://agent.example"
    tunnel = SimpleNamespace(public_host="agent.example")

    def __init__(self):
        self.status = "connecting"
        self.on_status = lambda _: None
        self.stopped = threading.Event()
        self.error = None
        self.close_calls = 0

    @property
    def is_connected(self):
        return self.status == "connected"

    def emit(self, status):
        self.status = status
        self.on_status(status)

    def wait(self):
        assert self.stopped.wait(10), "test left a listener running"
        if self.error:
            raise self.error

    def finish(self, *, status="closed", error=None, emit=True):
        self.error = error
        if emit:
            self.emit(status)
        self.stopped.set()

    def close(self):
        self.close_calls += 1
        self.finish()


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter_mod, "_inkbox_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(adapter_mod, "_inkbox_tunnel_state_dir", lambda: tmp_path / "tunnel")
    monkeypatch.setattr(adapter_mod, "install_tunnel_tls_compatibility", lambda: None)
    monkeypatch.setattr(adapter_mod, "check_inkbox_requirements", lambda: True)
    adapter = adapter_mod.InkboxAdapter(adapter_mod.PlatformConfig(extra={
        "api_key": "synthetic-key", "identity": "synthetic-agent",
        "signing_key": "synthetic-signing-key", "voice_stack": "inkbox_voice_ai",
    }))
    adapter._port = 0
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda **_: True, raising=False)
    monkeypatch.setattr(adapter, "_release_platform_lock", lambda: None, raising=False)
    monkeypatch.setattr(adapter, "_patch_identity_objects", lambda: None)
    monkeypatch.setattr(adapter, "_start_companion", AsyncMock())
    monkeypatch.setattr(adapter, "_catch_up_hosted_call_completions", AsyncMock())
    monkeypatch.setattr(adapter, "_catch_up_a2a_tasks", AsyncMock())
    monkeypatch.setattr(adapter_mod, "Inkbox", lambda **_: SimpleNamespace(close=lambda: None))
    listener = Listener()
    calls = []
    statuses = []
    notifications = []

    def connect(_client, **kwargs):
        calls.append(kwargs)
        listener.on_status = kwargs["on_status"]
        listener.emit("connecting")
        return listener

    def write_status(context, **kwargs):
        statuses.append((threading.get_ident(), kwargs))

    async def on_fatal(failed):
        notifications.append(failed)

    monkeypatch.setattr(adapter_mod, "inkbox_tunnel_connect", connect)
    monkeypatch.setattr(adapter, "_write_runtime_status_safe", write_status)
    adapter.set_fatal_error_handler(on_fatal)
    return SimpleNamespace(adapter=adapter, listener=listener, calls=calls,
                           statuses=statuses, notifications=notifications)


async def drain():
    # Give callbacks scheduled from the SDK thread gateway-loop turns.
    for _ in range(5):
        await asyncio.sleep(0)


async def health(adapter):
    response = await adapter._handle_health(None)
    return response.status, json.loads(response.text)["status"]


def test_transient_recovery_uses_one_listener_and_gateway_loop(runtime):
    async def scenario():
        adapter, listener = runtime.adapter, runtime.listener
        assert await adapter.connect()
        assert await health(adapter) == (503, "reconnecting")
        for status, expected in [("connected", 200), ("reconnecting", 503), ("connected", 200)]:
            await asyncio.to_thread(listener.emit, status)
            await drain()
            assert (await health(adapter))[0] == expected
            assert runtime.statuses[-1][1]["platform_state"] == ("connected" if expected == 200 else "retrying")
        assert len(runtime.calls) == 1
        assert not runtime.notifications
        assert all(thread == threading.get_ident() for thread, _ in runtime.statuses)
        await adapter.disconnect()
        await drain()
        assert not runtime.notifications
        assert listener.close_calls == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["auth401", "auth403", "superseded", "runtime", "clean"])
def test_terminal_waiter_failure_is_visible_and_never_retries(runtime, failure):
    async def scenario():
        adapter, listener = runtime.adapter, runtime.listener
        assert await adapter.connect()
        await asyncio.to_thread(listener.emit, "connected")
        await drain()
        assert await health(adapter) == (200, "ok")
        error = {
            "auth401": _TunnelAuthError("/_system/hello returned 401"),
            "auth403": _TunnelAuthError("/_system/hello returned 403"),
            "superseded": TunnelSupersededError("replaced"),
            "runtime": RuntimeError("synthetic private error detail"),
            "clean": None,
        }[failure]
        await asyncio.to_thread(listener.finish, status="superseded" if failure == "superseded" else "closed",
                                error=error, emit=failure != "clean")
        await asyncio.to_thread(adapter._tunnel_runtime_thread.join, 2)
        await drain()
        assert await health(adapter) == (503, "unavailable")
        assert not adapter.is_connected
        assert adapter.has_fatal_error and not adapter.fatal_error_retryable
        assert adapter.fatal_error_code == ("tunnel_superseded" if failure == "superseded" else "tunnel_stopped")
        assert not runtime.notifications
        assert len(runtime.calls) == 1
        assert "private error" not in str(runtime.statuses)
        # Delayed connected status cannot resurrect a terminated generation.
        listener.emit("connected")
        await drain()
        assert await health(adapter) == (503, "unavailable")
        await adapter.disconnect()
    asyncio.run(scenario())


@pytest.mark.parametrize("during", ["reconcile", "companion", "catchup"])
def test_terminal_exit_during_startup_cannot_be_overwritten(runtime, monkeypatch, during):
    async def stop_in_startup():
        await asyncio.to_thread(runtime.listener.finish)
        await drain()

    if during == "reconcile":
        monkeypatch.setattr(runtime.adapter, "_patch_identity_objects", runtime.listener.finish)
    elif during == "companion":
        monkeypatch.setattr(runtime.adapter, "_start_companion", stop_in_startup)
    else:
        monkeypatch.setattr(runtime.adapter, "_catch_up_a2a_tasks", stop_in_startup)

    async def scenario():
        assert not await runtime.adapter.connect()
        await drain()
        assert not runtime.adapter.is_connected
        assert runtime.adapter.has_fatal_error and not runtime.adapter.fatal_error_retryable
        # Failed connect is returned to the host; no concurrent teardown callback.
        assert not runtime.notifications
        assert runtime.listener.close_calls == 1
        assert runtime.adapter._site is None
        assert not any(status["platform_state"] == "connected" for _, status in runtime.statuses)
    asyncio.run(scenario())


def test_connected_transport_does_not_publish_readiness_before_catchup(runtime, monkeypatch):
    async def check_catchup():
        await asyncio.to_thread(runtime.listener.emit, "connected")
        await drain()
        assert await health(runtime.adapter) == (503, "starting")
        assert not any(status["platform_state"] == "connected" for _, status in runtime.statuses)
    monkeypatch.setattr(runtime.adapter, "_catch_up_a2a_tasks", check_catchup)

    async def scenario():
        assert await runtime.adapter.connect()
        assert await health(runtime.adapter) == (200, "ok")
        await runtime.adapter.disconnect()
    asyncio.run(scenario())


def test_stale_generation_and_shutdown_completion_cannot_change_status(runtime):
    async def scenario():
        adapter, listener = runtime.adapter, runtime.listener
        assert await adapter.connect()
        old_callback = listener.on_status
        await adapter.disconnect()
        await drain()
        assert not adapter.has_fatal_error
        listener.stopped.clear()
        assert await adapter.connect()
        await asyncio.to_thread(listener.emit, "connected")
        await drain()
        previous = len(runtime.statuses)
        await asyncio.to_thread(old_callback, "superseded")
        await asyncio.to_thread(old_callback, "closed")
        await drain()
        assert len(runtime.statuses) == previous
        assert await health(adapter) == (200, "ok")
        assert not runtime.notifications
        await adapter.disconnect()
    asyncio.run(scenario())


def test_shutdown_after_terminal_status_does_not_request_recovery(runtime):
    async def scenario():
        adapter = runtime.adapter
        assert await adapter.connect()
        adapter._handle_tunnel_exit(adapter._tunnel_generation)
        await adapter.disconnect()
        await drain()
        assert not runtime.notifications
        assert len(runtime.calls) == 1
    asyncio.run(scenario())


def test_terminal_exit_keeps_local_http_readiness_available(runtime):
    async def scenario():
        from aiohttp import ClientSession

        adapter = runtime.adapter
        assert await adapter.connect()
        await asyncio.to_thread(runtime.listener.finish)
        await asyncio.to_thread(adapter._tunnel_runtime_thread.join, 2)
        await drain()
        port = adapter._runner.addresses[0][1]
        async with ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/health") as response:
                assert response.status == 503
                assert (await response.json())["status"] == "unavailable"
        assert not runtime.notifications
        assert adapter._site is not None and adapter._tunnel is runtime.listener
        assert len(runtime.calls) == 1
        assert runtime.listener.close_calls == 0
        await adapter.disconnect()
        assert runtime.listener.close_calls == 1
    asyncio.run(scenario())


def test_external_public_url_readiness_keeps_existing_behavior(runtime):
    async def scenario():
        adapter = runtime.adapter
        adapter._public_url_override = "https://external.example"
        assert await adapter.connect()
        assert await health(adapter) == (200, "ok")
        assert not runtime.calls
        await adapter.disconnect()
        assert (await health(adapter))[0] == 503
    asyncio.run(scenario())


def test_cancelled_startup_cleanup_does_not_report_terminal_failure(runtime, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        async def catchup():
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(runtime.adapter, "_catch_up_a2a_tasks", catchup)
        connect = asyncio.create_task(runtime.adapter.connect())
        await asyncio.wait_for(started.wait(), 3)
        connect.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connect
        await runtime.adapter.disconnect()
        await drain()
        assert not runtime.adapter.has_fatal_error
        assert not runtime.notifications
        assert runtime.listener.close_calls == 1
        assert len(runtime.calls) == 1
    asyncio.run(scenario())
