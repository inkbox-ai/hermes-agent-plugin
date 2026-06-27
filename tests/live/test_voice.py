"""Live voice-call suite — real phone calls, real model, transcript-verified.

Two scenarios, each run against a gateway booted in the matching speech mode (the
workflow sets that up and selects the scenario via VOICE_SCENARIO):

  * inbound_inkbox   — the driver calls the agent; the agent answers with Inkbox
                       STT/TTS and holds a turn.
  * outbound_realtime — the driver texts "call me"; the agent places a call back,
                       powered by the realtime API, and holds a turn.

A companion driver process (voice_driver.py) bridges the driver's side of the call
over an Inkbox tunnel and speaks one line. We then read the stored call transcript
and assert both parties spoke — proving the agent reached the caller out loud.
"""

from __future__ import annotations

import json
import os
import re
import time

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
REAL = os.environ.get("LIVE_REAL_MODEL") == "1"
SCENARIO = os.environ.get("VOICE_SCENARIO", "")
STATE_FILE = os.environ.get("VOICE_DRIVER_STATE", "/tmp/voice_driver_state.json")
TIMEOUT_S = float(os.environ.get("LIVE_VOICE_TIMEOUT", "220"))
POLL_EVERY_S = 6.0

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY and REAL),
    reason="voice suite: needs both keys + LIVE_REAL_MODEL=1",
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _client(key):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _driver_state() -> dict:
    with open(STATE_FILE) as fh:
        return json.load(fh)


def _aut_phone(aut) -> str:
    nums = aut.phone_numbers.list()
    assert nums, "AUT identity has no phone number"
    return nums[0].number


def _segments(remote, number_id, call_id):
    """Transcript segments for a call, split by who spoke."""
    segs = remote.transcripts.list(number_id, call_id)
    rem = [s for s in segs if (getattr(s, "party", "") or "").lower() == "remote" and (s.text or "").strip()]
    loc = [s for s in segs if (getattr(s, "party", "") or "").lower() == "local" and (s.text or "").strip()]
    return segs, rem, loc


def _wait_for_two_way_call(remote, number_id, call_id):
    """Block until the call transcript shows BOTH the agent and the driver spoke."""
    deadline = time.monotonic() + TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        try:
            _all, rem, loc = _segments(remote, number_id, call_id)
        except Exception as exc:  # transcripts may 404 until the call is set up
            last = f"transcripts not ready: {exc!r}"
            time.sleep(POLL_EVERY_S)
            continue
        if rem and loc:
            agent_said = " | ".join(s.text.strip() for s in rem)
            return agent_said  # the agent reached the caller out loud, in a two-way call
        last = f"segments so far: remote={len(rem)} local={len(loc)}"
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"agent never held a two-way call within {TIMEOUT_S:.0f}s ({last})")


def _aut_speech_mode(aut, direction, driver_number):
    """(use_inkbox_tts, use_inkbox_stt) of the agent's most recent answered call
    in `direction` with the driver. Tells Inkbox STT/TTS (True/True) from realtime
    (False/False), so each leg can prove it ran the speech path it claims."""
    num_id = str(aut.phone_numbers.list()[0].id)
    tail = _digits(driver_number)[-10:]
    answered = [c for c in aut.calls.list(num_id, limit=10)
                if (getattr(c, "direction", "") or "").lower() == direction
                and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == tail
                and c.use_inkbox_tts is not None]
    assert answered, f"no answered {direction} agent call with the driver found"
    c = answered[0]  # newest first
    return c.use_inkbox_tts, c.use_inkbox_stt


@pytest.mark.skipif(SCENARIO != "inbound_inkbox", reason="inbound Inkbox STT/TTS leg only")
def test_inbound_call_inkbox_tts_stt():
    """Driver calls the agent; the agent answers via Inkbox STT/TTS and replies."""
    st = _driver_state()
    remote, aut = _client(REMOTE_KEY), _client(AUT_KEY)
    aut_phone = _aut_phone(aut)

    # Place the call to the agent, handing Inkbox the driver's own media WS.
    call = remote.calls.place(
        from_number=st["number"], to_number=aut_phone, client_websocket_url=st["ws_url"],
    )
    agent_said = _wait_for_two_way_call(remote, st["number_id"], call.id)
    assert agent_said, "agent produced no speech on the inbound call"

    tts, stt = _aut_speech_mode(aut, "inbound", st["number"])
    assert tts and stt, f"inbound call should run Inkbox STT/TTS, got tts={tts} stt={stt}"


@pytest.mark.skipif(SCENARIO != "outbound_realtime", reason="outbound realtime leg only")
def test_outbound_call_realtime():
    """Driver texts 'call me'; the agent places a realtime-powered call and replies."""
    st = _driver_state()
    remote, aut = _client(REMOTE_KEY), _client(AUT_KEY)
    aut_phone = _aut_phone(aut)
    tail = _digits(aut_phone)[-10:]

    def _inbound_from_aut():
        return [c for c in remote.calls.list(st["number_id"], limit=30)
                if (getattr(c, "direction", "") or "").lower() == "inbound"
                and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == tail]

    before = {c.id for c in _inbound_from_aut()}
    remote.texts.send(st["number_id"], to=aut_phone, text="Please call me right now by phone — give me a ring.")

    # Wait for the agent to dial back, then verify the call transcript.
    deadline = time.monotonic() + TIMEOUT_S
    call_id = None
    while time.monotonic() < deadline:
        fresh = [c for c in _inbound_from_aut() if c.id not in before]
        if fresh:
            call_id = fresh[0].id
            break
        time.sleep(POLL_EVERY_S)
    assert call_id, f"agent never placed a call back within {TIMEOUT_S:.0f}s"

    agent_said = _wait_for_two_way_call(remote, st["number_id"], call_id)
    assert agent_said, "agent produced no speech on the outbound call"

    tts, stt = _aut_speech_mode(aut, "outbound", st["number"])
    assert tts is False and stt is False, \
        f"outbound call must be powered by the realtime API (Inkbox speech off), got tts={tts} stt={stt}"
