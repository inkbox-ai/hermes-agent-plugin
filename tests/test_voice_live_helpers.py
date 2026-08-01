from __future__ import annotations

import importlib.util
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


def test_delivery_status_accepts_sdk_enum_shape() -> None:
    message = SimpleNamespace(
        delivery_status=SimpleNamespace(value="delivered"),
    )

    assert voice._delivery_status(message) == "delivered"
