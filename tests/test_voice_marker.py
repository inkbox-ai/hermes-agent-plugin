from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MARKER_HELPER = ROOT / "tests" / "live" / "voice_marker.py"
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
    result = subprocess.run(
        [sys.executable, str(MARKER_HELPER), token],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().split()


def test_live_voice_marker_is_deterministic_distinct_and_speech_safe():
    for digit in range(10):
        token = str(digit) * 5
        words = _marker(token)

        assert words == _marker(token)
        assert len(words) == 5
        assert len(set(words)) == len(words)
        assert set(words) <= SPEECH_WORDS


def test_live_voice_marker_mapping_is_stable():
    words = _marker("55071")

    assert words == ["umbrella", "chocolate", "banana", "library", "elephant"]


def test_hosted_voice_workflow_keeps_peer_alive_for_test_owned_hangup():
    workflow = (ROOT / ".github" / "workflows" / "live-voice.yml").read_text()
    live_test = (ROOT / "tests" / "live" / "test_voice.py").read_text()
    hosted_proof = live_test.split("def test_outbound_call_inkbox_voice_ai_and_completion():", 1)[1].split(
        "# Fixed identifiers for the mid-call contact-lookup leg.", 1
    )[0]

    assert 'HOSTED_MARKER="$("$PY" "$GITHUB_WORKSPACE/tests/live/voice_marker.py" "$DIGITS")"' in workflow
    assert 'DIGITS="${GITHUB_RUN_ID: -4}${GITHUB_RUN_ATTEMPT: -1}"' in workflow
    assert (
        'export VOICE_DRIVER_LINE="Create one post-call action now with both its title '
        'and details exactly: Send SMS $HOSTED_MARKER. Then list the actions. If either '
        'field lacks that exact phrase, edit that same action until both match. Only '
        'then read the five-word body back. After we hang up, send one SMS containing '
        'exactly $HOSTED_MARKER. Do not send it during the call."'
        in workflow
    )
    assert "export VOICE_DRIVER_LISTEN=180" in workflow
    assert "export VOICE_DRIVER_LISTEN=45" not in workflow
    assert hosted_proof.index("_wait_for_hosted_request(") < hosted_proof.index("_wait_for_hosted_action(")
    assert hosted_proof.index("_wait_for_hosted_action(") < hosted_proof.index("finally:")
    assert hosted_proof.index("finally:") < hosted_proof.index("_hangup_call(")
