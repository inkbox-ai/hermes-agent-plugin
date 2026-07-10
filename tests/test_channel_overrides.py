"""Tests for per-channel prompt + skill overrides on the Inkbox adapter."""

import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.adapter import InkboxAdapter


def _adapter(extra):
    """Build a config-only adapter shell for exercising the resolver helpers."""
    adapter = object.__new__(InkboxAdapter)
    adapter.config = types.SimpleNamespace(extra=extra)
    return adapter


# ── _merge_auto_skills ──────────────────────────────────────────────────────


def test_merge_defaults_only_normalizes_to_list():
    assert InkboxAdapter._merge_auto_skills("inkbox:inkbox-troubleshooting", None) == [
        "inkbox:inkbox-troubleshooting"
    ]


def test_merge_unions_defaults_and_configured_deduped_defaults_first():
    merged = InkboxAdapter._merge_auto_skills(
        ["inkbox:inkbox-troubleshooting", "inkbox:inkbox-imessage-responder"],
        ["inkbox:inkbox-imessage-responder", "inkbox:inkbox-outreach-sequence"],
    )
    assert merged == [
        "inkbox:inkbox-troubleshooting",
        "inkbox:inkbox-imessage-responder",
        "inkbox:inkbox-outreach-sequence",
    ]


def test_merge_all_empty_is_none():
    assert InkboxAdapter._merge_auto_skills(None, None) is None


# ── resolve: built-in reply-is-auto-sent directive ──────────────────────────


def test_builtin_directive_present_for_each_text_channel():
    """Even with no operator config, each text channel carries the tiny
    reply-is-auto-sent directive naming its own send tool."""
    adapter = _adapter({})
    for modality, tool in (
        ("sms", "inkbox_send_sms"),
        ("imessage", "inkbox_send_imessage"),
        ("email", "inkbox_send_email"),
    ):
        prompt, _ = adapter._resolve_channel_overrides(modality, "contact_1", None)
        assert prompt is not None
        assert "sent automatically" in prompt and tool in prompt


def test_no_builtin_directive_for_voice_or_external():
    """Voice and external events have their own handling — no text directive."""
    adapter = _adapter({})
    for modality in ("voice", "external"):
        prompt, _ = adapter._resolve_channel_overrides(modality, "contact_1", None)
        assert prompt is None


def test_no_overrides_returns_builtin_directive_and_defaults():
    adapter = _adapter({})
    prompt, skills = adapter._resolve_channel_overrides(
        "imessage", "contact_1", "inkbox:inkbox-troubleshooting"
    )
    assert prompt is not None and "inkbox_send_imessage" in prompt
    assert skills == ["inkbox:inkbox-troubleshooting"]


# ── resolve: channel_prompts (operator prompt appends AFTER the directive) ────


def test_modality_prompt_appends_after_builtin_directive():
    adapter = _adapter({"channel_prompts": {"imessage": "Inkbox iMessage concierge."}})
    prompt, _ = adapter._resolve_channel_overrides("imessage", "contact_1", None)
    assert prompt.endswith("Inkbox iMessage concierge.")
    assert "sent automatically" in prompt  # built-in directive still prepended


def test_contact_prompt_wins_over_modality_prompt():
    adapter = _adapter(
        {
            "channel_prompts": {
                "imessage": "Inkbox iMessage concierge.",
                "contact_1": "VIP Inkbox contact handling.",
            }
        }
    )
    prompt, _ = adapter._resolve_channel_overrides("imessage", "contact_1", None)
    assert prompt.endswith("VIP Inkbox contact handling.")


def test_blank_prompt_leaves_only_builtin_directive():
    adapter = _adapter({"channel_prompts": {"sms": "   "}})
    prompt, _ = adapter._resolve_channel_overrides("sms", "contact_1", None)
    # Blank operator prompt ignored; the built-in sms directive still stands.
    assert prompt is not None and "inkbox_send_sms" in prompt


# ── resolve: channel_skill_bindings ─────────────────────────────────────────


def test_configured_skills_merge_with_defaults():
    adapter = _adapter(
        {
            "channel_skill_bindings": [
                {"id": "imessage", "skills": ["inkbox:inkbox-outreach-sequence"]}
            ]
        }
    )
    _, skills = adapter._resolve_channel_overrides(
        "imessage", "contact_1", "inkbox:inkbox-imessage-responder"
    )
    assert skills == [
        "inkbox:inkbox-imessage-responder",
        "inkbox:inkbox-outreach-sequence",
    ]


def test_single_skill_shorthand_accepted():
    adapter = _adapter(
        {"channel_skill_bindings": [{"id": "sms", "skill": "inkbox:inkbox-sms-responder"}]}
    )
    _, skills = adapter._resolve_channel_overrides("sms", "contact_1", None)
    assert skills == ["inkbox:inkbox-sms-responder"]


def test_contact_binding_wins_over_modality_binding():
    adapter = _adapter(
        {
            "channel_skill_bindings": [
                {"id": "voice", "skills": ["inkbox:inkbox-call-review"]},
                {"id": "contact_1", "skills": ["inkbox:inkbox-outbound-calling"]},
            ]
        }
    )
    _, skills = adapter._resolve_channel_overrides("voice", "contact_1", None)
    assert skills == ["inkbox:inkbox-outbound-calling"]
