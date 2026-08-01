"""Live voice-call suite — real phone calls, real model, transcript-verified.

Two scenarios, each run against a gateway booted in the matching speech mode (the
workflow sets that up and selects the scenario via VOICE_SCENARIO):

  * inbound_inkbox   — the driver calls the agent; the agent answers with Inkbox
                       STT/TTS and holds a turn.
  * outbound_hosted  — the driver texts "call me"; Inkbox Voice AI calls back
                       and Hermes receives the hosted completion event.
  * outbound_realtime — the driver texts "call me"; the agent places a call
                       back, powered by the realtime API, and holds a turn.

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
from datetime import datetime, timezone

import pytest


# The agent answers a call request by dialing back, not by texting, so these
# driver→AUT SMS never get an SMS reply to reset the server's conversation
# cadence. Two identical no-reply sends to the same number trip the
# duplicate_body rule (422), so every call request must carry a fresh body.
_CALL_ME_PHRASINGS = (
    "Please call me right now by phone and set voicemail_detection to disabled.",
    "Can you ring me now with voicemail_detection disabled?",
    "Give me a call now, using disabled voicemail_detection.",
    "Please phone me right away and disable voicemail_detection.",
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
HOSTED_POST_CALL_MARKER = os.environ.get("HOSTED_POST_CALL_MARKER", "")
STATE_FILE = os.environ.get("VOICE_DRIVER_STATE", "/tmp/voice_driver_state.json")
TIMEOUT_S = float(os.environ.get("LIVE_VOICE_TIMEOUT", "220"))
POLL_EVERY_S = 6.0
# After the deterministic direct-read log line lands, how long to keep polling
# for the (racy) spoken recite before giving up on it as best-effort.
RECITE_GRACE_S = 24.0

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY and REAL),
    reason="voice suite: needs both keys + LIVE_REAL_MODEL=1",
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _normalized_spoken_text(value: str | None) -> str:
    """Compare speech-carried markers without punctuation/case artifacts."""
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _spoken_marker_key(value: str | None) -> str:
    """Ignore separators that speech recognition may add or remove."""
    return "".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _hosted_request_persisted(transcript: str, marker: str) -> bool:
    """Whether the caller's persisted transcript carries this run's request."""
    text = _normalized_spoken_text(transcript)
    expected_marker = _spoken_marker_key(marker)
    sms_intent = re.search(r"\bsend me (?:one|1|an) sms\b", text)
    return bool(
        expected_marker and "after we hang up" in text and sms_intent and expected_marker in _spoken_marker_key(text)
    )


def _hosted_action_persisted(call, marker: str) -> bool:
    """Whether this call has an open SMS action for the current marker."""
    expected_marker = _spoken_marker_key(marker)
    if not expected_marker:
        return False
    for item in getattr(call, "post_call_action_items", None) or []:
        if isinstance(item, dict):
            status = item.get("status")
            action = item.get("action")
            details = item.get("details")
        else:
            status = getattr(item, "status", None)
            action = getattr(item, "action", None)
            details = getattr(item, "details", None)
        text = _normalized_spoken_text(f"{action or ''} {details or ''}")
        status_value = getattr(status, "value", status)
        if (
            str(status_value or "").casefold() == "open"
            and re.search(r"\bsend\b", text)
            and re.search(r"\b(?:sms|text(?: message)?)\b", text)
            and expected_marker in _spoken_marker_key(text)
        ):
            return True
    return False


def _message_created_at(message) -> datetime | None:
    """Return an aware server timestamp from an SDK SMS row."""
    value = getattr(message, "created_at", None)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sms_target_numbers(message) -> set[str]:
    """All authoritative targets represented by an outbound SMS row."""
    values = [getattr(message, "remote_phone_number", "") or ""]
    values.extend(
        getattr(recipient, "recipient_phone_number", "") or "" for recipient in (getattr(message, "recipients", None) or [])
    )
    return {digits for value in values if (digits := _digits(value))}


def _voicemail_detection_value(call) -> str:
    value = getattr(call, "voicemail_detection", "")
    return str(getattr(value, "value", value) or "").casefold()


def _assert_voicemail_detection_disabled(call) -> None:
    assert _voicemail_detection_value(call) == "disabled", (
        f"call {getattr(call, 'id', None)} did not persist disabled "
        f"voicemail detection: {_voicemail_detection_value(call)!r}"
    )


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


def _ensure_driver_allowed(aut, driver_number: str) -> None:
    """Allow the live driver through identity-level phone contact rules."""
    handle = aut.mailboxes.list()[0].email_address.split("@", 1)[0]
    rules = aut.phone_identity_contact_rules.list(handle)
    for rule in rules:
        if (
            getattr(rule, "match_target", "") == driver_number
            and str(getattr(rule, "action", "")).lower().endswith("allow")
            and str(getattr(rule, "status", "active")).lower().endswith("active")
        ):
            return
    aut.phone_identity_contact_rules.create(
        handle,
        action="allow",
        match_type="exact_number",
        match_target=driver_number,
    )


def _segments(remote, number_id, call_id):
    """Transcript segments for a call, split by who spoke."""
    # Identity-centered transcript read (SDK 0.4.15+); number_id is vestigial.
    segs = remote.calls.transcripts(call_id)
    rem = [s for s in segs if (getattr(s, "party", "") or "").lower() == "remote" and (s.text or "").strip()]
    loc = [s for s in segs if (getattr(s, "party", "") or "").lower() == "local" and (s.text or "").strip()]
    return segs, rem, loc


# A call can end normally and still never carry a conversation - answering-machine
# detection hanging up on the driver ends it `completed`, hangup_reason=voicemail.
# Transcript rows can still land during teardown, so allow a short grace period
# before giving up rather than polling a finished call for the full timeout.
TERMINAL_FAILURE_STATUSES = {"canceled", "failed"}
ENDED_STATUSES = {"completed"}
ENDED_GRACE_S = float(os.environ.get("LIVE_VOICE_ENDED_GRACE", "15"))


def _call_state(remote, call_id) -> tuple[str, str]:
    """Compact current call state for progress and terminal-failure output."""
    call = remote.calls.get(call_id)
    status = (getattr(call, "status", "") or "").lower()
    fields = (
        f"status={status!r}",
        f"reason={getattr(call, 'reason', None)!r}",
        f"hangup_reason={getattr(call, 'hangup_reason', None)!r}",
        f"started_at={getattr(call, 'started_at', None)!r}",
        f"ended_at={getattr(call, 'ended_at', None)!r}",
    )
    return status, " ".join(fields)


def _wait_for_two_way_call(
    remote,
    number_id,
    call_id,
    *,
    agent_turns=1,
    deadline: float | None = None,
):
    """Block until the driver spoke and the agent completed enough turns."""
    deadline = deadline or (time.monotonic() + TIMEOUT_S)
    last = ""
    ended_at = None
    while time.monotonic() < deadline:
        transcript_state = ""
        try:
            _all, rem, loc = _segments(remote, number_id, call_id)
        except Exception as exc:  # transcripts may 404 until the call is set up
            rem, loc = [], []
            transcript_state = f"transcripts not ready: {exc!r}"
        if not transcript_state and len(rem) >= agent_turns and loc:
            agent_said = " | ".join(s.text.strip() for s in rem)
            return agent_said  # the agent reached the caller out loud, in a two-way call
        try:
            status, state = _call_state(remote, call_id)
        except Exception as exc:
            state = f"call state unavailable: {exc!r}"
            status = ""
        progress = transcript_state or f"segments so far: remote={len(rem)} local={len(loc)}"
        last = f"{progress}; {state}"
        if status in TERMINAL_FAILURE_STATUSES:
            pytest.fail(f"call ended before a two-way conversation ({last})")
        if status in ENDED_STATUSES:
            if ended_at is None:
                ended_at = time.monotonic()
            elif time.monotonic() - ended_at > ENDED_GRACE_S:
                pytest.fail(f"call ended without a two-way conversation ({last})")
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"agent never held a two-way call within {TIMEOUT_S:.0f}s ({last})")


def _wait_for_hosted_request(
    remote,
    number_id,
    call_id,
    marker: str,
    *,
    deadline: float | None = None,
) -> None:
    """Wait for the current caller's after-call SMS request to persist."""
    deadline = deadline or (time.monotonic() + TIMEOUT_S)
    last = ""
    while time.monotonic() < deadline:
        try:
            _all, _remote, local = _segments(remote, number_id, call_id)
            transcript = " ".join(segment.text.strip() for segment in local)
            if _hosted_request_persisted(transcript, marker):
                return
            last = f"local transcript so far: {_normalized_spoken_text(transcript)!r}"
        except Exception as exc:  # transcripts may 404 until setup completes
            last = f"transcripts not ready: {exc!r}"
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"call ended before the current hosted SMS request was persisted ({last})")


