import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_plugin_does_not_import_removed_hermes_modules():
    removed_modules = {"gateway.status"}
    violations = []
    plugin_sources = [*ROOT.glob("*.py"), *(ROOT / "webhook_providers").glob("*.py")]

    for path in plugin_sources:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module in removed_modules:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in removed_modules:
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {alias.name}")

    assert violations == []


@pytest.mark.parametrize(
    "workflow_name",
    ["live-channels.yml", "live-voice.yml", "live-external-events.yml"],
)
def test_live_workflow_uses_isolated_pytest_entrypoint(workflow_name):
    workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text()

    assert 'PYTEST="$HERMES_HOME/hermes-agent/venv/bin/pytest"' in workflow
    assert '"$PY" -m pytest' not in workflow


@pytest.mark.parametrize(
    "workflow_name",
    [
        "live-a2a.yml",
        "live-channels.yml",
        "live-external-events.yml",
        "live-voice.yml",
    ],
)
def test_agent_capable_live_workflow_disables_voicemail_detection(workflow_name):
    workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text()

    assert "INKBOX_VOICEMAIL_DETECTION=disabled" in workflow
