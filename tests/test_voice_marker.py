from pathlib import Path

from tests.live.voice_marker import marker_from_token


ROOT = Path(__file__).resolve().parents[1]
SPEECH_WORDS = {
    "banana",
    "elephant",
    "pineapple",
    "alligator",
    "motorcycle",
    "umbrella",
    "dinosaur",
    "potato",
    "computer",
    "volcano",
    "airplane",
    "butterfly",
    "kangaroo",
    "octopus",
    "calendar",
    "chocolate",
    "hospital",
    "library",
    "sandwich",
    "telescope",
}


def _marker(token: str) -> list[str]:
    return marker_from_token(token).split()


def test_live_voice_marker_is_deterministic_distinct_and_speech_safe():
    for run_number in range(100):
        token = f"31196027320-{run_number}"
        words = _marker(token)

        assert words == _marker(token)
        assert len(words) == 3
        assert len(set(words)) == len(words)
        assert set(words) <= SPEECH_WORDS


def test_live_voice_marker_mapping_is_stable():
    words = _marker("31196027320-1")

    assert words == ["sandwich", "octopus", "umbrella"]


def test_live_voice_marker_uses_substantially_more_than_trailing_id_digits():
    markers = {tuple(_marker(f"3119602{run_number:04d}-1")) for run_number in range(1_000)}

    assert len(markers) >= 850


def test_hosted_voice_workflow_keeps_peer_alive_for_test_owned_hangup():
    workflow = (ROOT / ".github" / "workflows" / "live-voice.yml").read_text()
    live_test = (ROOT / "tests" / "live" / "test_voice.py").read_text()
    hosted_proof = live_test.split("def test_outbound_call_inkbox_voice_ai_and_completion():", 1)[1].split(
        "# Fixed identifiers for the mid-call contact-lookup leg.", 1
    )[0]

    assert 'HOSTED_MARKER="$("$PY" "$GITHUB_WORKSPACE/tests/live/voice_marker.py" "$RUN_TOKEN")"' in workflow
    assert 'RUN_TOKEN="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in workflow
    assert (
        'export VOICE_DRIVER_LINE="After we hang up, send me one SMS with this exact '
        'three-word body: $HOSTED_MARKER. Record that post-call SMS action now. Once '
        'the action tool succeeds, read the exact three words back to me. Do not send '
        'the SMS during the call."'
        in workflow
    )
    assert "export VOICE_DRIVER_LISTEN=180" in workflow
    assert "export VOICE_DRIVER_LISTEN=45" not in workflow
    assert hosted_proof.index("_wait_for_hosted_request(") < hosted_proof.index("_wait_for_hosted_action(")
    assert hosted_proof.index("_wait_for_hosted_action(") < hosted_proof.index("finally:")
    assert hosted_proof.index("finally:") < hosted_proof.index("_hangup_fresh_calls(")
