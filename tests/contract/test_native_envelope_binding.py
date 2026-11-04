"""Exact journal/source/identity bindings and replay fences, without network calls."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

pytest.importorskip("gateway.session_adapter_execution")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent
from gateway.session_envelope import restore_native, snapshot_native
from hermes_state_runtime import RuntimeStoreError
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.conversation import reply_route
from inkbox_plugin.native_envelope import event_record, stamp, validate, route_for
from inkbox_plugin.native_turns import NativeTurns


@pytest.fixture(params=["ordinary", "queued"])
def bound(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
    adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent"}))
    adapter._identity_id, adapter._base_url = "sample-identity", "https://example.test"
    runner = NS(session_authority=NS(), config=NS(multiplex_profiles=False),
        _adapter_for_source=lambda _: adapter, _adapter_flag=lambda *args: False,
        _adapter_profile_for_source=lambda _: None, _is_user_authorized_for_source=lambda *args, **kwargs: True,
        session_store=NS(_generate_session_key=lambda source: source.chat_id))
    adapter.gateway_runner = runner
    source = adapter.build_source(chat_id="sample-chat", user_id="sample-user", chat_type="group")
    event = MessageEvent(text="Synthetic request", source=source, message_id="sample-event",
        metadata={"inkbox_prepared": True, "inkbox_reply_route": {
            "mode": "whatsapp", "chat_id": source.chat_id, "conversation_id": "sample-conversation", "author": "sample-user"}},
        raw_message={"event_type": "text.received"})
    if request.param == "queued":
        queue = NativeTurns.__new__(NativeTurns)
        queue.closed = False
        turn = {"id": event.message_id, "state": "running"}
        row = {"key": "sample-key", "chat_id": source.chat_id, "turns": [turn]}
        queue.rows, queue.active, queue.events = {"sample-key": row}, {source.chat_id: turn}, {event.message_id: event}
        queue._save = Mock()
        queue._stopped_worker_exited = Mock()
        adapter._native_turns = queue
        event.raw_message = {"_inkbox_native_key": "sample-key", "_inkbox_native_turn": event.message_id}
    else:
        adapter._conversation_state().remember(source.chat_id, event.message_id, event.metadata["inkbox_reply_route"])
    assert stamp(adapter, event)
    runner.session_authority.marker_adopters = {"sample-admission": event}
    payload = json.loads(json.dumps(snapshot_native(runner, event)))
    return adapter, runner, event, payload


def test_exact_roundtrip_and_validation_does_not_mutate_binding(bound):
    adapter, runner, original, payload = bound
    restored = restore_native(payload, runner)
    before = event_record(restored)
    assert validate(adapter, restored)
    assert event_record(restored) == before == event_record(original)
    assert restored.raw_message is None


@pytest.mark.parametrize("change", ["user", "profile", "chat", "route", "text", "identity"])
def test_changed_ownership_is_refused(bound, change):
    adapter, runner, original, payload = bound
    if change == "identity":
        adapter._identity_id = "another-identity"
    elif change in {"user", "profile", "chat"}:
        payload["native_text_v1"]["source"][{"user": "user_id", "profile": "profile", "chat": "chat_id"}[change]] = "another"
    elif change == "route":
        payload["native_text_v1"]["event"]["metadata"]["inkbox_native_binding"]["digest"] = "another"
    else:
        payload["text"] = "Different request"
    with pytest.raises(RuntimeStoreError):
        restore_native(payload, runner)


def test_execution_scope_resets_context_after_preworker_error(bound):
    adapter, runner, original, payload = bound
    restored = restore_native(payload, runner)
    adapter._event_session_key = lambda _: "sample-key"
    guard = NS(_inkbox_event=original)
    adapter._active_sessions = {"sample-key": guard}
    async def processing(event):
        guard._inkbox_event = event
        reply_route.set(route_for(adapter, event))
    adapter.on_processing_start = processing
    async def run():
        token = reply_route.set({"outside": True})
        try:
            with pytest.raises(ValueError, match="preworker"):
                async with adapter.native_execution_scope(restored):
                    assert guard._inkbox_event is restored
                    assert original._inkbox_execution_event is restored
                    assert reply_route.get() == route_for(adapter, original)
                    raise ValueError("preworker")
            assert reply_route.get() == {"outside": True}
        finally:
            reply_route.reset(token)
    asyncio.run(run())


def test_stopped_or_unknown_queued_turn_never_replays(bound):
    adapter, runner, original, payload = bound
    queue = getattr(adapter, "_native_turns", None)
    if queue is None:
        return
    turn = queue.active[original.source.chat_id]
    for state in ("cancelled", "uncertain", "sending", "done", "pending"):
        turn["state"] = state
        with pytest.raises(RuntimeStoreError):
            restore_native(payload, runner)
    turn["state"] = "running"
    queue.closed = True
    with pytest.raises(RuntimeStoreError):
        restore_native(payload, runner)


def test_oversized_unregistered_metadata_is_refused(bound):
    adapter, runner, event, _ = bound
    event.metadata["inkbox_extra"] = "x" * (16 * 1024)
    with pytest.raises(RuntimeStoreError):
        snapshot_native(runner, event)
    assert event.metadata["inkbox_extra"] == "x" * (16 * 1024)


@pytest.mark.parametrize("change", ["tampered_route", "missing_record", "expired_route"])
def test_journal_binding_cannot_be_substituted_or_expired(bound, change):
    adapter, runner, event, payload = bound
    queue = getattr(adapter, "_native_turns", None)
    if queue:
        records, key = queue.active[event.source.chat_id], "native_admission"
    else:
        records, key = adapter._conversation_state().row(event.source.chat_id)["native_admissions"], event.message_id
    if change == "tampered_route":
        records[key]["metadata"]["inkbox_reply_route"]["conversation_id"] = "another-conversation"
    elif change == "missing_record":
        records.pop(key)
    elif queue:
        queue.active.clear()
    else:
        adapter._conversation_state().row(event.source.chat_id)["routes"].pop(event.message_id)
    with pytest.raises(RuntimeStoreError):
        restore_native(payload, runner)


def test_old_host_path_does_not_project_metadata(bound, monkeypatch):
    import sys
    adapter, _, event, _ = bound
    # The plugin's own hook existing is not evidence that an older host calls it.
    before = copy.deepcopy(event.metadata)
    monkeypatch.setitem(sys.modules, "gateway.session_adapter_execution", None)
    assert stamp(adapter, event) is False
    assert event.metadata == before


@pytest.mark.parametrize("change", ["source", "identity", "journal", "copy"])
def test_execution_text_expansion_does_not_relax_delivery_ownership(bound, change):
    adapter, runner, original, payload = bound
    restored = restore_native(payload, runner)
    adapter._event_session_key = lambda _: "sample-key"
    adapter._active_sessions = {"sample-key": NS(_inkbox_event=original)}
    async def processing(event):
        pass
    adapter.on_processing_start = processing
    async def execute():
        async with adapter.native_execution_scope(restored):
            restored.text = "Host skill instructions\n" + restored.text
    asyncio.run(execute())
    expected_route = route_for(adapter, restored)
    assert expected_route and not validate(adapter, restored)
    if change == "source":
        restored.source.user_id = "different-user"
    elif change == "identity":
        adapter._identity_id = "different-identity"
    elif change == "copy":
        restored = copy.deepcopy(restored)
    else:
        queue = getattr(adapter, "_native_turns", None)
        record = (queue.active[original.source.chat_id]["native_admission"] if queue else
            adapter._conversation_state().row(original.source.chat_id)["native_admissions"][original.message_id])
        record["metadata"]["inkbox_reply_route"]["conversation_id"] = "different-conversation"
    with pytest.raises(RuntimeError, match="no longer owns"):
        route_for(adapter, restored)


@pytest.mark.parametrize("bound", ["ordinary"], indirect=True)
@pytest.mark.parametrize("served", [True, False])
def test_execution_owner_uses_actual_profile_authority_without_launch_fallback(bound, tmp_path, served):
    from gateway.session_authorities import SessionAuthorities
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    adapter, runner, launch_owner, payload = bound
    restored = restore_native(payload, runner)
    profile_home = tmp_path / "profiles" / "secondary"
    profile_owner = copy.deepcopy(launch_owner)
    registry = SessionAuthorities(tmp_path, multiplexed=True)
    registry.add(tmp_path, runner.session_authority)
    if served:
        registry.add(profile_home, NS(marker_adopters={"profile-admission": profile_owner}), name="secondary")
    runner.session_authorities = registry
    adapter._event_session_key = lambda _: "sample-key"
    async def processing(event):
        pass
    adapter.on_processing_start = processing
    async def run():
        token = set_hermes_home_override(profile_home)
        try:
            if served:
                async with adapter.native_execution_scope(restored):
                    assert profile_owner._inkbox_execution_event is restored
                    assert not hasattr(launch_owner, "_inkbox_execution_event")
            else:
                with pytest.raises(RuntimeError, match="serving authority"):
                    async with adapter.native_execution_scope(restored):
                        pytest.fail("unserved profile executed")
                assert not hasattr(launch_owner, "_inkbox_execution_event")
        finally:
            reset_hermes_home_override(token)
    asyncio.run(run())
