import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin.adapter import InkboxAdapter


# --- minimal SDK fakes (mirror test_sms_conversations) -------------------

class FakeText:
    id = "txt-1"
    delivery_status = "queued"
    conversation_id = "conv-home"


class FakeIdentity:
    def __init__(self):
        self.sent_texts = []

    def send_text(self, **kwargs):
        self.sent_texts.append(kwargs)
        return FakeText()


class FakeInkboxClient:
    def __init__(self, identity):
        self.identity = identity
        self.contacts = types.SimpleNamespace(get=lambda _cid: None)

    def get_identity(self, _handle):
        return self.identity


# --- send(): external-event reply routing --------------------------------

def test_external_reply_dropped_without_home_channel():
    # No home channel → the reply is dropped cleanly (success, so the host
    # doesn't log a delivery failure), and nothing is sent.
    adapter = object.__new__(InkboxAdapter)
    adapter._home_channel = ""
    result = asyncio.run(
        adapter.send("external:inkbox-ai/servers", "I called Jane about the CI failure.")
    )
    assert result.success is True
    assert result.message_id == "external-event-no-home-channel"


def test_external_reply_routed_to_home_channel(monkeypatch):
    # With a home channel configured, the external-event summary is redirected
    # there (here an SMS conversation) instead of dropped.
    identity = FakeIdentity()

    async def _inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "to_thread", _inline_to_thread)
    adapter = object.__new__(InkboxAdapter)
    adapter._home_channel = "home-contact"
    adapter._active_call_ws = {}
    adapter._voice_recently_closed = {}
    adapter._last_inbound_modality = {"home-contact": "sms"}
    adapter._last_inbound_sms = {
        "home-contact": {
            "conversation_id": "conv-home",
            "remote_phone_number": "+15555550101",
            "text_id": "txt-in",
        },
    }
    adapter._inkbox = FakeInkboxClient(identity)
    adapter._identity_handle = "agent"

    result = asyncio.run(
        adapter.send("external:inkbox-ai/servers", "Called Jane about the CI failure.")
    )
    assert result.success is True
    assert identity.sent_texts == [
        {"conversation_id": "conv-home", "text": "Called Jane about the CI failure."}
    ]


# --- _on_external_event: injected system-prompt directive ----------------

def _external_adapter():
    adapter = object.__new__(InkboxAdapter)
    adapter.platform = "inkbox"
    adapter._enqueued = []

    async def _capture(event):
        adapter._enqueued.append(event)

    adapter._enqueue = _capture
    adapter._resolve_channel_overrides = lambda *a, **k: (None, None)
    return adapter


def test_external_event_injects_directive_prompt():
    adapter = _external_adapter()
    asyncio.run(adapter._on_external_event({"event": "workflow_run", "title": "CI failed"}, "req-1"))
    event = adapter._enqueued[0]
    # The directive is injected as the per-turn channel_prompt (system prompt).
    assert event.channel_prompt == adapter_mod.EXTERNAL_EVENT_DIRECTIVE
    assert "NOT delivered" in event.channel_prompt  # the "no human reads this" clause


def test_external_event_directive_composes_with_operator_prompt():
    adapter = _external_adapter()
    adapter._resolve_channel_overrides = lambda *a, **k: ("OPS PLAYBOOK", "inkbox:oncall")
    asyncio.run(adapter._on_external_event({"event": "workflow_run"}, "req-2"))
    event = adapter._enqueued[0]
    # Directive first, then the operator's per-source playbook.
    assert event.channel_prompt.startswith(adapter_mod.EXTERNAL_EVENT_DIRECTIVE)
    assert event.channel_prompt.endswith("OPS PLAYBOOK")
    assert event.auto_skill == "inkbox:oncall"
