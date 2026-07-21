from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "workflow_name",
    ["live-channels.yml", "live-voice.yml", "live-external-events.yml"],
)
def test_live_workflow_uses_isolated_pytest_entrypoint(workflow_name):
    workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text()

    assert 'PYTEST="$HERMES_HOME/hermes-agent/venv/bin/pytest"' in workflow
    assert '"$PY" -m pytest' not in workflow
