import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.realtime import (
    MAIN_AGENT_CAPABILITIES,
    RealtimeCallMeta,
    _agent_consult_tool_schema,
    build_realtime_instructions,
)


def _manifest_provides_tools() -> set[str]:
    # Same parse as test_registration.py; the manifest is asserted equal to
    # runtime registration there, so it is a safe drift anchor here too.
    tools: set[str] = set()
    in_block = False
    for raw_line in (ROOT / "plugin.yaml").read_text().splitlines():
        if raw_line.startswith("provides_tools:"):
            in_block = True
            continue
        if in_block and raw_line and not raw_line.startswith(" "):
            break
        if in_block:
            line = raw_line.strip()
            if line.startswith("- "):
                tools.add(line[2:].strip())
    return tools


def _meta() -> RealtimeCallMeta:
    return RealtimeCallMeta(
        call_id="call-1",
        contact_id="contact-1",
        contact_name="unknown",
        remote_phone_number="+15550001111",
        direction="inbound",
    )


def test_every_plugin_tool_maps_to_exactly_one_capability_group():
    manifest = _manifest_provides_tools()
    mapped: list[str] = []
    for _group, _summary, tool_names in MAIN_AGENT_CAPABILITIES:
        mapped.extend(tool_names)

    # Stale names would let the prompt promise tools that no longer exist.
    stale = set(mapped) - manifest
    assert not stale, f"capability map names tools the plugin no longer provides: {stale}"

    # A new tool must land in some group or the voice model never hears of it.
    unmapped = manifest - set(mapped)
    assert not unmapped, f"plugin tools missing from MAIN_AGENT_CAPABILITIES: {unmapped}"

    assert len(mapped) == len(set(mapped)), "a tool appears in more than one capability group"


def test_instructions_and_consult_description_render_the_same_capabilities():
    instructions = build_realtime_instructions(_meta())
    description = _agent_consult_tool_schema()["description"]
    # Both surfaces must carry every group summary verbatim so they can't
    # drift back into two hand-maintained lists.
    for _group, summary, _tool_names in MAIN_AGENT_CAPABILITIES:
        assert summary in instructions
        assert summary in description
