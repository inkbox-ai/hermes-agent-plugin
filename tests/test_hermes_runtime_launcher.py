"""CI cannot bypass native admission or assume a legacy dependency directory."""
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from tests.ci.hermes_python import native_command
from tests.ci import check_hermes_runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('source, expected', [
    (None, 'inkbox==0.1.0'),
    ({'url': 'https://example.test/sdk.git', 'vcs_info': {'vcs': 'git', 'commit_id': 'a' * 40},
      'subdirectory': 'sdk/python'}, 'inkbox @ git+https://example.test/sdk.git@' + 'a' * 40 + '#subdirectory=sdk/python'),
])
def test_independent_runner_reproduces_admitted_sdk_source(monkeypatch, source, expected):
    package = SimpleNamespace(version='0.1.0', read_text=lambda name: json.dumps(source) if source else None)
    monkeypatch.setattr(check_hermes_runtime, 'distribution', lambda name: package)
    assert check_hermes_runtime.admitted_sdk_requirement() == expected


def test_independent_runner_rejects_unreproducible_local_install(monkeypatch):
    package = SimpleNamespace(version='0.1.0', read_text=lambda name: '{"url":"file:///tmp/sdk"}')
    monkeypatch.setattr(check_hermes_runtime, 'distribution', lambda name: package)
    with pytest.raises(RuntimeError, match='not reproducible'):
        check_hermes_runtime.admitted_sdk_requirement()


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
    assert "pytest pyyaml fastapi 'uvicorn[standard]'" in workflow
    assert 'python3 "$GITHUB_WORKSPACE/tests/ci/hermes_python.py" \\\n              "$GITHUB_WORKSPACE/tests/ci/check_gateway_ready.py"' in workflow


def test_native_gateway_keeps_recognized_argv_and_spy_stays_private():
    workflow = (ROOT / '.github/workflows/live-channels.yml').read_text()
    assert 'hermes gateway run > "$GATEWAY_LOG" 2>&1 &' in workflow
    assert 'PYTHONPATH=' not in workflow
    assert 'cat "$SPY_FILE"' not in workflow
    assert '${{ runner.temp }}/send_intents.jsonl' in workflow  # test input remains enabled
    assert 'actions/upload-artifact' not in workflow


@pytest.mark.parametrize('name', ['live-voice.yml', 'tests.yml'])
def test_actual_voice_media_probe_uses_the_independent_runner(name):
    workflow = (ROOT / '.github/workflows' / name).read_text()
    assert "pytest pyyaml fastapi 'uvicorn[standard]'" in workflow
    probe = ('"$RUNNER_TEMP/inkbox-live-runner/bin/python" \\\n'
             '            "$GITHUB_WORKSPACE/tests/ci/check_voice_driver_ws.py"')
    assert probe in workflow
    assert workflow.index("'uvicorn[standard]'") < workflow.index(probe)
    if name == 'live-voice.yml':
        assert workflow.index(probe) < workflow.index('- name: Start voice driver')
    else:
        assert workflow.index(probe) < workflow.index('tests/ci/check_native_install.py')
