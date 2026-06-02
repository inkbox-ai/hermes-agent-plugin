import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.adapter import InkboxAdapter, _is_hermes_admin_notice


def _bare_adapter():
    adapter = object.__new__(InkboxAdapter)
    adapter._active_call_ws = {}
    adapter._last_inbound_modality = {}
    return adapter


@pytest.mark.parametrize(
    "body",
    [
        '⚙️ inkbox_send_sms: "Hi Dima - this is a quick test text..."',
        "⚙ inkbox_send_email...",
    ],
)
def test_gear_tool_progress_is_admin_notice(body):
    assert _is_hermes_admin_notice(body) is True


def test_voice_calls_do_not_support_tool_progress():
    adapter = _bare_adapter()
    adapter._active_call_ws["contact-voice"] = object()

    assert adapter.supports_progress_updates("contact-voice") is False


def test_voice_calls_still_support_interim_messages():
    adapter = _bare_adapter()
    adapter._active_call_ws["contact-voice"] = object()

    assert adapter.supports_interim_messages("contact-voice") is True


def test_sms_still_supports_tool_progress():
    adapter = _bare_adapter()
    adapter._last_inbound_modality["+15555550101"] = "sms"

    assert adapter.supports_progress_updates("+15555550101") is True

