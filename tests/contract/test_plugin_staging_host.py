"""Current native package-manager staging must not recurse into its source."""

import tomllib

import pytest

pytest.importorskip("hermes_cli.plugins")

from pm.workspace import _workspace_member
from tests.ci.stage_hermes_plugin import stage_plugin
from tests.fixtures.plugin_checkout import plugin_checkout


def test_committed_plugin_materializes_in_native_managed_workspace(tmp_path):
    source, runtime = plugin_checkout(tmp_path)
    staged = stage_plugin(source, runtime, tmp_path / "runner-temp")
    workspace = runtime / "installs" / "generation" / "workspace"
    workspace.mkdir(parents=True)
    member = _workspace_member(staged, workspace, identity=staged)
    assert member.is_relative_to(workspace / "plugin-sources")
    assert not workspace.is_relative_to(staged)
    assert (member / "__init__.py").read_text() == 'VALUE = "committed"\n'
    assert (member / "plugin.yaml").read_bytes() == (staged / "plugin.yaml").read_bytes()
    assert {path.name for path in member.iterdir()} == {"plugin.yaml", "pyproject.toml", "__init__.py"}
    declared = tomllib.loads((staged / "pyproject.toml").read_text())
    admitted = tomllib.loads((member / "pyproject.toml").read_text())
    assert admitted["project"]["dependencies"] == declared["project"]["dependencies"]
    assert admitted["tool"]["uv"]["package"] is False
    assert (runtime / ".env").read_text() == "SYNTHETIC_PRIVATE_RUNTIME=must-not-copy\n"
