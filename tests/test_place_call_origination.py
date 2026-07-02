"""Outbound-call line resolution: explicit choice, capability fallback, and
channel-aware defaulting when the identity has BOTH a dedicated number and
iMessage enabled.

Regression guard for the bug where an agent on an iMessage conversation asked
to "call me" and the call went out over the dedicated number instead of the
shared iMessage line.
"""

import types

import tools


def _identity(has_number: bool, imessage: bool):
    return types.SimpleNamespace(
        phone_number=types.SimpleNamespace(number="+15550000000") if has_number else None,
        imessage_enabled=imessage,
    )


def _set_channel(monkeypatch, thread_id):
    # _current_channel_hint reads the host session var, falling back to
    # os.environ when the gateway isn't present (as in tests).
    if thread_id is None:
        monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_THREAD_ID", thread_id)


def test_single_line_resolves_unambiguously(monkeypatch):
    _set_channel(monkeypatch, None)
    assert tools._resolve_call_origination(_identity(True, False), "") == "dedicated_number"
    assert tools._resolve_call_origination(_identity(False, True), "") == "shared_imessage_number"
    assert tools._resolve_call_origination(_identity(False, False), "") is None


def test_explicit_choice_wins_over_channel(monkeypatch):
    _set_channel(monkeypatch, "imessage:conv")
    assert tools._resolve_call_origination(_identity(True, True), "dedicated_number") == "dedicated_number"
    _set_channel(monkeypatch, "sms:conv")
    assert tools._resolve_call_origination(_identity(True, True), "shared_imessage_number") == "shared_imessage_number"


def test_both_lines_follow_conversation_channel(monkeypatch):
    both = _identity(True, True)
    _set_channel(monkeypatch, "imessage:conv-1")
    assert tools._resolve_call_origination(both, "") == "shared_imessage_number"
    _set_channel(monkeypatch, "sms:conv-1")
    assert tools._resolve_call_origination(both, "") == "dedicated_number"
    _set_channel(monkeypatch, "phone:conv-1")
    assert tools._resolve_call_origination(both, "") == "dedicated_number"


def test_both_lines_unknown_channel_defaults_dedicated(monkeypatch):
    _set_channel(monkeypatch, None)
    assert tools._resolve_call_origination(_identity(True, True), "") == "dedicated_number"


def test_channel_only_breaks_ties(monkeypatch):
    # An iMessage-only identity stays shared even on an SMS-looking thread.
    _set_channel(monkeypatch, "sms:conv")
    assert tools._resolve_call_origination(_identity(False, True), "") == "shared_imessage_number"
