from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scheduled_failures_use_one_verified_reporting_path():
    canary = (ROOT / ".github/workflows/canary.yml").read_text()
    stack = (ROOT / ".github/workflows/live-stack.yml").read_text()
    report = (ROOT / ".github/workflows/scheduled-failure-report.yml").read_text()

    assert "workflow_call:" in canary
    assert "schedule:" not in canary
    assert "notify:" not in canary
    assert "schedule:" in stack
    assert "uses: ./.github/workflows/canary.yml" in stack
    assert "workflow_run:" not in stack
    assert "notify:" not in stack
    assert 'workflows: ["Full stack e2e"]' in report
    assert "github.event.workflow_run.event == 'schedule'" in report
    assert "failure" in report and "timed_out" in report and "startup_failure" in report
    assert "if: always()" in report
    assert "actions: read" in report
    assert "X-Hub-Signature-256" in report
    assert "chat_thread_key" in report
