"""The plugin identifies itself and its version in the SDK User-Agent."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import config as config_mod


def test_user_agent_names_the_plugin_and_its_version():
    config_mod.plugin_user_agent.cache_clear()

    ua = config_mod.plugin_user_agent()

    assert ua.startswith("inkbox-hermes/")
    assert ua.split("/", 1)[1]


def test_client_kwargs_carry_the_prefix():
    kwargs = config_mod.inkbox_client_kwargs("ak_test")

    assert kwargs["api_key"] == "ak_test"
    assert kwargs["user_agent_prefix"] == config_mod.plugin_user_agent()


def test_unknown_distribution_still_yields_a_token(monkeypatch):
    import importlib.metadata

    def _missing(_name):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", _missing)
    config_mod.plugin_user_agent.cache_clear()

    assert config_mod.plugin_user_agent() == "inkbox-hermes/unknown"

    config_mod.plugin_user_agent.cache_clear()
