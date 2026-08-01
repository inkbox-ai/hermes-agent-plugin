from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MARKER_HELPER = ROOT / "tests" / "live" / "voice_marker.py"
NATO_WORDS = {
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliett",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
}


def _marker(token: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, str(MARKER_HELPER), token],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().split()


def test_live_voice_marker_is_deterministic_distinct_and_nato_only():
    words = _marker("55071")

    assert words == _marker("55071")
    assert len(words) == 5
    assert len(set(words)) == len(words)
    assert set(words) <= NATO_WORDS


def test_hosted_voice_workflow_keeps_peer_alive_for_test_owned_hangup():
    workflow = (ROOT / ".github" / "workflows" / "live-voice.yml").read_text()
    live_test = (ROOT / "tests" / "live" / "test_voice.py").read_text()
    hosted_proof = live_test.split("def test_outbound_call_inkbox_voice_ai_and_completion():", 1)[1].split(
        "# Fixed identifiers for the mid-call contact-lookup leg.", 1
    )[0]

    assert 'HOSTED_MARKER="$("$PY" "$GITHUB_WORKSPACE/tests/live/voice_marker.py" "$DIGITS")"' in workflow
    assert 'DIGITS="${GITHUB_RUN_ID: -4}${GITHUB_RUN_ATTEMPT: -1}"' in workflow
    assert (
        'export VOICE_DRIVER_LINE="After we hang up, send me one SMS. '
        'Create the post-call action now with this exact SMS body: $HOSTED_MARKER. '
        'Read those five words back to me after the action is saved. '
        'Do not send it during the call."'
        in workflow
    )
    assert "export VOICE_DRIVER_LISTEN=180" in workflow
    assert "export VOICE_DRIVER_LISTEN=45" not in workflow
    assert hosted_proof.index("_wait_for_hosted_request(") < hosted_proof.index("_wait_for_hosted_action(")
    assert hosted_proof.index("_wait_for_hosted_action(") < hosted_proof.index("finally:")
    assert hosted_proof.index("finally:") < hosted_proof.index("_hangup_call(")
