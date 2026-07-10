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
import uuid

import pytest


# The agent answers a call request by dialing back, not by texting, so these
# driver→AUT SMS never get an SMS reply to reset the server's conversation
# cadence. Two identical no-reply sends to the same number trip the
# duplicate_body rule (422), so every call request must carry a fresh body.
_CALL_ME_PHRASINGS = (
    "Please call me right now by phone — give me a ring.",
    "Can you ring me on the phone right now?",
    "Give me a call on my number now, please.",
    "Please phone me right away — I'd rather talk than text.",
)


def _call_me_text() -> str:
    """A fresh call-request body each send (rotating phrasing + unique ref)."""
    phrasing = _CALL_ME_PHRASINGS[uuid.uuid4().int % len(_CALL_ME_PHRASINGS)]
    return f"{phrasing} (ref {uuid.uuid4().hex[:6]})"

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
    # Identity-centered transcript read (SDK 0.4.15+); number_id is vestigial.
    segs = remote.calls.transcripts(call_id)
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
    tail = _digits(driver_number)[-10:]
    answered = [c for c in aut.calls.list(limit=10)
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


# Fixed identifiers for the mid-call contact-lookup leg. Fixed (not uuid) so the
# workflow can bake the matching question into VOICE_DRIVER_LINE; the test seeds
# and deletes the card around the call. The name must survive TWO audio hops
# (driver TTS → realtime ASR), so it has to be phonetically ordinary — an
# invented surname came back as "Miracle Zibberwood" and the lookup rightly
# found nothing. The assert still strips spaces before matching.
LOOKUP_CONTACT_GIVEN = "Olivia"
LOOKUP_CONTACT_FAMILY = "Parker"
LOOKUP_CONTACT_EMAIL = "olivia.parker.livetest@example.com"
GATEWAY_LOG = os.environ.get("GATEWAY_LOG", "")


def _gateway_log_text() -> str:
    """All gateway log content we can find: stdout capture + hermes log files."""
    from pathlib import Path

    paths = [Path(GATEWAY_LOG)] if GATEWAY_LOG else []
    hermes_home = os.environ.get("HERMES_HOME", "")
    if hermes_home:
        paths.extend(sorted(Path(hermes_home, "logs").glob("*")))
    chunks = []
    for path in paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _delete_contacts_by_email(client, email: str) -> None:
    for contact in client.contacts.lookup(email=email) or []:
        contact_id = str(getattr(contact, "id", "") or "")
        if contact_id:
            client.contacts.delete(contact_id)


def _ensure_driver_is_a_known_contact(aut, driver_number: str) -> None:
    """Seed the driver as a contact so the caller counts as recognized.

    The realtime prompt forbids reciting third-party contact details to an
    unrecognized caller, so this leg deliberately tests the allowed path:
    a known caller asking about another contact.
    """
    if aut.contacts.lookup(phone=driver_number):
        return
    from inkbox.contacts.types import ContactPhone

    aut.contacts.create(
        given_name="Penny",
        family_name="Tester",
        phones=[ContactPhone(label="mobile", value=driver_number)],
    )


@pytest.mark.skipif(SCENARIO != "outbound_realtime_contact", reason="realtime contact-lookup leg only")
def test_outbound_call_realtime_direct_contact_lookup():
    """Mid-call, the realtime agent answers a contact question with details.

    The driver (a recognized contact) texts "call me"; the agent dials back on
    the realtime path and the driver asks for the email on file for a seeded
    contact. Proves utility end to end: the spoken answer must carry the seeded
    card's distinctive details, and the gateway log must show the direct
    contact-read tool ran (not a consult_agent round-trip).
    """
    from inkbox.contacts.types import ContactEmail

    st = _driver_state()
    remote, aut = _client(REMOTE_KEY), _client(AUT_KEY)
    aut_phone = _aut_phone(aut)
    tail = _digits(aut_phone)[-10:]

    _ensure_driver_is_a_known_contact(aut, st["number"])
    _delete_contacts_by_email(aut, LOOKUP_CONTACT_EMAIL)
    aut.contacts.create(
        given_name=LOOKUP_CONTACT_GIVEN,
        family_name=LOOKUP_CONTACT_FAMILY,
        emails=[ContactEmail(label="work", value=LOOKUP_CONTACT_EMAIL)],
    )
    try:
        driver_tail = _digits(st["number"])[-10:]

        def _inbound_to_driver():
            # The driver's INBOUND leg (remote/penetrator identity). Used only to
            # confirm a call was placed — its transcript comes from the driver's
            # own relay, not the agent's, so it is NOT where the recite lands.
            return [c for c in remote.calls.list(limit=30)
                    if (getattr(c, "direction", "") or "").lower() == "inbound"
                    and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == tail]

        def _outbound_from_agent():
            # The agent's OWN outbound call to the driver — the record the Inkbox
            # console shows, and where the realtime relay persists the recite
            # (client_ws_agent → CLIENT_TRANSCRIPT_FINAL). Read THIS for the recite.
            return [c for c in aut.calls.list(limit=30)
                    if (getattr(c, "direction", "") or "").lower() == "outbound"
                    and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == driver_tail]

        def _recite_from(reads):
            # reads: (client, call_id) pairs. Return the transcript that carries
            # the seeded card's spoken details ("…parker…example…"), or "".
            for client, cid in reads:
                try:
                    segs = client.calls.transcripts(cid)
                except Exception:  # transcripts 404 until the call is set up
                    continue
                transcript = " ".join(
                    (s.text or "").strip() for s in segs if (s.text or "").strip()
                )
                squashed = transcript.lower().replace(" ", "")
                if LOOKUP_CONTACT_FAMILY.lower() in squashed and "example" in squashed:
                    return transcript
            return ""

        # place_call can succeed API-side yet the PSTN leg never materialize (bad
        # carrier window); a fresh "call me" retries. Allow one fresh call before
        # failing.
        attempt_timeout = max(TIMEOUT_S / 2, 110.0)
        recite = ""
        placed_call = False
        end_ids = []  # (client, call_id) legs to hang up so nothing lingers
        for attempt in (1, 2):
            before_out = {c.id for c in _outbound_from_agent()}
            before_in = {c.id for c in _inbound_to_driver()}
            remote.texts.send(st["number_id"], to=aut_phone, text=_call_me_text())

            deadline = time.monotonic() + attempt_timeout
            while time.monotonic() < deadline:
                fresh_out = [c for c in _outbound_from_agent() if c.id not in before_out]
                fresh_in = [c for c in _inbound_to_driver() if c.id not in before_in]
                placed_call = placed_call or bool(fresh_out or fresh_in)
                end_ids = [(aut, c.id) for c in fresh_out] + [(remote, c.id) for c in fresh_in]
                # The recite persists on the AGENT's own call record; the driver
                # leg is a fallback only.
                recite = recite or _recite_from(end_ids)
                # The direct-read log line proves the agent used the direct
                # contact tool (not a consult loop) — deterministic anchor.
                got_log = "direct contact read inkbox_" in _gateway_log_text()
                if recite and got_log:
                    break
                time.sleep(POLL_EVERY_S)
            if recite and "direct contact read inkbox_" in _gateway_log_text():
                break

        # Ensure every call actually ends. Realtime sends a `stop` frame, but the
        # PSTN leg can linger at "answered" — force it closed via the SDK hangup so
        # it finalizes (and doesn't burn a call slot). Best-effort; already-ended
        # legs just error out harmlessly.
        for client, cid in end_ids:
            try:
                client.calls.hangup(cid)
            except Exception:
                pass

        # After teardown the transcript is final — give the recite a last chance
        # to land (in case it persisted on the very last turn).
        if not recite and end_ids:
            end_deadline = time.monotonic() + 30.0
            while time.monotonic() < end_deadline and not recite:
                recite = _recite_from(end_ids)
                if recite:
                    break
                time.sleep(POLL_EVERY_S)

        assert placed_call, (
            f"agent never placed a call back within {attempt_timeout:.0f}s "
            "in two attempts"
        )
        # Deterministic anchor: the realtime agent did a DIRECT contact read on
        # the call (vs a consult loop or no lookup) — written when the contact
        # tool is invoked.
        log_text = _gateway_log_text()
        assert log_text, "gateway log unavailable to prove the direct contact read"
        assert "direct contact read inkbox_" in log_text, \
            "gateway logs show no direct contact read during the call"
        # And the agent actually recited the seeded card's email out loud — read
        # from the agent's own call record, where the relay persists it.
        assert recite, (
            "agent never recited the seeded contact's email on the call — no "
            "transcript segment on the agent's own call carried the card details"
        )

        tts, stt = _aut_speech_mode(aut, "outbound", st["number"])
        assert tts is False and stt is False, \
            f"call must be on the realtime path (Inkbox speech off), got tts={tts} stt={stt}"
    finally:
        _delete_contacts_by_email(aut, LOOKUP_CONTACT_EMAIL)


@pytest.mark.skipif(SCENARIO != "outbound_realtime", reason="outbound realtime leg only")
def test_outbound_call_realtime():
    """Driver texts 'call me'; the agent places a realtime-powered call and replies."""
    st = _driver_state()
    remote, aut = _client(REMOTE_KEY), _client(AUT_KEY)
    aut_phone = _aut_phone(aut)
    tail = _digits(aut_phone)[-10:]

    def _inbound_from_aut():
        return [c for c in remote.calls.list(limit=30)
                if (getattr(c, "direction", "") or "").lower() == "inbound"
                and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == tail]

    before = {c.id for c in _inbound_from_aut()}
    remote.texts.send(st["number_id"], to=aut_phone, text=_call_me_text())

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
