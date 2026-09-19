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


@pytest.mark.parametrize("peer_at, expected_retry", [(None, 159.0), (158.0, 164.0)])
def test_long_request_does_not_requeue_before_playback_and_peer_quiet(
    driver, monkeypatch, peer_at, expected_retry,
):
    now = 100.0
    observed_state = None
    original_wait = driver._wait_for_greeting

    async def wait_for_greeting(state):
        nonlocal observed_state
        observed_state = state
        return await original_wait(state)

    async def sleep(delay):
        nonlocal now
        now += delay
        await asyncio.sleep(0)

    async def wait_for(awaitable, timeout):
        nonlocal now
        awaitable.close()
        now += timeout
        if now == peer_at:
            observed_state["last_heard"] = now
        await asyncio.sleep(0)
        raise asyncio.TimeoutError

    monkeypatch.setattr(driver, "_wait_for_greeting", wait_for_greeting)
    monkeypatch.setattr(driver, "LINE", " ".join(["word"] * 80))
    monkeypatch.setattr(driver, "SPEAK_AFTER_S", 5)
    monkeypatch.setattr(driver, "QUIET_GAP_S", 6)
    monkeypatch.setattr(driver, "REASK_EVERY_S", 20)
    monkeypatch.setattr(driver, "LISTEN_S", 70)
    monkeypatch.setattr(driver, "MAX_REASKS", 1)
    loop = SimpleNamespace(time=lambda: now)
    monkeypatch.setattr(driver, "asyncio", SimpleNamespace(
        get_event_loop=lambda: loop, get_running_loop=lambda: loop,
        sleep=sleep, wait_for=wait_for, Event=asyncio.Event,
        create_task=asyncio.create_task, TimeoutError=asyncio.TimeoutError,
        CancelledError=asyncio.CancelledError,
    ))

    async def run():
        class Socket:
            client_state = "disconnected"

            def __init__(self):
                self.started = False
                self.stopped = asyncio.Event()
                self.spoken = []

            async def accept(self, **_kwargs):
                pass

            async def send_text(self, raw):
                event = json.loads(raw)
                if "delta" in event:
                    self.spoken.append((now, event["delta"]))
                if event["event"] == "stop":
                    self.stopped.set()

            async def receive_text(self):
                if not self.started:
                    self.started = True
                    return json.dumps({"event": "start"})
                await self.stopped.wait()
                return json.dumps({"event": "stop"})

        socket = Socket()
        await driver.phone_media_ws(socket)
        return socket.spoken

    spoken = asyncio.run(run())
    assert spoken == [
        (100.0, driver.GREETING),
        (105.0, driver.LINE),
        (expected_retry, driver.LINE),
    ]


def test_required_peer_greeting_has_bounded_wait(driver, monkeypatch):
    state = {"last_heard": 0.0}
    now, _pauses = _clock(driver, monkeypatch, state)
    monkeypatch.setattr(driver, "WAIT_FOR_PEER", True)

    assert not asyncio.run(driver._wait_for_greeting(state))
    assert now() == 130


@pytest.mark.parametrize(
    "test_owns_hangup, external_stop, expected_stops",
    [(False, 130, [120.0]), (True, 130, []), (True, None, [296.0])],
)
def test_contact_peer_waits_for_late_greeting_and_test_owned_completion(
    driver, monkeypatch, test_owns_hangup, external_stop, expected_stops,
):
    now = 100.0
    pending = [
        (107, {"event": "transcript", "text": "Hi", "is_final": False}),
        (110, {"event": "transcript", "text": "Hi, caller", "is_final": True}),
        # An incomplete email-like answer must not stop a test-owned call.
        (120, {"event": "transcript", "text": "example", "is_final": True}),
    ]
    if external_stop is not None:
        pending.append((external_stop, {"event": "stop"}))
    incoming = None

    async def advance(delay):
        nonlocal now
        target = now + delay
        while pending and pending[0][0] <= target:
            now, event = pending.pop(0)
            incoming.put_nowait(json.dumps(event))
            await asyncio.sleep(0)
        now = target
        await asyncio.sleep(0)

    async def wait_for(awaitable, timeout):
        awaitable.close()
        await advance(timeout)
        raise asyncio.TimeoutError

    loop = SimpleNamespace(time=lambda: now)
    monkeypatch.setattr(driver, "asyncio", SimpleNamespace(
        get_event_loop=lambda: loop, get_running_loop=lambda: loop,
        sleep=advance, wait_for=wait_for, Event=asyncio.Event,
        create_task=asyncio.create_task, TimeoutError=asyncio.TimeoutError,
    ))
    monkeypatch.setattr(driver, "WAIT_FOR_PEER", True)
    monkeypatch.setattr(driver, "TEST_OWNS_HANGUP", test_owns_hangup)
    monkeypatch.setattr(driver, "SPEAK_AFTER_S", 5)
    monkeypatch.setattr(driver, "QUIET_GAP_S", 6)
    monkeypatch.setattr(driver, "LISTEN_S", 180)
    monkeypatch.setattr(driver, "REASK_EVERY_S", 0)

    async def run():
        nonlocal incoming
        incoming = asyncio.Queue()
        incoming.put_nowait(json.dumps({"event": "start"}))

        class Socket:
            client_state = "disconnected"

            def __init__(self):
                self.sent = []

            async def accept(self, **_kwargs):
                pass

            async def send_text(self, raw):
                event = json.loads(raw)
                self.sent.append((now, event))
                if event["event"] == "stop":
                    incoming.put_nowait(json.dumps({"event": "stop"}))

            async def receive_text(self):
                return await incoming.get()

        socket = Socket()
        await driver.phone_media_ws(socket)
        return socket.sent

    sent = asyncio.run(run())
    assert [(when, event["delta"]) for when, event in sent if "delta" in event] == [
        (100.0, driver.GREETING), (116.0, driver.LINE),
    ]
    stops = [when for when, event in sent if event["event"] == "stop"]
    assert stops == expected_stops
