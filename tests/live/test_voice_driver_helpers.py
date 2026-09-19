"""Offline regressions for the live peer's speech turn-taking."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def driver(monkeypatch):
    # The turn scheduler needs no HTTP server or API connection.
    class App:
        def get(self, _path):
            return lambda function: function

        websocket = get

    with monkeypatch.context() as imports:
        imports.setenv("REMOTE_INKBOX_API_KEY", "synthetic-driver-key")
        imports.delenv("VOICE_DRIVER_LINE_FILE", raising=False)
        imports.setitem(sys.modules, "uvicorn", SimpleNamespace())
        imports.setitem(sys.modules, "fastapi", SimpleNamespace(FastAPI=App, WebSocket=object))
        imports.setitem(sys.modules, "starlette.websockets", SimpleNamespace(
            WebSocketState=SimpleNamespace(DISCONNECTED="disconnected"),
        ))
        imports.setitem(sys.modules, "inkbox", SimpleNamespace(Inkbox=object))
        imports.setitem(sys.modules, "inkbox.tunnels.client", SimpleNamespace(connect=None))
        path = Path(__file__).with_name("voice_driver.py")
        spec = importlib.util.spec_from_file_location("live_voice_driver_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _clock(driver, monkeypatch, state, utterances=(), continuous=False):
    now = 100.0
    events = iter(utterances)
    next_event = next(events, None)
    pauses = []

    async def sleep(delay):
        nonlocal now, next_event
        pauses.append(delay)
        now += delay
        while next_event is not None and next_event <= now:
            state["last_heard"] = next_event
            next_event = next(events, None)
        if continuous:
            state["last_heard"] = now

    monkeypatch.setattr(driver, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: now), sleep=sleep,
    ))
    monkeypatch.setattr(driver, "SPEAK_AFTER_S", 5)
    monkeypatch.setattr(driver, "QUIET_GAP_S", 6)
    return lambda: now, pauses


def test_first_request_waits_for_multisentence_greeting(driver, monkeypatch):
    state = {"last_heard": 0.0}
    now, pauses = _clock(driver, monkeypatch, state, utterances=[104, 108, 112])

    assert asyncio.run(driver._wait_for_greeting(state))
    assert now() == 118
    assert pauses[0] == 5
    assert now() - state["last_heard"] == 6


def test_silent_peer_gets_request_without_extra_greeting_wait(driver, monkeypatch):
    state = {"last_heard": 0.0}
    now, pauses = _clock(driver, monkeypatch, state)

    assert asyncio.run(driver._wait_for_greeting(state))
    assert now() == 105
    assert pauses == [5]


def test_continuous_peer_speech_has_bounded_wait(driver, monkeypatch):
    state = {"last_heard": 0.0}
    now, _pauses = _clock(driver, monkeypatch, state, continuous=True)

    assert not asyncio.run(driver._wait_for_greeting(state))
    assert now() == 130


def test_partial_transcript_extends_quiet_gate_before_final_transcript(driver, monkeypatch):
    observed = {}

    async def run():
        greeting_started = asyncio.Event()
        partial_seen = asyncio.Event()
        state_seen = None

        async def wait_for_greeting(state):
            nonlocal state_seen
            state_seen = state
            greeting_started.set()
            await partial_seen.wait()
            return False

        monkeypatch.setattr(driver, "_wait_for_greeting", wait_for_greeting)

        class Socket:
            client_state = "disconnected"

            def __init__(self):
                self.received = 0
                self.sent = []
                self.stopped = asyncio.Event()

            async def accept(self, **_kwargs):
                pass

            async def send_text(self, raw):
                message = json.loads(raw)
                self.sent.append(message)
                if message.get("event") == "stop":
                    self.stopped.set()

            async def receive_text(self):
                self.received += 1
                if self.received == 1:
                    return json.dumps({"event": "start"})
                if self.received == 2:
                    await greeting_started.wait()
                    return json.dumps({"event": "transcript", "text": "Still speaking", "is_final": False})
                observed["partial_activity"] = state_seen["last_heard"]
                partial_seen.set()
                await self.stopped.wait()
                return json.dumps({"event": "stop"})

        socket = Socket()
        await driver.phone_media_ws(socket)
        observed["utterances"] = [message["delta"] for message in socket.sent if "delta" in message]
        observed["stopped"] = socket.stopped.is_set()

    asyncio.run(run())
    assert observed["partial_activity"] > 0
    assert observed["utterances"] == [driver.GREETING]
    assert observed["stopped"]
