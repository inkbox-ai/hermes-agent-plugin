#!/usr/bin/env python3
"""Exercise the actual voice driver's media WS using only loopback traffic."""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import socket
from unittest.mock import patch

import aiohttp
import uvicorn


async def check() -> None:
    driver_path = Path(__file__).resolve().parents[1] / 'live/voice_driver.py'
    # Import the real app, never main(): no SDK client, tunnel or phone call.
    with patch.dict(os.environ, {
        'REMOTE_INKBOX_API_KEY': 'offline-fixture-only',
        'VOICE_DRIVER_GREETING': 'Loopback greeting.',
        'VOICE_DRIVER_AUTO_STOP': 'false',
    }):
        spec = importlib.util.spec_from_file_location('voice_driver_ws_smoke', driver_path)
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    address, port = listener.getsockname()
    server = uvicorn.Server(uvicorn.Config(driver.app, log_level='error', lifespan='off'))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while not server.started:
            if task.done():
                await task
                raise AssertionError('voice fixture server exited before startup')
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError('voice fixture server did not start')
            await asyncio.sleep(.01)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
            async with client.get(f'http://{address}:{port}/health') as response:
                assert response.status == 200 and await response.json() == {'ok': True}
            async with client.ws_connect(f'http://{address}:{port}/phone/media/ws') as ws:
                # Both acceptance headers select the driver's real text media mode.
                assert ws._response.headers['x-use-inkbox-text-to-speech'] == 'true'
                assert ws._response.headers['x-use-inkbox-speech-to-text'] == 'true'
                await ws.send_json({'event': 'start', 'stream_id': 'loopback-fixture'})
                assert await asyncio.wait_for(ws.receive_json(), 5) == {
                    'event': 'text', 'delta': driver.GREETING,
                }
                assert await asyncio.wait_for(ws.receive_json(), 5) == {'event': 'text', 'done': True}
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
            listener.close()
    assert task.done() and listener.fileno() == -1
    print('voice driver WS smoke: health, upgrade, speech headers and greeting/done frames passed; server stopped')


if __name__ == '__main__':
    asyncio.run(check())
