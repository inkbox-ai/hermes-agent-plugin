from pathlib import Path

from tests.ci.restrict_hermes_tools import restrict_inkbox_platform


ROOT = Path(__file__).resolve().parents[1]


def test_every_live_workflow_uses_retrying_installer():
    for name in (
        "live-a2a.yml",
        "live-channels.yml",
        "live-external-events.yml",
        "live-voice.yml",
    ):
        workflow = ROOT.joinpath(".github", "workflows", name).read_text()
        assert 'bash "$GITHUB_WORKSPACE/tests/ci/install_hermes.sh"' in workflow
        assert "--skip-browser --no-skills" in workflow
        assert '"$GITHUB_WORKSPACE/tests/ci/restrict_hermes_tools.py"' in workflow
        assert "hermes-agent.nousresearch.com/install.sh" not in workflow


def test_host_contract_workflows_use_authenticated_checkout():
    for name in ("canary.yml", "tests.yml"):
        workflow = ROOT.joinpath(".github", "workflows", name).read_text()
        assert "repository: NousResearch/hermes-agent" in workflow
        assert "path: .upstream-hermes" in workflow
        assert 'uv pip install --editable "$GITHUB_WORKSPACE/.upstream-hermes"' in workflow
        assert "git clone --depth 1 https://github.com/NousResearch/hermes-agent" not in workflow


def test_live_workflows_precheckout_the_host_and_keep_bounded_install_retries():
    for name in (
        "live-a2a.yml",
        "live-channels.yml",
        "live-external-events.yml",
        "live-voice.yml",
    ):
        workflow = ROOT.joinpath(".github", "workflows", name).read_text()
        assert "repository: NousResearch/hermes-agent" in workflow
        assert "path: .hermes/hermes-agent" in workflow
        assert "fetch-depth: 1" in workflow

    install = ROOT.joinpath("tests", "ci", "install_hermes.sh").read_text()
    assert "HERMES_INSTALL_ATTEMPTS:-4" in install
    assert "attempt * 15" in install
    assert "--retry-all-errors" in install


def test_live_runs_never_cancel_an_existing_shared_cycle():
    workflow = ROOT.joinpath(".github", "workflows", "live-stack.yml").read_text()
    assert "cancel-in-progress: false" in workflow


def test_full_stack_live_validation_runs_for_pull_requests():
    workflow = ROOT.joinpath(".github", "workflows", "live-stack.yml").read_text()
    assert "pull_request:" in workflow
    assert "github.event_name == 'pull_request'" in workflow
    assert "workflow_dispatch:" in workflow
    assert "schedule:" in workflow
    assert "uses: ./.github/workflows/canary.yml" in workflow
    assert "workflow_run:" not in workflow


def test_live_tool_scope_preserves_other_config_and_allows_only_inkbox():
    config = {
        "model": {"default": "test-model"},
        "platform_toolsets": {"cli": ["hermes-cli"]},
    }

    restricted = restrict_inkbox_platform(config)

    assert restricted["model"] == {"default": "test-model"}
    assert restricted["platform_toolsets"]["cli"] == ["hermes-cli"]
    assert restricted["platform_toolsets"]["inkbox"] == ["inkbox", "no_mcp"]
