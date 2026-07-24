import importlib.util
import logging
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_entry_module():
    module_name = "hermes_agent_plugin_test_entry"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DummyContext:
    def __init__(self):
        self.platforms = []
        self.tools = []
        self.cli_commands = []
        self.commands = []
        self.skills = []
        self.hooks = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)

    def register_tool(self, *args, **kwargs):
        self.tools.append((args, kwargs))

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs)

    def register_command(self, *args, **kwargs):
        self.commands.append((args, kwargs))

    def register_skill(self, *args, **kwargs):
        self.skills.append((args, kwargs))

    def register_hook(self, *args, **kwargs):
        self.hooks.append((args, kwargs))


def _manifest_provides_tools() -> set[str]:
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


def test_registers_inkbox_platform_tools_commands_and_skills():
    entry = _load_entry_module()
    ctx = DummyContext()

    entry.register(ctx)

    assert len(ctx.platforms) == 1
    platform = ctx.platforms[0]
    assert platform["name"] == "inkbox"
    assert callable(platform["adapter_factory"])
    assert callable(platform["setup_fn"])
    assert callable(platform["standalone_sender_fn"])
    assert platform["required_env"] == ["INKBOX_API_KEY", "INKBOX_IDENTITY"]

    tool_names = {args[0] for args, _kwargs in ctx.tools}
    assert tool_names == {
        "inkbox_whoami",
        "inkbox_lookup_contact",
        "inkbox_list_contacts",
        "inkbox_get_contact",
        "inkbox_create_contact",
        "inkbox_update_contact",
        "inkbox_delete_contact",
        "inkbox_send_email",
        "inkbox_send_sms",
        "inkbox_list_text_conversations",
        "inkbox_get_text_conversation",
        "inkbox_list_texts",
        "inkbox_get_text",
        "inkbox_mark_text_read",
        "inkbox_mark_text_conversation_read",
        "inkbox_imessage_triage_number",
        "inkbox_send_imessage",
        "inkbox_list_imessage_assignments",
        "inkbox_list_imessage_conversations",
        "inkbox_get_imessage_conversation",
        "inkbox_send_imessage_reaction",
        "inkbox_mark_imessage_conversation_read",
        "inkbox_place_call",
        "inkbox_a2a_complete",
        "inkbox_a2a_ask_caller",
        "inkbox_a2a_fail",
    }
    assert _manifest_provides_tools() == tool_names

    assert ctx.cli_commands[0]["name"] == "inkbox"
    assert ctx.commands[0][0][0] == "inkbox"
    assert {args[0] for args, _kwargs in ctx.skills}
    assert ctx.hooks[0][0][0] == "pre_llm_call"


def test_env_enablement_warns_once_when_plugin_is_unconfigured(monkeypatch, caplog):
    entry = _load_entry_module()
    monkeypatch.delenv("INKBOX_API_KEY", raising=False)
    monkeypatch.delenv("INKBOX_IDENTITY", raising=False)

    with caplog.at_level(logging.WARNING):
        assert entry._env_enablement() is None
        assert entry._env_enablement() is None

    warnings = [record.message for record in caplog.records if "[Inkbox]" in record.message]
    assert len(warnings) == 1
    assert "missing INKBOX_API_KEY and INKBOX_IDENTITY" in warnings[0]
    assert "hermes inkbox setup" in warnings[0]


def test_env_enablement_does_not_warn_when_configured(monkeypatch, caplog):
    entry = _load_entry_module()
    monkeypatch.setenv("INKBOX_API_KEY", "ApiKey_test")
    monkeypatch.setenv("INKBOX_IDENTITY", "test-agent")

    with caplog.at_level(logging.WARNING):
        seed = entry._env_enablement()

    assert seed is not None
    assert seed["api_key"] == "ApiKey_test"
    assert seed["identity"] == "test-agent"
    assert not [record for record in caplog.records if "[Inkbox]" in record.message]


def test_skill_required_tools_match_runtime_tools():
    available = _manifest_provides_tools()
    for skill_md in (ROOT / "skills").glob("*/SKILL.md"):
        text = skill_md.read_text()
        match = re.search(r"## Required tools(?P<section>.*?)(?:\n## |\Z)", text, flags=re.S)
        if not match:
            continue
        required = set(re.findall(r"`(inkbox_[A-Za-z0-9_]+)`", match.group("section")))
        assert required <= available, f"{skill_md} requires unavailable tools: {required - available}"
