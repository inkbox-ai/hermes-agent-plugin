"""CI cannot bypass native admission or assume a legacy dependency directory."""
from pathlib import Path

import pytest

from tests.ci.hermes_python import native_command

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('args', [[], ['-m']])
def test_launcher_rejects_missing_entrypoint_before_inspecting_host(tmp_path, args):
    with pytest.raises(ValueError, match='script entrypoint'):
        native_command(tmp_path, args)


@pytest.mark.parametrize('name', ['a2a', 'channels', 'voice', 'external-events'])
def test_live_setup_does_not_mutate_or_cache_managed_generations(name):
    workflow = (ROOT / '.github/workflows' / f'live-{name}.yml').read_text()
    assert 'hermes-agent/venv/' not in workflow
    assert '$HERMES_HOME/bin/uv' not in workflow
    assert 'hermes-ci-bin' not in workflow
    assert 'hermes gateway run > "$GATEWAY_LOG" 2>&1 &' in workflow
    assert 'astral-sh/setup-uv@v8.1.0' in workflow
    assert 'echo "$HERMES_HOME/hermes-agent/.hermes/bin" >> "$GITHUB_PATH"' in workflow
    assert workflow.index('hermes plugins enable inkbox') < workflow.index('tests/ci/check_hermes_runtime.py')
    assert 'uv pip install --python "$RUNNER_TEMP/inkbox-live-runner/bin/python"' in workflow
    assert '"$SDK_REQUIREMENT"' in workflow
    assert 'python3 "$GITHUB_WORKSPACE/tests/ci/hermes_python.py" \\\n              "$GITHUB_WORKSPACE/tests/ci/check_gateway_ready.py"' in workflow


def test_native_gateway_keeps_recognized_argv_and_spy_stays_private():
    workflow = (ROOT / '.github/workflows/live-channels.yml').read_text()
    assert 'hermes gateway run > "$GATEWAY_LOG" 2>&1 &' in workflow
    assert 'PYTHONPATH=' not in workflow
    assert 'cat "$SPY_FILE"' not in workflow
    assert '${{ runner.temp }}/send_intents.jsonl' in workflow  # test input remains enabled
    upload = workflow.split('- name: Upload artifacts', 1)[1]
    assert 'send_intents.jsonl' not in upload