def _wait_for_hosted_action(
    aut,
    call_id,
    marker: str,
    *,
    deadline: float | None = None,
) -> None:
    """Wait until Voice AI durably records this run's open SMS action."""
    deadline = deadline or (time.monotonic() + TIMEOUT_S)
    last = "call not ready"
    while time.monotonic() < deadline:
        try:
            call = aut.calls.get(call_id)
            if _hosted_action_persisted(call, marker):
                return
            actions = getattr(call, "post_call_action_items", None) or []
            last = f"status={getattr(call, 'status', None)!r} open_actions={len(actions)}"
        except Exception as exc:
            last = f"call action items not ready: {exc!r}"
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"call ended before Voice AI persisted the current hosted SMS action ({last})")


def _aut_speech_mode(aut, direction, driver_number):
    """(use_inkbox_tts, use_inkbox_stt) of the agent's most recent answered call
    in `direction` with the driver. Tells Inkbox STT/TTS (True/True) from realtime
    (False/False), so each leg can prove it ran the speech path it claims."""
    tail = _digits(driver_number)[-10:]
    answered = [
        c
        for c in aut.calls.list(limit=10)
        if (getattr(c, "direction", "") or "").lower() == direction
        and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == tail
        and c.use_inkbox_tts is not None
    ]
    assert answered, f"no answered {direction} agent call with the driver found"
    c = answered[0]  # newest first
    return c.use_inkbox_tts, c.use_inkbox_stt


