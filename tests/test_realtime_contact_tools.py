import asyncio
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import tools as tools_mod
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.realtime import (
    CONTACT_LIST_TOOL_NAME,
    CONTACT_LOOKUP_TOOL_NAME,
    CONTACT_READ_MAX_RESULTS,
    CONTACT_READ_NOTES_MAX_CHARS,
    MAIN_AGENT_CAPABILITIES,
    REALTIME_CONTACT_READ_TOOLS,
    RealtimeCallMeta,
    RealtimeConfig,
    _BridgeState,
    _dispatch_tool_call,
    _send_session_update,
    build_realtime_instructions,
)


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_str(self, payload):
        self.sent.append(json.loads(payload))


def _meta(**overrides) -> RealtimeCallMeta:
    fields = {
        "call_id": "call-1",
        "contact_id": "contact-1",
        "contact_name": "unknown",
        "remote_phone_number": "+15550001111",
        "direction": "inbound",
    }
    fields.update(overrides)
    return RealtimeCallMeta(**fields)


async def _noop_consult(*_args, **_kwargs):
    return ""


def _dispatch(name: str, arguments: dict) -> _FakeWS:
    ws = _FakeWS()
    asyncio.run(_dispatch_tool_call(
        openai_ws=ws,
        call_id="fn-1",
        name=name,
        arguments_json=json.dumps(arguments),
        state=_BridgeState(),
        config=RealtimeConfig(enabled=True, api_key="sk-test"),
        meta=_meta(),
        on_agent_consult=_noop_consult,
    ))
    return ws


def _submitted_output(ws: _FakeWS) -> dict:
    frame = ws.sent[0]
    assert frame["type"] == "conversation.item.create"
    assert frame["item"]["type"] == "function_call_output"
    return json.loads(frame["item"]["output"])


def test_session_update_includes_contact_read_tools():
    ws = _FakeWS()
    asyncio.run(_send_session_update(
        ws, RealtimeConfig(enabled=True, api_key="sk-test"), _meta(),
    ))
    tool_names = {tool["name"] for tool in ws.sent[0]["session"]["tools"]}
    assert set(REALTIME_CONTACT_READ_TOOLS) <= tool_names


def test_contact_read_tools_are_a_subset_of_the_contacts_capability_group():
    contacts_group_tools = next(
        tool_names
        for group, _summary, tool_names in MAIN_AGENT_CAPABILITIES
        if group == "contacts"
    )
    assert set(REALTIME_CONTACT_READ_TOOLS) <= set(contacts_group_tools)


def test_instructions_route_contact_questions_to_direct_tools():
    instructions = build_realtime_instructions(_meta())
    assert CONTACT_LIST_TOOL_NAME in instructions
    assert CONTACT_LOOKUP_TOOL_NAME in instructions
    assert "third" in instructions.lower()  # third-party disclosure guardrail


def test_contact_list_dispatch_caps_results_and_trims_for_voice(monkeypatch):
    seen_args = {}

    def _fake_list(args, **_kwargs):
        seen_args.update(args)
        contacts = [
            {
                "id": f"c{i}",
                "preferred_name": f"Contact {i}",
                "emails": [{"value": f"c{i}@example.com", "is_primary": True}],
                "phones": [{"value": f"+1555000{i:04d}"}],
                "notes": "x" * 500,
                "created_at": "2026-01-01T00:00:00Z",
            }
            for i in range(7)
        ]
        return json.dumps({"ok": True, "count": 7, "contacts": contacts})

    monkeypatch.setattr(tools_mod, CONTACT_LIST_TOOL_NAME, _fake_list)

    ws = _dispatch(CONTACT_LIST_TOOL_NAME, {"q": "alex"})
    output = _submitted_output(ws)

    # The wrapper forces the small page size on the underlying tool call.
    assert seen_args["limit"] == CONTACT_READ_MAX_RESULTS
    assert len(output["contacts"]) == CONTACT_READ_MAX_RESULTS
    assert output["truncated_to"] == CONTACT_READ_MAX_RESULTS
    first = output["contacts"][0]
    # Cards are flattened + clipped for speech: bare values, short notes,
    # no incidental metadata.
    assert first["emails"] == ["c0@example.com"]
    assert first["phones"] == ["+15550000000"]
    assert len(first["notes"]) == CONTACT_READ_NOTES_MAX_CHARS
    assert "created_at" not in first
    # The result frame is followed by response.create so the model speaks it.
    assert ws.sent[1]["type"] == "response.create"


def test_contact_lookup_dispatch_passes_errors_through(monkeypatch):
    def _fake_lookup(args, **_kwargs):
        return json.dumps({"error": "Specify exactly one of email, phone, ..."})

    monkeypatch.setattr(tools_mod, CONTACT_LOOKUP_TOOL_NAME, _fake_lookup)

    ws = _dispatch(CONTACT_LOOKUP_TOOL_NAME, {})
    output = _submitted_output(ws)
    assert output["error"].startswith("Specify exactly one")


def test_consult_prompt_carries_caller_trust_context(monkeypatch):
    captured = {}

    async def _fake_exec(*cmd, **_kwargs):
        captured["prompt"] = cmd[2]

        class _Proc:
            async def communicate(self):
                return b"the answer", b""

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    adapter = object.__new__(InkboxAdapter)

    asyncio.run(adapter._realtime_agent_consult(
        _meta(contact_known=False), "who is alex?", [],
    ))
    assert "unverified" in captured["prompt"]
    assert "third parties" in captured["prompt"]

    asyncio.run(adapter._realtime_agent_consult(
        _meta(contact_known=True, contact_name="Jane"), "who is alex?", [],
    ))
    assert "known Inkbox contact" in captured["prompt"]
    assert "unverified" not in captured["prompt"]
