from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_voice_module():
    path = Path(__file__).parent / "live" / "test_voice.py"
    spec = importlib.util.spec_from_file_location("live_voice_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


voice = _load_voice_module()


def test_spoken_marker_normalization_is_case_and_punctuation_insensitive() -> None:
    assert voice._normalized_spoken_text("Alpha, BRAVO! Charlie.") == ("alpha bravo charlie")
    assert voice._spoken_marker_key("Alpha X-ray") == "alphaxray"
    assert voice._spoken_marker_key("alpha x ray") == "alphaxray"


def test_settlement_candidate_filter_is_not_the_exact_final_sms_oracle() -> None:
    marker = voice._spoken_marker_key("alpha xray charlie")

    assert voice._sms_contains_marker(SimpleNamespace(text="Alpha, X-ray Charlie."), marker)
    assert voice._sms_contains_marker(SimpleNamespace(text="Requested words: alpha x ray charlie — done."), marker)
    assert not voice._sms_contains_marker(SimpleNamespace(text="alpha xray"), marker)
    assert not voice._sms_contains_marker(SimpleNamespace(text="alpha delta xray charlie"), marker)
    assert not voice._sms_contains_marker(SimpleNamespace(text=None), marker)


def test_sms_targets_include_direct_and_recipient_rows() -> None:
    message = SimpleNamespace(
        remote_phone_number="+1 (555) 111-2222",
        recipients=[
            SimpleNamespace(recipient_phone_number="+1 555 333 4444"),
        ],
    )

    assert voice._sms_target_numbers(message) == {
        "15551112222",
        "15553334444",
    }


def test_voicemail_detection_normalizes_sdk_enum_and_string() -> None:
    assert voice._voicemail_detection_value(SimpleNamespace(voicemail_detection="disabled")) == "disabled"
    assert (
        voice._voicemail_detection_value(
            SimpleNamespace(
                voicemail_detection=SimpleNamespace(value="disabled"),
            )
        )
        == "disabled"
    )


def test_hosted_request_requires_intent_and_current_marker() -> None:
    marker = "alpha xray charlie"

    assert voice._hosted_request_persisted(
        "After we hang up, send me one SMS containing Alpha, X-ray Charlie.",
        marker,
    )
    assert voice._hosted_request_persisted(
        "After we hang up, send me 1 SMS containing alpha x ray charlie.",
        marker,
    )
    assert voice._hosted_request_persisted(
        "After we hang up, send me an SMS containing alpha xray charlie.",
        marker,
    )
    assert not voice._hosted_request_persisted(
        "After we hang up, send me one SMS containing delta echo foxtrot.",
        marker,
    )
    assert not voice._hosted_request_persisted(
        "Alpha xray charlie is a useful phrase.",
        marker,
    )


def test_hosted_action_requires_open_sms_intent_and_current_marker() -> None:
    marker = "alpha xray charlie"

    matching = SimpleNamespace(
        post_call_action_items=[
            SimpleNamespace(
                status="open",
                action="Send a text message after the call",
                details="Use the words Alpha, X-ray Charlie.",
            ),
        ],
    )
    assert voice._hosted_action_persisted(matching, marker)

    dict_matching = SimpleNamespace(
        post_call_action_items=[
            {
                "status": "open",
                "action": "Send SMS",
                "details": "alpha x ray charlie",
            }
        ],
    )
    assert voice._hosted_action_persisted(dict_matching, marker)

    wrong_marker = SimpleNamespace(
        post_call_action_items=[
            SimpleNamespace(
                status="open",
                action="Send SMS",
                details="delta echo foxtrot",
            ),
        ],
    )
    assert not voice._hosted_action_persisted(wrong_marker, marker)

    closed = SimpleNamespace(
        post_call_action_items=[
            SimpleNamespace(
                status="completed",
                action="Send SMS",
                details="alpha xray charlie",
            ),
        ],
    )
    assert not voice._hosted_action_persisted(closed, marker)

    missing_status = SimpleNamespace(
        post_call_action_items=[
            SimpleNamespace(
                status=None,
                action="Send SMS",
                details="alpha xray charlie",
            ),
        ],
    )
    assert not voice._hosted_action_persisted(missing_status, marker)

    discussion_only = SimpleNamespace(
        post_call_action_items=[
            SimpleNamespace(
                status="open",
                action="Discuss SMS options",
                details="alpha xray charlie",
            ),
        ],
    )
    assert not voice._hosted_action_persisted(discussion_only, marker)


def test_message_created_at_normalizes_sdk_timestamp_shapes() -> None:
    aware = datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 8, 1, 7, 0)

    assert voice._message_created_at(SimpleNamespace(created_at=aware)) == aware
    assert voice._message_created_at(SimpleNamespace(created_at=naive)) == aware
    assert voice._message_created_at(SimpleNamespace(created_at="2026-08-01T07:00:00Z")) == aware
    assert voice._message_created_at(SimpleNamespace(created_at="bad")) is None