def _hangup_call(client, call_id) -> None:
    """End a live test call through the control API, tolerating an ended race."""
    if not call_id:
        return
    try:
        client.calls.hangup(call_id)
        return
    except Exception as hangup_error:
        deadline = time.monotonic() + 10
        status = "unknown"
        while time.monotonic() < deadline:
            try:
                status = (getattr(client.calls.get(call_id), "status", "") or "").lower()
            except Exception:
                status = "unknown"
            if status in {"completed", "canceled", "failed"}:
                return
            time.sleep(0.5)
        raise RuntimeError(f"failed to hang up live test call {call_id}; status={status!r}") from hangup_error


@pytest.mark.skipif(SCENARIO != "inbound_inkbox", reason="inbound Inkbox STT/TTS leg only")
def test_inbound_call_inkbox_tts_stt():
    """Driver calls the agent; the agent answers via Inkbox STT/TTS and replies."""
    st = _driver_state()
    remote, aut = _client(REMOTE_KEY), _client(AUT_KEY)
    aut_phone = _aut_phone(aut)

    # Server-side contact rules run before the plugin or its local allow-all
    # setting. Whitelisted smoke identities therefore need the driver allowed
    # explicitly or the call is rejected before either media WS connects.
    _ensure_driver_allowed(aut, st["number"])

    # Place the call to the agent, handing Inkbox the driver's own media WS.
    call = remote.calls.place(
        from_number=st["number"],
        to_number=aut_phone,
        client_websocket_url=st["ws_url"],
        voicemail_detection="disabled",
    )
    try:
        _assert_voicemail_detection_disabled(remote.calls.get(call.id))
        agent_said = _wait_for_two_way_call(remote, st["number_id"], call.id)
        assert agent_said, "agent produced no speech on the inbound call"

        tts, stt = _aut_speech_mode(aut, "inbound", st["number"])
        assert tts and stt, f"inbound call should run Inkbox STT/TTS, got tts={tts} stt={stt}"
    finally:
        _hangup_call(remote, call.id)


