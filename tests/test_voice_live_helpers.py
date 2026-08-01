from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


def _load_voice_module():
    path = Path(__file__).parent / "live" / "test_voice.py"
    spec = importlib.util.spec_from_file_location("live_voice_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


voice = _load_voice_module()


def test_spoken_marker_normalization_is_case_and_punctuation_insensitive() -> None:
    assert voice._normalized_spoken_text("Alpha, BRAVO! Charlie.") == (
        "alpha bravo charlie"
    )
    assert voice._spoken_marker_key("Alpha X-ray") == "alphaxray"
    assert voice._spoken_marker_key("alpha x ray") == "alphaxray"


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


def test_message_created_at_normalizes_sdk_timestamp_shapes() -> None:
    aware = datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 8, 1, 7, 0)

    assert voice._message_created_at(SimpleNamespace(created_at=aware)) == aware
    assert voice._message_created_at(SimpleNamespace(created_at=naive)) == aware
    assert voice._message_created_at(
        SimpleNamespace(created_at="2026-08-01T07:00:00Z")
    ) == aware
    assert voice._message_created_at(SimpleNamespace(created_at="bad")) is None
