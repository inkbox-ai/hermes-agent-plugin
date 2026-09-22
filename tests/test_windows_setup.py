"""Native-Windows setup and readiness checks; run on the Windows CI runner."""

import asyncio
import importlib.metadata
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter, bootstrap, diagnostics, doctor, setup_wizard
from inkbox_plugin.config import set_runtime_config_extra

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("INKBOX_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    set_runtime_config_extra({})
    yield
    set_runtime_config_extra({})


@pytest.mark.parametrize("version", ["0.7.1", "0.7.3", "0.7.4rc1"])
def test_old_sdk_explains_outbound_vs_inbound_and_preserves_identity(monkeypatch, version):
    monkeypatch.setattr(diagnostics.importlib.metadata, "version", lambda _: version)
    issue = diagnostics.windows_tunnel_issue()
    assert "cannot open the built-in tunnel" in issue
    assert "existing identity and API key" in issue
    assert "hermes inkbox setup" in issue
    assert diagnostics.windows_tunnel_issue("https://receiver.example") is None


def test_fixed_sdk_is_accepted(monkeypatch):
    monkeypatch.setattr(diagnostics.importlib.metadata, "version", lambda _: "0.7.4")
    assert diagnostics.windows_tunnel_issue() is None


def test_missing_sdk_requires_upgrade(monkeypatch):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError("inkbox")

    monkeypatch.setattr(diagnostics.importlib.metadata, "version", missing)
    assert "installed: unknown" in diagnostics.windows_tunnel_issue()


def test_windows_installer_targets_host_python_and_fixed_version(monkeypatch):
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _: None)
    monkeypatch.setattr(setup_wizard.sys, "executable", r"C:\Users\Example User\Hermes\python.exe")
    command = setup_wizard._install_commands()[0][0]
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert f"inkbox>={diagnostics.INKBOX_MIN_VERSION},<1.0.0" in command
    assert setup_wizard._install_command_text().startswith("& 'C:\\Users\\Example User\\Hermes\\python.exe'")


def test_public_receiver_bypasses_tunnel_check_but_keeps_setup_minimum(monkeypatch):
    monkeypatch.setenv("INKBOX_PUBLIC_URL", "https://receiver.example")
    monkeypatch.setattr(setup_wizard.importlib.metadata, "version", lambda _: "0.7.1")
    assert diagnostics.windows_tunnel_issue("https://receiver.example") is None
    assert not setup_wizard._inkbox_version_ok()


def test_doctor_reports_old_windows_runtime_before_api_check(monkeypatch):
    monkeypatch.setattr(diagnostics.importlib.metadata, "version", lambda _: "0.7.1")
    result = doctor.run_doctor()
    assert not result["ok"]
    assert any(f["id"] == "inkbox/windows-tunnel-sdk-upgrade" for f in result["findings"])


def test_gateway_rejects_old_sdk_before_locks_or_remote_mutations(monkeypatch, caplog):
    monkeypatch.setattr(diagnostics.importlib.metadata, "version", lambda _: "0.7.1")
    monkeypatch.setattr(adapter, "check_inkbox_requirements", lambda: True)
    # Only the preflight fields are needed: accessing anything else would mean
    # startup crossed the unsupported-runtime boundary.
    gateway = object.__new__(adapter.InkboxAdapter)
    gateway._voice_stack_invalid_value = ""
    gateway._api_key = "example-key"
    gateway._identity_handle = "example-agent"
    gateway._require_signature = True
    gateway._signing_key = "example-signing-key"
    gateway._public_url_override = ""
    assert not asyncio.run(gateway.connect())
    assert "Native Windows inbound delivery" in caplog.text


def test_bootstrap_rejects_old_sdk_before_saving_or_rotating_credentials(monkeypatch):
    monkeypatch.setattr(diagnostics.importlib.metadata, "version", lambda _: "0.7.1")
    monkeypatch.setattr(bootstrap, "_load_inkbox_symbols", lambda: pytest.fail("must not reach provisioning"))
    result = bootstrap.bootstrap(identity_handle="example-agent", api_key="example-key", start_gateway=True)
    assert result["status"] == "error"
    assert "Native Windows inbound delivery" in result["error"]


def test_setup_rechecks_version_after_install(monkeypatch):
    monkeypatch.setattr(setup_wizard.importlib.metadata, "version", lambda _: "0.7.1")
    monkeypatch.setattr(setup_wizard, "_load_inkbox_symbols", lambda: {"Inkbox": object()})
    monkeypatch.setattr(setup_wizard, "_is_interactive_stdin", lambda: True)
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_: True)
    monkeypatch.setattr(setup_wizard, "_run_install_plan", lambda: True)
    monkeypatch.setattr(setup_wizard, "_purge_inkbox_modules", lambda: None)
    assert setup_wizard._ensure_inkbox_sdk() is None


@pytest.mark.parametrize("platform_config", [
    {"public_url": "https://receiver.example"},
    {"publicUrl": "https://receiver.example"},
    {"extra": {"public_url": "https://receiver.example"}},
])
def test_yaml_receiver_is_resolved_without_gateway_startup(monkeypatch, platform_config):
    host_config = types.ModuleType("hermes_cli.config")
    host_config.load_config = lambda: {"platforms": {"inkbox": platform_config}}
    monkeypatch.setitem(sys.modules, "hermes_cli.config", host_config)
    monkeypatch.setattr(diagnostics.importlib.metadata, "version", lambda _: "0.7.1")
    assert not setup_wizard._inkbox_version_ok()
    assert not any(f["id"] == "inkbox/windows-tunnel-sdk-upgrade" for f in doctor.run_doctor()["findings"])
