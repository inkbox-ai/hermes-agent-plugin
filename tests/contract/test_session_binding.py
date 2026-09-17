"""Exercise session binding with the host's real callback and session store."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway.config import GatewayConfig, Platform
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.session import SessionStore

from tests.test_a2a import _adapter, _event
from inkbox_plugin.a2a_context import activate_next_a2a_turn_context


def test_a2a_ingestion_with_real_host_session_wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(PlatformEntry(
        name="inkbox", label="Inkbox", adapter_factory=lambda config: None,
        check_fn=lambda: True,
    ), scope=str(tmp_path))
    adapter = _adapter(tmp_path)
    adapter.platform = Platform("inkbox")
    del adapter._bind_a2a_turn_context
    store = SessionStore(tmp_path / "sessions", GatewayConfig())

    class Runner(GatewayAdapterLifecycleMixin):
        config = SimpleNamespace(multiplex_profiles=False)

        def _standalone_launch_scope(self):
            return nullcontext()

        async def _handle_message(self, event):
            return store.get_or_create_session(event.source).session_id

    adapter.set_message_handler(Runner()._primary_message_handler())
    adapter.set_session_store(store)
    response = asyncio.run(adapter._on_a2a_event(_event()))

    assert response.status == 200
    assert len(adapter._enqueued) == len(adapter._a2a_receipts) == 1
    event = adapter._enqueued[0]
    session_id = adapter._a2a_session_by_chat[event.source.chat_id]
    assert asyncio.run(adapter._message_handler(event)) == session_id
    context = activate_next_a2a_turn_context(session_id)
    assert context["task_id"] == "task-1"
    assert context["message_id"] == "message-1"
    assert asyncio.run(adapter._on_a2a_event(_event("retry"))).text == "duplicate"
    assert len(adapter._enqueued) == len(adapter._a2a_receipts) == 1