@pytest.mark.parametrize("defect", ["prose", "merged_words", "extra_marker", "extra_other", "early", "missing_created", "missing_ended", "wrong_target", "extra_target"])
def test_post_call_sms_rejects_false_success(defect):
    from datetime import timedelta

    ended = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    message = SimpleNamespace(id="new", text="Alpha Bravo Charlie", created_at=ended,
                              remote_phone_number="+15551112222", recipients=[])
    call = SimpleNamespace(ended_at=ended)
    messages = [message]
    if defect == "prose":
        message.text = "Your words: Alpha Bravo Charlie"
    elif defect == "merged_words":
        message.text = "AlphaBravoCharlie"
    elif defect in {"extra_marker", "extra_other"}:
        messages.append(SimpleNamespace(id="extra", text=message.text if defect == "extra_marker" else "Done",
                                        created_at=ended, remote_phone_number="+15551112222", recipients=[]))
    elif defect == "early":
        message.created_at = ended - timedelta(microseconds=1)
    elif defect == "missing_created":
        message.created_at = None
    elif defect == "missing_ended":
        call.ended_at = None
    elif defect == "wrong_target":
        message.remote_phone_number = "+15553334444"
    else:
        message.recipients = [SimpleNamespace(recipient_phone_number="+15553334444")]
    with pytest.raises(AssertionError):
        voice._assert_post_call_sms(messages, set(), "Alpha Bravo Charlie", call, "+15551112222")


def test_post_call_sms_accepts_one_exact_body_after_persisted_end_and_ignores_baseline():
    ended = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    old = SimpleNamespace(id="old", text="Unrelated old message")
    new = SimpleNamespace(id="new", text="Alpha, Bravo Charlie.", created_at=ended.isoformat(),
                          remote_phone_number="+15551112222", recipients=[])
    voice._assert_post_call_sms([old, new], {"old"}, "Alpha Bravo Charlie",
                                SimpleNamespace(ended_at=ended), "+15551112222")


@pytest.mark.parametrize("peer_text,passes", [
    ("After we hang up, send me one SMS containing Alpha Bravo Charlie.", True),
    ("After we hang up, send me one SMS.", False),
    ("", False),
])
def test_hosted_request_remote_gate_requires_actual_received_marker(monkeypatch, peer_text, passes):
    now = [0.0]
    monkeypatch.setattr(voice.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(voice.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    client = SimpleNamespace(calls=SimpleNamespace(transcripts=lambda _: [
        SimpleNamespace(party="local", text="After we hang up, send me one SMS containing Alpha Bravo Charlie."),
        SimpleNamespace(party="remote", text=peer_text),
    ]))
    if passes:
        voice._wait_for_hosted_request(client, "unused", "call", "Alpha Bravo Charlie", deadline=10, party="remote")
    else:
        with pytest.raises(pytest.fail.Exception):
            voice._wait_for_hosted_request(client, "unused", "call", "Alpha Bravo Charlie", deadline=10, party="remote")


@pytest.mark.parametrize("local_text,passes", [("Alpha Bravo Charlie", True), ("Hello", False), ("Alpha Bravo", False)])
@pytest.mark.parametrize("party", ["local", "remote"])
def test_readback_requires_complete_speech_from_correct_party(monkeypatch, local_text, passes, party):
    now = [0.0]
    monkeypatch.setattr(voice.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(voice.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    client = SimpleNamespace(calls=SimpleNamespace(transcripts=lambda _: [
        SimpleNamespace(party="local" if party == "remote" else "remote", text="Alpha Bravo Charlie"),
        SimpleNamespace(party=party, text=local_text),
    ]))
    if passes:
        voice._wait_for_hosted_readback(client, "call", "Alpha Bravo Charlie", deadline=10, party=party)
    else:
        with pytest.raises(pytest.fail.Exception):
            voice._wait_for_hosted_readback(client, "call", "Alpha Bravo Charlie", deadline=10, party=party)


def test_sms_window_exhausts_actual_sdk_pages_and_retains_all_targets(monkeypatch):
    from uuid import UUID
    from inkbox import Inkbox

    client = Inkbox(api_key="offline")
    bound = "2026-08-01T12:00:00+00:00"
    def row(index):
        return dict(id=str(UUID(int=index)), direction="outbound", local_phone_number="+15551112222",
                    remote_phone_number="+15553334444" if index == 201 else "+15555556666",
                    text="Alpha Bravo Charlie" if index == 201 else "Other", type="sms", is_read=True,
                    created_at=bound, updated_at=bound)
    rows = [row(index) for index in range(1, 202)]
    requests = []
    def get(path, *, params):
        requests.append(dict(params))
        assert params["start_datetime"] == bound
        return rows[params["offset"]:params["offset"] + params["limit"]]
    monkeypatch.setattr(client.texts._http, "get", get)
    baseline = voice._outbound_sms_since(client, "number", bound)
    before = {message.id for message in baseline}
    rows.insert(0, row(202))
    current = voice._outbound_sms_since(client, "number", bound)
    assert len(baseline) == 201 and len(current) == 202
    assert {message.id for message in current} - before == {UUID(int=202)}
    assert current[-1].remote_phone_number == "+15553334444"
    assert [request["offset"] for request in requests] == [0, 200, 0, 200]
    assert all(request["start_datetime"] == bound for request in requests)
