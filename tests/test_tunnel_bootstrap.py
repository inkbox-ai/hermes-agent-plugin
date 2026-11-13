from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import tunnel_bootstrap as module


def test_startup_reuses_connect_arguments_and_result(tmp_path):
    async def run():
        listener = SimpleNamespace(close=Mock())
        connect = Mock(return_value=listener)
        owner = SimpleNamespace()
        result = await module.connect_tunnel(owner, connect, "client", name="sample", state_dir=tmp_path)
        assert result is listener
        connect.assert_called_once_with("client", name="sample", state_dir=tmp_path)
        listener.close.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("detach", ["cancel", "timeout"])
def test_detached_startup_closes_late_listener_before_reconnecting(monkeypatch, detach, tmp_path):
    async def run():
        started, release = threading.Event(), threading.Event()
        closed = threading.Event()
        calls = []
        first = SimpleNamespace(close=closed.set)
        second = SimpleNamespace(close=Mock())

        def connect(**_kwargs):
            calls.append(len(calls))
            if len(calls) == 1:
                started.set()
                assert release.wait(3)
                return first
            assert closed.is_set()
            return second

        owner = SimpleNamespace()
        if detach == "timeout":
            monkeypatch.setattr(module, "BOOTSTRAP_TIMEOUT_SECONDS", 0.02)
        attempt = asyncio.create_task(module.connect_tunnel(owner, connect, state_dir=tmp_path))
        assert await asyncio.to_thread(started.wait, 2)
        if detach == "cancel":
            attempt.cancel()
        try:
            with pytest.raises(asyncio.CancelledError if detach == "cancel" else TimeoutError):
                await attempt
            retry = asyncio.create_task(module.connect_tunnel(SimpleNamespace(), connect, state_dir=tmp_path))
            await asyncio.sleep(0)
            assert len(calls) == 1
        finally:
            release.set()
        assert await asyncio.wait_for(retry, 2) is second
        assert closed.is_set()
        assert len(calls) == 2
        second.close.assert_not_called()

    asyncio.run(run())


def test_failed_cleanup_blocks_new_attempt(tmp_path):
    async def run():
        started, release = threading.Event(), threading.Event()

        def connect(**_kwargs):
            started.set()
            assert release.wait(3)
            return SimpleNamespace(close=Mock(side_effect=RuntimeError("close failed")))

        owner = SimpleNamespace()
        attempt = asyncio.create_task(module.connect_tunnel(owner, connect, state_dir=tmp_path))
        assert await asyncio.to_thread(started.wait, 2)
        attempt.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await attempt
        finally:
            release.set()
        next_connect = Mock()
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await module.connect_tunnel(SimpleNamespace(), next_connect, state_dir=tmp_path)
        next_connect.assert_not_called()

    asyncio.run(run())
