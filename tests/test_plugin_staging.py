"""Native plugin admission receives an isolated committed source tree."""

from pathlib import Path
import subprocess
import sys

import pytest

from tests.ci.stage_hermes_plugin import stage_plugin
from tests.fixtures.plugin_checkout import plugin_checkout


ROOT = Path(__file__).resolve().parents[1]


def test_archive_uses_head_excludes_private_runtime_and_isolates_attempts(tmp_path):
    source, runtime = plugin_checkout(tmp_path)
    temp_root = tmp_path / "runner-temp"
    first = stage_plugin(source, runtime, temp_root)
    second = stage_plugin(source, runtime, temp_root)
    assert first != second
    for staged in (first, second):
        assert not staged.is_relative_to(source)
        assert not staged.is_relative_to(runtime)
        assert {path.name for path in staged.iterdir()} == {"plugin.yaml", "pyproject.toml", "__init__.py"}
        assert (staged / "__init__.py").read_text() == 'VALUE = "committed"\n'
    assert (source / "__init__.py").read_text() == 'VALUE = "uncommitted"\n'
    assert (runtime / ".env").is_file()


@pytest.mark.parametrize("nested", ["checkout", "runtime"])
def test_rejects_staging_inside_source_or_managed_home_before_writing(tmp_path, nested):
    source, runtime = plugin_checkout(tmp_path)
    root = (source if nested == "checkout" else runtime) / "staging"
    with pytest.raises(ValueError, match="outside the checkout and Hermes home"):
        stage_plugin(source, runtime, root)
    assert not root.exists()


def test_archive_failure_cleans_only_its_own_stage(tmp_path):
    source = tmp_path / "not-a-repository"
    source.mkdir()
    temp_root = tmp_path / "runner-temp"
    temp_root.mkdir()
    unrelated = temp_root / "unrelated"
    unrelated.write_text("preserve")
    with pytest.raises(subprocess.CalledProcessError):
        stage_plugin(source, tmp_path / "runtime", temp_root)
    assert list(temp_root.iterdir()) == [unrelated]
    assert unrelated.read_text() == "preserve"


def test_cli_returns_only_the_staged_source_path(tmp_path):
    source, runtime = plugin_checkout(tmp_path)
    result = subprocess.run([
        sys.executable, str(ROOT / "tests/ci/stage_hermes_plugin.py"),
        "--source", str(source), "--runtime-home", str(runtime),
        "--temp-root", str(tmp_path / "runner-temp"),
    ], check=True, capture_output=True, text=True)
    assert len(result.stdout.splitlines()) == 1
    assert Path(result.stdout.strip()).joinpath("plugin.yaml").is_file()
    assert not result.stderr


@pytest.mark.parametrize("name", ["live-a2a.yml", "live-channels.yml", "live-external-events.yml", "live-voice.yml"])
def test_every_live_workflow_stages_before_native_admission(name):
    workflow = (ROOT / ".github/workflows" / name).read_text()
    stage = workflow.index('"$GITHUB_WORKSPACE/tests/ci/stage_hermes_plugin.py"')
    enable = workflow.index("hermes plugins enable inkbox")
    restrict = workflow.index('"$GITHUB_WORKSPACE/tests/ci/restrict_hermes_tools.py"')
    assert stage < enable < restrict
    assert '--source "$GITHUB_WORKSPACE" --runtime-home "$HERMES_HOME" --temp-root "$RUNNER_TEMP"' in workflow
    assert 'ln -sfn "$PLUGIN_SOURCE" "$HERMES_HOME/plugins/inkbox"' in workflow
    assert 'ln -sfn "$GITHUB_WORKSPACE"' not in workflow
