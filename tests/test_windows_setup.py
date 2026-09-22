"""Native PowerShell setup commands; run on the Windows CI runner."""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import setup_wizard

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows")


def test_windows_installer_targets_host_python_and_fixed_version(monkeypatch):
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _: None)
    monkeypatch.setattr(setup_wizard.sys, "executable", r"C:\Users\Example User\Hermes\python.exe")
    command = setup_wizard._install_commands()[0][0]
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert f"inkbox>={setup_wizard.INKBOX_MIN_VERSION},<1.0.0" in command
    assert setup_wizard._install_command_text().startswith("& 'C:\\Users\\Example User\\Hermes\\python.exe'")
