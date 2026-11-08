import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.ci.summarize_live_failure import summarize


ROOT = Path(__file__).resolve().parents[1]


def test_cli_only_emits_counts_for_sensitive_log_content(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_text("ERROR secret=synthetic-credential\nWARNING private message body\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/ci/summarize_live_failure.py"), "gateway", str(log)],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == {
        "gateway": {"present": True, "line_count": 2, "error_lines": 1, "warning_lines": 1},
    }
    assert result.stderr == ""


def test_missing_log_still_produces_a_summary(tmp_path):
    assert summarize(tmp_path / "missing.log") == {
        "present": False, "line_count": 0, "error_lines": 0, "warning_lines": 0,
    }


@pytest.mark.parametrize("name", ["a2a", "channels", "voice", "external-events"])
def test_live_workflows_do_not_publish_raw_logs(name):
    workflow = (ROOT / ".github/workflows" / f"live-{name}.yml").read_text()
    assert "summarize_live_failure.py" in workflow
    assert "actions/upload-artifact" not in workflow
    for line in workflow.splitlines():
        if "cat " in line or "tail " in line:
            assert not any(value in line for value in ("$GATEWAY_LOG", "$DRIVER_LOG", "$DRIVER_STATE", "/logs/", "/mock.log"))
