"""Capability changes rebuild an existing agent through the pinned host's registry."""
import sys
import threading
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("gateway.platforms.webhook_ingress")

from tools import registry as host_registry
from tools.mcp_tool_agent import refresh_agent_mcp_tools
from inkbox_plugin import capabilities as caps
from inkbox_plugin.config import InkboxPluginConfig


@pytest.mark.parametrize("surface", ["cli", "gateway"])
def test_refresh_adds_and_removes_tools_from_an_existing_host_snapshot(monkeypatch, tmp_path, surface):
    import model_tools
    cfg = InkboxPluginConfig(api_key="synthetic", identity="example-agent")
    state = dict(sms_send=False, sms_history=False, errors=[])
    monkeypatch.setattr(caps, "read_runtime_config", lambda: cfg)
    monkeypatch.setattr(caps, "snapshot", lambda **kwargs: dict(state))
    monkeypatch.setattr(caps, "inkbox_state_path", lambda: tmp_path / "state.json")
    import gateway.run
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: None)
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    registry = host_registry.ToolRegistry()
    monkeypatch.setattr(host_registry, "registry", registry)
    names = {"inkbox_send_sms", "inkbox_list_texts", "inkbox_whoami", "inkbox_refresh_capabilities"}
    for name in names:
        registry.register(name=name, toolset="inkbox", schema={"parameters": {"type": "object", "properties": {}}},
                          handler=lambda args: "{}", check_fn=caps.check(name))

    def definitions(enabled_toolsets, disabled_toolsets, quiet_mode, **_kwargs):
        return registry.get_definitions(names, quiet=True) if "inkbox" in enabled_toolsets and "inkbox" not in disabled_toolsets else []

    monkeypatch.setattr(model_tools, "_compute_tool_definitions", definitions)
    monkeypatch.setattr(model_tools, "_tool_defs_cache", {})
    monkeypatch.setattr(model_tools, "_tool_defs_cache_key", lambda enabled, disabled, *args: (tuple(enabled), tuple(disabled)))
    agent = NS(session_id="active", tools=[], valid_tool_names=set(), enabled_toolsets=["inkbox"], disabled_toolsets=[])
    if surface == "gateway":
        from gateway.run import GatewayRunner
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = NS(multiplex_profiles=False)
        runner.session_authority = NS(db=NS(db_path=str(tmp_path / "state.db")))
        runner._agent_cache = {"active": (agent, "signature")}
        runner._agent_cache_lock = threading.Lock()
        monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)
    monkeypatch.setitem(sys.modules, "cli", NS(_active_agent_ref=agent if surface == "cli" else None))
    refresh_agent_mcp_tools(agent)
    assert agent.valid_tool_names == {"inkbox_whoami", "inkbox_refresh_capabilities"}
    state.update(sms_send=True, sms_history=True)
    refreshed = caps.refresh(session_id="active")
    assert refreshed["ok"] and refreshed["refreshed"] == [surface]
    assert agent.valid_tool_names == names
    state.update(sms_send=False)
    assert caps.refresh(session_id="active")["ok"]
    assert "inkbox_send_sms" not in agent.valid_tool_names
    assert "inkbox_list_texts" in agent.valid_tool_names
    state.update(sms_history=False)
    assert caps.refresh(session_id="active")["ok"]
    assert agent.valid_tool_names == {"inkbox_whoami", "inkbox_refresh_capabilities"}
    agent.disabled_toolsets = ["inkbox"]
    state.update(sms_send=True, sms_history=True)
    assert caps.refresh(session_id="active")["ok"]
    assert not agent.valid_tool_names
