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
        assert '"$GITHUB_WORKSPACE/tests/ci/resolve_inkbox_identity.py"' in workflow
        assert 'os.environ["HERMES_INKBOX_API_KEY"]' not in workflow
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


def test_security_scan_uses_only_the_tracked_plugin_snapshot():
    for name in ("canary.yml", "tests.yml"):
        workflow = ROOT.joinpath(".github", "workflows", name).read_text()
        assert 'git -C "$GITHUB_WORKSPACE" archive HEAD' in workflow
        assert '"$RUNNER_TEMP/plugin-scan"' in workflow


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


def test_live_identity_resolution_uses_the_configured_api(monkeypatch):
    from tests.ci.resolve_inkbox_identity import resolve_identity

    calls = []

    class FakeMailboxes:
        @staticmethod
        def list():
            return [type("Mailbox", (), {"email_address": "ci-agent@example.com"})()]

    class FakeClient:
        mailboxes = FakeMailboxes()

    def fake_client_factory(**kwargs):
        calls.append(kwargs)
        return FakeClient()

    monkeypatch.setenv("HERMES_INKBOX_API_KEY", "test-key")
    monkeypatch.setenv("INKBOX_BASE_URL", "https://example.com")

    assert resolve_identity(fake_client_factory) == "ci-agent"
    assert calls == [{"api_key": "test-key", "base_url": "https://example.com"}]


def test_installer_retries_a_hung_attempt_without_waiting_for_job_timeout(tmp_path):
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('''#!/bin/bash
cat > "$RUNNER_TEMP/hermes-install.sh" <<'INSTALL'
#!/bin/bash
if [ ! -e "$RUNNER_TEMP/first-attempt" ]; then
  touch "$RUNNER_TEMP/first-attempt"
  /bin/sleep 30
fi
INSTALL
''')
    curl.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/bash\nexit 0\n")
    sleep.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "tests/ci/install_hermes.sh"), "--skip-browser", "--no-skills"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path),
             "HERMES_INSTALL_TIMEOUT": "0.1", "HERMES_INSTALL_ATTEMPTS": "2"},
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "attempt 1 failed; retrying" in result.stdout