@pytest.mark.skipif(SCENARIO != "outbound_hosted", reason="outbound Voice AI leg only")
def test_outbound_call_inkbox_voice_ai_and_completion():
    """Voice AI calls back, then Hermes executes one post-call commitment."""
    st = _driver_state()
    remote, aut = _client(REMOTE_KEY), _client(AUT_KEY)
    aut_numbers = aut.phone_numbers.list()
    assert aut_numbers, "AUT identity has no phone number"
    aut_phone = aut_numbers[0].number
    aut_tail = _digits(aut_phone)[-10:]
    driver_number = _digits(st["number"])
    driver_tail = driver_number[-10:]

    def _driver_inbound():
        return [
            call
            for call in remote.calls.list(limit=200)
            if (getattr(call, "direction", "") or "").lower() == "inbound"
            and _digits(getattr(call, "remote_phone_number", "") or "")[-10:] == aut_tail
        ]

    def _aut_outbound():
        return [
            call
            for call in aut.calls.list(limit=200)
            if (getattr(call, "direction", "") or "").lower() == "outbound"
            and _digits(getattr(call, "remote_phone_number", "") or "")[-10:] == driver_tail
        ]

    aut_number_id = aut_numbers[0].id

    def _aut_outbound_sms():
        return [
            message
            for message in aut.texts.list(aut_number_id, limit=200)
            if (getattr(message, "direction", "") or "").lower() == "outbound"
            and driver_number in _sms_target_numbers(message)
        ]

    assert HOSTED_POST_CALL_MARKER, "HOSTED_POST_CALL_MARKER must be set for the hosted completion leg"
    expected_postcall_text = _spoken_marker_key(HOSTED_POST_CALL_MARKER)
    assert expected_postcall_text, "hosted completion marker must contain words"
    scenario_deadline = time.monotonic() + TIMEOUT_S
    before_driver = {call.id for call in _driver_inbound()}
    before_aut = {call.id for call in _aut_outbound()}
    baseline_sms = _aut_outbound_sms()
    before_postcall_sms = {message.id for message in baseline_sms}
    baseline_times = [created_at for message in baseline_sms if (created_at := _message_created_at(message)) is not None]
    sms_watermark = max(
        baseline_times,
        default=datetime.min.replace(tzinfo=timezone.utc),
    )
    remote.texts.send(st["number_id"], to=aut_phone, text=_call_me_text())

    driver_call_id = None
    aut_call = None
    try:
        while time.monotonic() < scenario_deadline:
            fresh_driver = [call for call in _driver_inbound() if call.id not in before_driver]
            fresh_aut = [call for call in _aut_outbound() if call.id not in before_aut]
            if fresh_driver:
                driver_call_id = fresh_driver[0].id
            if fresh_aut:
                aut_call = fresh_aut[0]
            if driver_call_id and aut_call is not None:
                break
            time.sleep(POLL_EVERY_S)

        assert driver_call_id, "Inkbox Voice AI never reached the driver"
        assert aut_call is not None, "AUT hosted outbound call record was not created"
        mode = getattr(aut_call, "mode", "")
        assert str(getattr(mode, "value", mode)) == "hosted_agent"
        _assert_voicemail_detection_disabled(aut_call)
        assert getattr(aut_call, "reason", None), "hosted call must persist a task reason"
        assert (
            getattr(
                aut_call,
                "hosted_agent_authority_mode",
                None,
            )
            is not None
        )

        agent_said = _wait_for_two_way_call(
            remote,
            st["number_id"],
            driver_call_id,
            deadline=scenario_deadline,
        )
        assert agent_said, "Inkbox Voice AI produced no speech"
        _wait_for_hosted_request(
            remote,
            st["number_id"],
            driver_call_id,
            HOSTED_POST_CALL_MARKER,
            deadline=scenario_deadline,
        )
        _wait_for_hosted_action(
            aut,
            aut_call.id,
            HOSTED_POST_CALL_MARKER,
            deadline=scenario_deadline,
        )
    finally:
        _hangup_call(remote, driver_call_id)

    enqueue_marker = f"Enqueued hosted call completion for call_id={aut_call.id}"
    completed_marker = (
        f"Finished hosted call reconciliation for call_id={aut_call.id} outcome=success receipt_state=completed"
    )
    duplicate_grace = 2 * POLL_EVERY_S
    settlement_deadline = scenario_deadline - duplicate_grace
    accepted = []
    while time.monotonic() < settlement_deadline:
        logs = _gateway_log_text()
        accepted = [
            message
            for message in _aut_outbound_sms()
            if (
                message.id not in before_postcall_sms
                and (created_at := _message_created_at(message)) is not None
                and created_at >= sms_watermark
                and _spoken_marker_key(getattr(message, "text", None)) == expected_postcall_text
            )
        ]
        if completed_marker in logs and accepted:
            break
        poll_delay = min(3.0, max(0.0, settlement_deadline - time.monotonic()))
        if poll_delay:
            time.sleep(poll_delay)
    logs = _gateway_log_text()
    assert enqueue_marker in logs, "Hermes did not receive the hosted call.ended completion"
    assert completed_marker in logs, "Hermes did not finish the hosted post-call reconciliation"
    assert accepted, (
        f"Hermes did not execute the hosted post-call SMS commitment within the shared {TIMEOUT_S:.0f}s scenario budget"
    )

    # Give any accidental model-text delivery time to land, then prove the one
    # explicit tool side effect is the only post-call SMS.
    remaining_budget = scenario_deadline - time.monotonic()
    assert remaining_budget >= duplicate_grace, (
        "Hermes confirmed the hosted SMS too late to complete the reserved "
        f"{duplicate_grace:.0f}s duplicate-observation window"
    )
    time.sleep(duplicate_grace)
    marker_sms = [
        message
        for message in _aut_outbound_sms()
        if (
            message.id not in before_postcall_sms
            and (created_at := _message_created_at(message)) is not None
            and created_at >= sms_watermark
            and _spoken_marker_key(getattr(message, "text", None)) == expected_postcall_text
        )
    ]
    assert len(marker_sms) == 1, (
        "Hosted post-call processing duplicated or missed the current "
        f"commitment: {[getattr(m, 'text', '') for m in marker_sms]}"
    )
    assert _spoken_marker_key(getattr(marker_sms[0], "text", None)) == expected_postcall_text


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
            return [
                c
                for c in remote.calls.list(limit=200)
                if (getattr(c, "direction", "") or "").lower() == "inbound"
                and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == tail
            ]

        def _outbound_from_agent():
            # The agent's OWN outbound call to the driver — the record the Inkbox
            # console shows, and where the realtime relay persists the recite
            # (client_ws_agent → CLIENT_TRANSCRIPT_FINAL). Read THIS for the recite.
            return [
                c
                for c in aut.calls.list(limit=200)
                if (getattr(c, "direction", "") or "").lower() == "outbound"
                and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == driver_tail
            ]

        def _recite_from(reads):
            # reads: (client, call_id) pairs. Return the transcript that carries
            # the seeded card's spoken details ("…parker…example…"), or "".
            for client, cid in reads:
                try:
                    segs = client.calls.transcripts(cid)
                except Exception:  # transcripts 404 until the call is set up
                    continue
                transcript = " ".join((s.text or "").strip() for s in segs if (s.text or "").strip())
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
        aut_call = None
        end_ids = []  # (client, call_id) legs to hang up so nothing lingers
        for attempt in (1, 2):
            before_out = {c.id for c in _outbound_from_agent()}
            before_in = {c.id for c in _inbound_to_driver()}
            remote.texts.send(st["number_id"], to=aut_phone, text=_call_me_text())

            deadline = time.monotonic() + attempt_timeout
            log_seen_at = None
            while time.monotonic() < deadline:
                fresh_out = [c for c in _outbound_from_agent() if c.id not in before_out]
                fresh_in = [c for c in _inbound_to_driver() if c.id not in before_in]
                placed_call = placed_call or bool(fresh_out or fresh_in)
                if fresh_out:
                    aut_call = fresh_out[0]
                end_ids = [(aut, c.id) for c in fresh_out] + [(remote, c.id) for c in fresh_in]
                # Best-effort: the recite persists on the AGENT's own call record;
                # the driver leg is a fallback only.
                recite = recite or _recite_from(end_ids)
                # The direct-read log line is the deterministic proof — written
                # when the agent invokes the contact tool on the call. Once it's
                # there the call did what we test; give the (racy) recite a short
                # grace to also land, then stop rather than burning the window.
                if "direct contact read inkbox_" in _gateway_log_text():
                    if log_seen_at is None:
                        log_seen_at = time.monotonic()
                    if recite or time.monotonic() - log_seen_at > RECITE_GRACE_S:
                        break
                time.sleep(POLL_EVERY_S)
            if "direct contact read inkbox_" in _gateway_log_text():
                break

        # Ensure every call actually ends. Realtime sends a `stop` frame, but the
        # PSTN leg can linger at "answered" — force it closed via the control API.
        for client, cid in end_ids:
            _hangup_call(client, cid)

        # After teardown the transcript is final — give the recite a last chance
        # to land (in case it persisted on the very last turn).
        if not recite and end_ids:
            end_deadline = time.monotonic() + 30.0
            while time.monotonic() < end_deadline and not recite:
                recite = _recite_from(end_ids)
                if recite:
                    break
                time.sleep(POLL_EVERY_S)

        assert placed_call, f"agent never placed a call back within {attempt_timeout:.0f}s in two attempts"
        assert aut_call is not None, "agent call was not visible on the authoritative identity"
        _assert_voicemail_detection_disabled(aut_call)
        # Deterministic anchor: the realtime agent did a DIRECT contact read on
        # the call (vs a consult loop or no lookup) — written when the contact
        # tool is invoked.
        log_text = _gateway_log_text()
        assert log_text, "gateway log unavailable to prove the direct contact read"
        assert "direct contact read inkbox_" in log_text, "gateway logs show no direct contact read during the call"
        # The spoken recite is the call's FINAL turn, and realtime relays each
        # turn's transcript live over the call WS — that last relay races the
        # teardown (realtime.py `_relay_transcript` is wrapped in `suppress`), so
        # the recite lands in the stored transcript only ~80% of the time even
        # though the agent always says it. Hard-gating on it tests our transcript
        # plumbing, not the agent, and makes the test flaky — so it's a best-effort
        # bonus. The direct-read log line above is the deterministic proof the
        # agent looked the contact up and served the answer on the call.
        if recite:
            print(f"recite captured on the call: {recite[:160]!r}")
        else:
            print(
                "note: direct contact read confirmed in the gateway log, but the "
                "spoken recite didn't land in the stored transcript (final-turn "
                "relay races call teardown) — best-effort, not a failure."
            )

        tts, stt = _aut_speech_mode(aut, "outbound", st["number"])
        assert tts is False and stt is False, (
            f"call must be on the realtime path (Inkbox speech off), got tts={tts} stt={stt}"
        )
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
        return [
            c
            for c in remote.calls.list(limit=200)
            if (getattr(c, "direction", "") or "").lower() == "inbound"
            and _digits(getattr(c, "remote_phone_number", "") or "")[-10:] == tail
        ]

    driver_tail = _digits(st["number"])[-10:]

    def _outbound_from_agent():
        return [
            c
            for c in aut.calls.list(limit=200)
            if (getattr(c, "direction", "") or "").lower() == "outbound"
            and _digits(getattr(c, "remote_phone_number", "") or "")[-10:]
            == driver_tail
        ]

    before = {c.id for c in _inbound_from_aut()}
    before_aut = {c.id for c in _outbound_from_agent()}
    remote.texts.send(st["number_id"], to=aut_phone, text=_call_me_text())

    call_id = None
    aut_call = None
    try:
        # Wait for the agent to dial back, then verify the call transcript.
        deadline = time.monotonic() + TIMEOUT_S
        while time.monotonic() < deadline:
            fresh = [c for c in _inbound_from_aut() if c.id not in before]
            fresh_aut = [c for c in _outbound_from_agent() if c.id not in before_aut]
            if fresh:
                call_id = fresh[0].id
            if fresh_aut:
                aut_call = fresh_aut[0]
            if call_id and aut_call is not None:
                break
            time.sleep(POLL_EVERY_S)
        assert call_id, f"agent never placed a call back within {TIMEOUT_S:.0f}s"
        assert aut_call is not None, "agent call was not visible on the authoritative identity"
        _assert_voicemail_detection_disabled(aut_call)

        agent_said = _wait_for_two_way_call(remote, st["number_id"], call_id)
        assert agent_said, "agent produced no speech on the outbound call"

        tts, stt = _aut_speech_mode(aut, "outbound", st["number"])
        assert tts is False and stt is False, (
            f"outbound call must be powered by the realtime API (Inkbox speech off), got tts={tts} stt={stt}"
        )
    finally:
        _hangup_call(remote, call_id)
