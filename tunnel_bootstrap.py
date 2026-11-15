"""Bounded tunnel startup with reconciliation after caller cancellation."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any
from pathlib import Path
from weakref import WeakKeyDictionary

logger = logging.getLogger(__name__)
BOOTSTRAP_TIMEOUT_SECONDS = 210.0
_BOOTSTRAPS: WeakKeyDictionary = WeakKeyDictionary()


class TunnelCleanupError(RuntimeError):
    pass


def _close_listener(listener: Any) -> None:
    try:
        listener.close()
    except Exception:
        raise TunnelCleanupError("Tunnel startup cleanup failed") from None


class TunnelBootstrap:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cleanup: asyncio.Task[None] | None = None

    async def connect(
        self, connect: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        async with self._lock:
            if self._cleanup is not None:
                await asyncio.shield(self._cleanup)
                self._cleanup = None
            abandoned = threading.Event()

            def start():
                listener = connect(*args, **kwargs)
                if abandoned.is_set():
                    _close_listener(listener)
                    return None
                return listener

            attempt = asyncio.create_task(asyncio.to_thread(start))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(attempt), BOOTSTRAP_TIMEOUT_SECONDS
                )
            except (asyncio.CancelledError, TimeoutError):
                abandoned.set()
                self._cleanup = asyncio.create_task(self._discard(attempt))
                self._cleanup.add_done_callback(self._observe_cleanup)
                raise

    @staticmethod
    async def _discard(attempt: asyncio.Task[Any]) -> None:
        try:
            listener = await attempt
        except TunnelCleanupError:
            raise
        except Exception:
            logger.warning("Tunnel startup ended after its caller detached")
            return
        if listener is not None:
            await asyncio.to_thread(_close_listener, listener)

    @staticmethod
    def _observe_cleanup(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Tunnel startup cleanup failed; reconnect requires reconciliation"
            )


async def connect_tunnel(
    owner: Any, connect: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    state_dir = kwargs.get("state_dir")
    if state_dir is None:
        raise ValueError("Tunnel startup requires a state directory")
    bootstraps = _BOOTSTRAPS.setdefault(asyncio.get_running_loop(), {})
    key = str(Path(state_dir).expanduser().resolve())
    bootstrap = bootstraps.setdefault(key, TunnelBootstrap())
    owner._tunnel_bootstrap = bootstrap
    return await bootstrap.connect(connect, *args, **kwargs)
