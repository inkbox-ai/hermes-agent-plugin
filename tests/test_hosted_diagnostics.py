import importlib.util
import json
import os
from pathlib import Path
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hosted_diagnostics_under_test", ROOT / "" / "hosted_diagnostics.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def report(tmp_path, monkeypatch):
    path = tmp_path / "diagnostics.json"
    value = {"version": 1, "identity": "example", "harness": "hermes", "pid": os.getpid(),
             "observed_at": time.time(), "ready": True, "signing_key_configured": True}
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    monkeypatch.setenv("INKBOX_HOSTED_MODE", "1")
    monkeypatch.setenv("INKBOX_DIAGNOSTIC_CONTEXT", "terminal")
    monkeypatch.setenv("INKBOX_DIAGNOSTICS_FILE", str(path))
    return path, value


def test_terminal_uses_runtime_health_without_receiving_the_key(report):
    result = module.hosted_diagnostics("@example", "hermes", None)
    assert result["ready"]
    assert "subscriptions are not required" in result["checks"][0][2]


@pytest.mark.parametrize("update", [{"ready": False}, {"signing_key_configured": False},
    {"observed_at": 1}, {"observed_at": time.time() + 3600}, {"identity": "other"},
    {"harness": "other"}, {"pid": -1}, {"ready": "true"}, {"version": 2}])
def test_unhealthy_stale_or_mismatched_report_never_passes(report, update):
    path, value = report
    path.write_text(json.dumps(value | update))
    assert not module.hosted_diagnostics("example", "hermes", None)["ready"]


@pytest.mark.parametrize("kind", ["missing", "malformed", "public", "symlink", "oversized"])
def test_unavailable_report_does_not_claim_readiness(report, kind):
    path, _ = report
    if kind == "missing":
        path.unlink()
    elif kind == "malformed":
        path.write_text("private invalid payload")
    elif kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        other = path.with_suffix(".saved")
        path.rename(other)
        path.symlink_to(other)
    else:
        path.write_text("x" * 4097)
    result = module.hosted_diagnostics("example", "hermes", None)
    assert not result["ready"]
    assert "private invalid payload" not in json.dumps(result)


def test_runtime_still_requires_its_actual_signing_key(report, monkeypatch):
    monkeypatch.setenv("INKBOX_DIAGNOSTIC_CONTEXT", "runtime")
    assert not module.hosted_diagnostics("example", "hermes", None)["ready"]
    assert module.hosted_diagnostics("example", "hermes", "synthetic")["ready"]


def test_external_mode_keeps_existing_checks_even_with_a_report(report, monkeypatch):
    monkeypatch.delenv("INKBOX_HOSTED_MODE")
    assert module.hosted_diagnostics("example", "hermes", None) is None


def test_doctor_entrypoint_uses_hosted_report_before_standalone_checks(report, monkeypatch):
    import importlib
    from types import SimpleNamespace
    import sys
    import types
    package = types.ModuleType("inkbox_plugin")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "inkbox_plugin", package)
    doctor = importlib.import_module("inkbox_plugin.doctor")
    monkeypatch.setattr(doctor, "read_runtime_config", lambda: SimpleNamespace(identity="example", signing_key=None))
    result = doctor.run_doctor()
    assert result["ready"] and not result["findings"]
