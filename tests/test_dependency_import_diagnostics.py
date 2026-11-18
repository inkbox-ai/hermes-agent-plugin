import builtins
import importlib.util
import logging
from pathlib import Path

import pytest


@pytest.mark.parametrize("dependency", ["inkbox", "aiohttp"])
def test_adapter_records_import_failure_without_exception_payload(dependency, monkeypatch, caplog):
    original = builtins.__import__
    intercepted = False

    def fail_once(name, globals=None, locals=None, fromlist=(), level=0):
        nonlocal intercepted
        expected = "Inkbox" if dependency == "inkbox" else "WSMsgType"
        if name == dependency and expected in fromlist and not intercepted:
            intercepted = True
            raise ImportError("partially initialized module; synthetic-secret", name=dependency, path="/private/secret")
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_once)
    spec = importlib.util.spec_from_file_location("inkbox_plugin.adapter_import_probe", Path(__file__).parents[1] / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    with caplog.at_level(logging.WARNING):
        spec.loader.exec_module(module)
    assert intercepted and not module.check_inkbox_requirements()
    assert f"dependency={dependency}" in caplog.text
    assert f"module={dependency}" in caplog.text
    assert "partially_initialized=True" in caplog.text
    assert "synthetic-secret" not in caplog.text and "/private/secret" not in caplog.text
    assert original(dependency)


def test_import_diagnostic_rejects_non_module_names(caplog):
    from inkbox_plugin.adapter import _dependency_import_failure

    with caplog.at_level(logging.WARNING):
        _dependency_import_failure("inkbox", ModuleNotFoundError("synthetic-secret", name="https://secret.example/key"))
    assert "category=module_not_found module=unknown" in caplog.text
    assert "secret" not in caplog.text
