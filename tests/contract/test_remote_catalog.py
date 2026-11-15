"""Remote catalogs and native turn IDs through the pinned tool dispatcher."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("hermes_cli.plugins")

from inkbox_plugin.remote_tools import RemoteTools


@pytest.mark.parametrize("approved", [True, False])
def test_catalog_changes_native_approval_and_source_lifetime(tmp_path, monkeypatch, approved):
    import model_tools
    import tools.approval
    from tools import registry as registry_module
    from hermes_cli import plugins
    from hermes_cli.plugins_manifest import PluginManifest
    from inkbox_plugin import capabilities
    from inkbox_plugin import remote_tools

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = registry_module.ToolRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(model_tools, "registry", registry)
    monkeypatch.setattr(remote_tools, "inkbox_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(capabilities, "refresh_host_tools", lambda **kwargs: {})
    monkeypatch.setattr(remote_tools.time, "monotonic", lambda: 10.0)
    manager = plugins.PluginManager()
    ctx = plugins.PluginContext(PluginManifest(name="inkbox", description="Synthetic", version="1", path=tmp_path), manager)
    owner = RemoteTools(ctx)
    calls = []
    descriptors = [
        {
            "name": "inkbox_synthetic",
            "description": "First",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": False},
            "outputSchema": {"type": "object"},
        }
    ]

    async def call(name, args, **kwargs):
        kwargs["before_execute"]()
        calls.append((name, kwargs))
        return {"content": [], "structuredContent": {"accepted": True}}

    async def discover():
        return SimpleNamespace(call=call), list(descriptors)

    monkeypatch.setattr(owner, "discover", discover)
    monkeypatch.setattr(
        plugins, "invoke_hook", lambda hook, **kwargs: [owner.approve(**kwargs)] if hook == "pre_tool_call" else []
    )
    approval_requests = []

    def request_approval(*args, **kwargs):
        approval_requests.append(args)
        return {"approved": approved, "message": "Synthetic declined"}

    monkeypatch.setattr(tools.approval, "request_tool_approval", request_approval)
    owner.begin(session_id="session", turn_id="first", platform="cli")
    assert registry.get_entry("inkbox_synthetic").schema["description"] == "First"
    result = json.loads(
        model_tools.handle_function_call(
            "inkbox_synthetic", {}, session_id="session", turn_id="first", tool_call_id="native-call"
        )
    )
    assert approval_requests
    assert bool(calls) is approved
    if approved:
        assert result["structuredContent"] == {"accepted": True}
        model_tools.handle_function_call(
            "inkbox_synthetic", {}, session_id="session", turn_id="first", tool_call_id="native-call"
        )
        assert calls[0][1]["call_identity"] == calls[1][1]["call_identity"]
    owner.end(session_id="session", turn_id="first")
    before = len(calls)
    model_tools.handle_function_call("inkbox_synthetic", {}, session_id="session", turn_id="first", tool_call_id="late-call")
    assert len(calls) == before

    descriptors[0] = {**descriptors[0], "description": "Changed"}
    owner.refreshed_at = 0
    owner.begin(session_id="session", turn_id="second", platform="cli")
    assert registry.get_entry("inkbox_synthetic").schema["description"] == "Changed"
    owner.end(session_id="session", turn_id="second")
    descriptors.clear()
    owner.refreshed_at = 0
    owner.begin(session_id="session", turn_id="third", platform="cli")
    assert registry.get_entry("inkbox_synthetic") is None
