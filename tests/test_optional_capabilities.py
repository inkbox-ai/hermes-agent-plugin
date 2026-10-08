"""The published SDK floor stays usable; opt-ins require their actual interfaces."""
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from inkbox.agent_identity import AgentIdentity

from inkbox_plugin.config import read_config, set_runtime_config_extra
from inkbox_plugin.extended_tools import register, VAULT_TOOLS, THREAD_TOOLS
from inkbox_plugin.imessage_state import require_threading
from inkbox_plugin.slack_companion import require_sdk_support


def test_catalog_opt_ins_and_runtime_config(monkeypatch):
    monkeypatch.setenv("INKBOX_SLACK_ENABLED", "true")
    monkeypatch.setenv("INKBOX_IMESSAGE_THREADED_REPLIES", "true")
    assert read_config({"slack_enabled": False}).slack_enabled is False
    set_runtime_config_extra({})
    gates = {}
    register(SimpleNamespace(register_tool=lambda name, *args, **kwargs: gates.update({name: kwargs["check_fn"]})), lambda: True)
    names = {name for name, check in gates.items() if check()}
    assert {tool["name"] for tool in VAULT_TOOLS + THREAD_TOOLS} <= set(names)
    assert len([name for name in names if name.startswith("inkbox_slack_")]) == 8
    monkeypatch.setenv("INKBOX_SLACK_ENABLED", "false")
    monkeypatch.setenv("INKBOX_IMESSAGE_THREADED_REPLIES", "false")
    # Native registration precedes YAML loading. The already registered
    # definitions must follow runtime visibility without re-registering.
    names = {name for name, check in gates.items() if check()}
    assert set(names) == {tool["name"] for tool in VAULT_TOOLS}
    set_runtime_config_extra({"slack_enabled": True})
    assert len([name for name, check in gates.items() if name.startswith("inkbox_slack_") and check()]) == 8
    set_runtime_config_extra({})


def test_real_installed_sdk_capability_boundaries():
    installed = tuple(int(part) for part in version("inkbox").split(".")[:3])
    assert installed >= (0, 7, 11)
    # Read class interfaces without creating a network client or identity.
    if installed >= (0, 7, 13):
        require_threading(AgentIdentity)
    else:
        with pytest.raises(RuntimeError, match="0.7.13"):
            require_threading(AgentIdentity)
    if installed >= (0, 7, 14):
        require_sdk_support()
    else:
        with pytest.raises(ValueError, match="0.7.14"):
            require_sdk_support()
