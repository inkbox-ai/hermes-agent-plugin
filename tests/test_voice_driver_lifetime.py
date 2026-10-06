"""Offline checks that test-owned voice calls outlive the scripted peer turn."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _driver(monkeypatch, auto_stop):
    class App:
        def get(self, _path):
            return lambda function: function

        websocket = get

    with monkeypatch.context() as imports:
        imports.setenv("REMOTE_INKBOX_API_KEY", "synthetic-driver-key")
        imports.delenv("VOICE_DRIVER_LINE_FILE", raising=False)
        if auto_stop is None:
            imports.delenv("VOICE_DRIVER_AUTO_STOP", raising=False)
        else:
            imports.setenv("VOICE_DRIVER_AUTO_STOP", auto_stop)
        imports.setitem(sys.modules, "uvicorn", SimpleNamespace())
        imports.setitem(sys.modules, "fastapi", SimpleNamespace(FastAPI=App, WebSocket=object))
        imports.setitem(sys.modules, "starlette.websockets", SimpleNamespace(
            WebSocketState=SimpleNamespace(DISCONNECTED="disconnected"),
        ))
        imports.setitem(sys.modules, "inkbox", SimpleNamespace(Inkbox=object))
        imports.setitem(sys.modules, "inkbox.tunnels.client", SimpleNamespace(connect=None))
        path = Path(__file__).parent / "live" / "voice_driver.py"
        spec = importlib.util.spec_from_file_location("voice_driver_lifetime_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("auto_stop", [None, "false", "true"])
@pytest.mark.parametrize("mode", ["listen_expired", "answer", "greeting_deadline"])
def test_driver_keeps_media_open_until_test_hangup(monkeypatch, auto_stop, mode):
    driver = _driver(monkeypatch, auto_stop)

    monkeypatch.setattr(driver, "SPEAK_AFTER_S", 0)
    monkeypatch.setattr(driver, "LISTEN_S", 180 if mode == "answer" else 0)
    monkeypatch.setattr(driver, "REASK_EVERY_S", 0)
    monkeypatch.setattr(driver, "ANSWER_SETTLE_S", 0)
    monkeypatch.setattr(driver, "TEST_OWNS_HANGUP", False)

    async def greeting(_state):
        return mode != "greeting_deadline"

    monkeypatch.setattr(driver, "_wait_for_greeting", greeting)

    async def run():
        incoming = asyncio.Queue()
        incoming.put_nowait(json.dumps({"event": "start"}))
        turns = []
        turn_started = asyncio.Event()
        create_task = asyncio.create_task

        def capture_turn(coro, **kwargs):
            task = create_task(coro, **kwargs)
            turns.append(task)
            turn_started.set()
            return task

        monkeypatch.setattr(asyncio, "create_task", capture_turn)

        class Socket:
            client_state = "connected"

            def __init__(self):
                self.sent = []
                self.closed = False

            async def accept(self, **_kwargs):
                pass

            async def send_text(self, raw):
                event = json.loads(raw)
                self.sent.append(event)
                if event.get("delta") == driver.LINE and mode == "answer":
                    incoming.put_nowait(json.dumps({
                        "event": "transcript", "text": "person@example.com", "is_final": True,
                    }))

            async def receive_text(self):
                return await incoming.get()

            async def close(self):
                self.closed = True
                self.client_state = "disconnected"

        socket = Socket()
        handler = create_task(driver.phone_media_ws(socket))
        try:
            await asyncio.wait_for(turn_started.wait(), timeout=1)
            await asyncio.wait_for(asyncio.gather(*turns), timeout=1)
            # The actual scripted turn has finished (including the listen/answer
            # gate), but its return must not close the enclosing media handler.
            assert all(task.done() for task in turns)
            assert not handler.done()
            assert not socket.closed
            stops = [event for event in socket.sent if event["event"] == "stop"]
            assert len(stops) == (0 if auto_stop == "false" else 1)
            expected = [driver.GREETING]
            if mode != "greeting_deadline":
                expected.append(driver.LINE)
            assert [event["delta"] for event in socket.sent if "delta" in event] == expected
            incoming.put_nowait(json.dumps({"event": "stop", "reason": "test cleanup"}))
            await asyncio.wait_for(handler, timeout=1)
            assert socket.closed
        finally:
            handler.cancel()
            await asyncio.gather(handler, *turns, return_exceptions=True)

    asyncio.run(run())
