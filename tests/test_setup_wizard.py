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


def test_detect_openai_realtime_key_prefers_plugin_specific_env(monkeypatch):
    values = {
        "OPENAI_API_KEY": "sk-openai",
        "INKBOX_REALTIME_API_KEY": "sk-realtime",
    }
    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_env", lambda name: values.get(name, ""))

    assert setup_wizard._detect_openai_realtime_key() == ("INKBOX_REALTIME_API_KEY", "sk-realtime")


def test_detect_openai_realtime_key_prefers_config(monkeypatch):
    values = {
        "OPENAI_API_KEY": "sk-openai",
        "INKBOX_REALTIME_API_KEY": "sk-realtime",
    }
    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "sk-config")
    monkeypatch.setattr(setup_wizard, "_env", lambda name: values.get(name, ""))

    assert setup_wizard._detect_openai_realtime_key() == (
        "platforms.inkbox.realtime.api_key",
        "sk-config",
    )


def test_configure_realtime_calls_existing_key_success(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []
    tested = []

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_env", lambda name: "sk-existing" if name == "OPENAI_API_KEY" else "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(
        setup_wizard,
        "_test_openai_realtime_api_key",
        lambda key, model: tested.append((key, model)) or (True, "ok"),
    )

    setup_wizard._configure_realtime_calls(identity)

    assert tested == [("sk-existing", "gpt-realtime-2")]
    assert ("INKBOX_REALTIME_ENABLED", "true") in saved
    assert ("INKBOX_REALTIME_MODEL", "gpt-realtime-2") in saved
    assert ("INKBOX_REALTIME_API_KEY", "sk-existing") in saved


def test_configure_realtime_calls_prompts_for_missing_key(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_env", lambda _name: "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "prompt", lambda *_args, **_kwargs: "sk-pasted")
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(setup_wizard, "_test_openai_realtime_api_key", lambda *_args: (True, "ok"))

    setup_wizard._configure_realtime_calls(identity)

    assert ("INKBOX_REALTIME_ENABLED", "true") in saved
    assert ("INKBOX_REALTIME_API_KEY", "sk-pasted") in saved


def test_configure_realtime_calls_validation_failure_disables(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []
    answers = iter([True, False])

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_env", lambda name: "sk-bad" if name == "OPENAI_API_KEY" else "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(
        setup_wizard,
        "_test_openai_realtime_api_key",
        lambda *_args: (False, "OpenAI rejected the key or Realtime permission: HTTP 403"),
    )

    setup_wizard._configure_realtime_calls(identity)

    assert saved == [
        ("INKBOX_REALTIME_ENABLED", "false"),
        ("INKBOX_REALTIME_ENABLED", "false"),
    ]


def test_configure_realtime_calls_retries_after_validation_failure(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []
    tested = []
    answers = iter([True, True])
    keys = iter(["sk-bad", "sk-good"])

    def test_key(key, model):
        tested.append((key, model))
        if key == "sk-good":
            return True, "ok"
        return False, "invalid_api_key: Incorrect API key provided"

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_env", lambda _name: "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(setup_wizard, "prompt", lambda *_args, **_kwargs: next(keys))
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(setup_wizard, "_test_openai_realtime_api_key", test_key)

    setup_wizard._configure_realtime_calls(identity)

    assert tested == [
        ("sk-bad", "gpt-realtime-2"),
        ("sk-good", "gpt-realtime-2"),
    ]
    assert saved == [
        ("INKBOX_REALTIME_ENABLED", "false"),
        ("INKBOX_REALTIME_ENABLED", "true"),
        ("INKBOX_REALTIME_MODEL", "gpt-realtime-2"),
        ("INKBOX_REALTIME_API_KEY", "sk-good"),
    ]


def test_configure_realtime_calls_without_phone_skips(monkeypatch):
    saved = []
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))

    setup_wizard._configure_realtime_calls(types.SimpleNamespace(phone_number=None))

    assert saved == []
