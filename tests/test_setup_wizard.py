import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import setup_wizard


def test_install_command_prefers_uv_when_available(monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "executable", "/tmp/hermes/venv/bin/python")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)

    assert setup_wizard._install_commands()[0] == [[
        "/bin/uv",
        "pip",
        "install",
        "--python",
        "/tmp/hermes/venv/bin/python",
        "inkbox>=0.4.6",
        "aiohttp>=3.9",
    ]]


def test_install_command_falls_back_to_pip_and_ensurepip(monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "executable", "/tmp/hermes/venv/bin/python")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _name: None)

    assert setup_wizard._install_commands() == [
        [["/tmp/hermes/venv/bin/python", "-m", "pip", "install", "inkbox>=0.4.6", "aiohttp>=3.9"]],
        [
            ["/tmp/hermes/venv/bin/python", "-m", "ensurepip", "--upgrade"],
            ["/tmp/hermes/venv/bin/python", "-m", "pip", "install", "inkbox>=0.4.6", "aiohttp>=3.9"],
        ],
    ]


def test_missing_sdk_guidance_prints_hermes_python(monkeypatch, capsys):
    def fail_import():
        raise ImportError("No module named 'inkbox'")

    monkeypatch.setattr(setup_wizard, "_load_inkbox_symbols", fail_import)
    monkeypatch.setattr(setup_wizard, "_is_interactive_stdin", lambda: False)
    monkeypatch.setattr(setup_wizard.sys, "executable", "/tmp/hermes/venv/bin/python")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)

    assert setup_wizard._ensure_inkbox_sdk() is None

    out = capsys.readouterr().out
    assert "/tmp/hermes/venv/bin/python" in out
    assert "uv pip install --python" in out
    assert "inkbox>=0.4.6" in out
    assert "aiohttp>=3.9" in out


def test_plugin_install_does_not_prompt_for_inkbox_env():
    text = (ROOT / "plugin.yaml").read_text()

    assert "requires_env: []" in text
    assert "name: INKBOX_API_KEY" in text
    assert "name: INKBOX_IDENTITY" in text
