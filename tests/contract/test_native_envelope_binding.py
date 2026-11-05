"""Private journal binding against the official host's actual event types."""
import copy
from unittest.mock import Mock
from weakref import ref

import pytest

pytest.importorskip("gateway.platforms.webhook_ingress")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.native_envelope import (
    _digest,
    _lookup,
    _prepare_media,
    event_record,
    identity_scope,
    install,
    route_for,
    uninstall,
)


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter, check_fn=lambda: True))
    adapter = InkboxAdapter(PlatformConfig(extra={"identity": "sample-agent"}))
    adapter._identity_id, adapter._base_url = "sample-identity", "https://example.test"
    source = adapter.build_source(chat_id="sample-chat", user_id="sample-user", chat_type="group")
    event = MessageEvent(text="Synthetic request", source=source, message_id="sample-event",
        metadata={"inkbox_prepared": True, "inkbox_reply_route": {
            "mode": "whatsapp", "chat_id": source.chat_id, "conversation_id": "sample-conversation", "author": "sample-user"}})
    media = tmp_path / "image.png"
    media.write_bytes(b"synthetic image bytes")
    event.media_urls, event.media_types = [str(media)], ["image/png"]
    journal = adapter._conversation_state()
    journal.remember(source.chat_id, event.message_id, event.metadata["inkbox_reply_route"])
    record = {"scope": identity_scope(adapter), "event": event_record(event)}
    record["digest"] = _digest(record["event"])
    journal.row(source.chat_id).setdefault("native_admissions", {})[event.message_id] = record
    journal.save(source.chat_id)
    restored = copy.deepcopy(event)
    restored.metadata = {}
    return adapter, event, restored, record, media


def test_exact_wire_input_resolves_only_its_durable_context(bound):
    adapter, original, wire, _, _ = bound
    assert _lookup(adapter, wire, wire=True)
    assert wire.metadata == {}
    assert original.raw_message is None


@pytest.mark.parametrize("change", ["identity", "source", "author", "message", "text", "timestamp", "journal", "digest", "media", "route_expired", "metadata"])
def test_changed_input_or_owner_cannot_borrow_journal(bound, change):
    adapter, _, event, record, media = bound
    if change == "identity":
        adapter._identity_id = "other-identity"
    elif change == "source":
        event.source.chat_id = "other-chat"
    elif change == "author":
        event.source.user_id = "other-user"
    elif change == "message":
        event.message_id = "other-message"
    elif change == "text":
        event.text = "Changed input"
    elif change == "timestamp":
        event.timestamp = event.timestamp.replace(year=event.timestamp.year - 1)
    elif change == "journal":
        record["event"]["metadata"]["inkbox_reply_route"]["conversation_id"] = "other-conversation"
    elif change == "digest":
        record["digest"] = "changed"
    elif change == "media":
        media.write_bytes(b"different image bytes")
    elif change == "metadata":
        event.metadata = {"inkbox_prepared": True}
    else:
        adapter._conversation_state().row(event.source.chat_id)["routes"].clear()
    assert _lookup(adapter, event, wire=True) is None


def test_retained_media_path_can_change_but_bytes_cannot(bound, tmp_path):
    adapter, _, event, _, media = bound
    retained = tmp_path / "core-retained.png"
    retained.write_bytes(media.read_bytes())
    event.media_urls = [str(retained)]
    assert _lookup(adapter, event, wire=True)
    retained.write_bytes(b"tampered retained bytes")
    assert _lookup(adapter, event, wire=True) is None


def test_execution_proof_allows_host_text_expansion_and_media_cleanup_only_for_actual_event(bound):
    adapter, _, event, record, media = bound
    assert _lookup(adapter, event, wire=True)
    event.metadata = copy.deepcopy(record["event"]["metadata"])
    event.metadata["inkbox_native_binding"] = {"kind": "ordinary", "scope": record["scope"], "digest": record["digest"]}
    event._inkbox_execution_admission = (ref(event), record["digest"], tuple(event.media_urls), record["event"]["media"])
    event.text = "Host-loaded skill\n" + event.text
    media.unlink()
    assert route_for(adapter, event)["conversation_id"] == "sample-conversation"
    copied = copy.deepcopy(event)
    with pytest.raises((OSError, RuntimeError)):
        route_for(adapter, copied)
    event.source.user_id = "other-user"
    with pytest.raises(RuntimeError):
        route_for(adapter, event)


def test_handler_decorator_preserves_bound_owner_and_shared_registration():
    class Owner:
        async def _handle_message(self, event):
            return "unchanged"
    owner = Owner()
    original = owner._handle_message
    first, second = Mock(), Mock()
    install(first, owner)
    wrapped = owner._handle_message
    assert wrapped.__self__ is owner
    install(first, owner)
    install(second, owner)
    assert owner._handle_message is wrapped
    uninstall(first, owner)
    assert owner._handle_message is wrapped
    uninstall(second, owner)
    assert owner._handle_message == original


def test_nonproducer_empty_metadata_keeps_base_hook_semantics(bound):
    import asyncio
    adapter, _, event, _, _ = bound
    seen = []
    async def start(actual):
        seen.append(actual)
    adapter.on_processing_start = start
    asyncio.run(adapter._run_processing_hook("on_processing_start", event))
    assert seen == [event]


@pytest.mark.parametrize("change", [None, "identity", "profile", "text", "timestamp", "missing_journal", "media", "remote_media"])
def test_real_admission_codec_rejects_changed_private_binding(tmp_path, monkeypatch, change):
    """The public producer path and real retained-media codec preserve exact input ownership."""
    import asyncio

    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_envelope import restore_native
    from hermes_state_runtime import get_session_admission
    from inkbox_plugin.native_envelope import dispatch

    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("INKBOX_ALLOW_ALL_USERS", "true")
        platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
            check_fn=lambda: True, allow_all_env="INKBOX_ALLOW_ALL_USERS"))
        adapter = InkboxAdapter(PlatformConfig(enabled=True, typing_indicator=False, extra={"identity": "sample-agent"}))
        adapter._identity_id, adapter._base_url = "sample-identity", "https://example.test"
        runner = GatewayRunner(GatewayConfig())
        runner.adapters = {adapter.platform: adapter}
        authority = await initialize_session_authority(runner, profile_id="default", instance_id="binding-proof")
        runner._wire_adapter_handlers(adapter)
        adapter.gateway_runner = runner
        monkeypatch.setattr(authority, "_schedule", lambda *_args: None)
        source = adapter.build_source(chat_id="email:sample@example.test", user_id="sample@example.test", chat_type="dm")
        route = {"mode": "email", "chat_id": source.chat_id, "author": source.user_id,
                 "stored_message_id": "stored-original", "message_id": "binding-event"}
        media = tmp_path / "original.png"
        media.write_bytes(b"synthetic retained attachment")
        event = MessageEvent(text="Original request", source=source, message_id="binding-event",
            media_urls=[str(media)], media_types=["image/png"],
            metadata={"inkbox_prepared": True, "inkbox_reply_route": route})
        if change == "remote_media":
            event.media_urls.insert(0, "https://example.test/unavailable.png")
            event.media_types.insert(0, "image/jpeg")
            event.media_text_inlined = [True, False]
        journal = adapter._conversation_state()
        journal.remember(source.chat_id, event.message_id, route)
        try:
            await dispatch(adapter, event)
            record = journal.row(source.chat_id)["native_admissions"][event.message_id]
            row = get_session_admission(authority.db, admission_id=record["receipt"]["id"])
            assert row["status"] == "queued"
            wire = restore_native(row["payload"], runner)
            assert wire.metadata == {}
            assert wire is not event
            assert _lookup(adapter, wire, wire=True)
            if change == "remote_media":
                assert wire.text.endswith("[The user sent an attachment (image/jpeg) that could not be retrieved.]")
                assert wire.media_types == ["image/png"] and wire.media_text_inlined == [False]
                assert len(wire.media_urls) == 1
            if change == "identity":
                adapter._identity_id = "another-identity"
            elif change == "profile":
                wire.source.profile = "another-profile"
            elif change == "text":
                wire.text += " changed"
            elif change == "timestamp":
                wire.timestamp = wire.timestamp.replace(year=wire.timestamp.year - 1)
            elif change == "missing_journal":
                journal.row(source.chat_id)["native_admissions"].clear()
            elif change == "media":
                from pathlib import Path
                Path(wire.media_urls[0]).write_bytes(b"altered retained bytes")
            assert bool(_lookup(adapter, wire, wire=True)) is (change in {None, "remote_media"})
        finally:
            tasks = list(getattr(adapter, "_inkbox_receipt_tasks", {}).values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            uninstall(adapter, runner)
            authority.db.close()
    asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"media_urls": "https://example.test/image.png"},
    {"media_urls": [None]},
    {"media_types": ["image/png", "image/jpeg"]},
    {"media_text_inlined": ["true"]},
])
def test_media_preparation_rejects_malformed_lists_without_mutating_input(bound, changes):
    _, _, event, _, _ = bound
    for key, value in changes.items():
        setattr(event, key, value)
    original = copy.deepcopy(event)
    with pytest.raises(ValueError):
        _prepare_media(event)
    assert event == original


@pytest.mark.parametrize("outcome", ["return", "raise", "cancel"])
def test_owned_handler_context_always_resets(bound, outcome):
    import asyncio

    from gateway.session_ingress import executing_admission
    from inkbox_plugin.conversation import reply_route

    adapter, _, event, record, _ = bound
    event.metadata = copy.deepcopy(record["event"]["metadata"])
    event.metadata["inkbox_native_binding"] = {"kind": "ordinary", "scope": record["scope"], "digest": record["digest"]}
    event._inkbox_producer_execution = True
    class Owner:
        def _adapter_for_source(self, source):
            return adapter
        async def _handle_message(self, actual):
            assert actual is event
            assert reply_route.get()["conversation_id"] == "sample-conversation"
            if outcome == "raise":
                raise ValueError("Synthetic handler failure")
            if outcome == "cancel":
                raise asyncio.CancelledError()
            return "reply"
    owner = Owner()
    install(adapter, owner)
    async def run():
        outer = {"mode": "outer-route"}
        route_token = reply_route.set(outer)
        admission_token = executing_admission.set(True)
        try:
            if outcome == "return":
                assert await owner._handle_message(event) == "reply"
            else:
                with pytest.raises(ValueError if outcome == "raise" else asyncio.CancelledError):
                    await owner._handle_message(event)
            assert reply_route.get() is outer
        finally:
            executing_admission.reset(admission_token)
            reply_route.reset(route_token)
            uninstall(adapter, owner)
    asyncio.run(run())

@pytest.mark.parametrize("ending", ["unknown", "disconnect"])
def test_receipt_observer_failure_releases_without_claiming_delivery(bound, monkeypatch, ending):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from inkbox_plugin import native_envelope
    import hermes_state_runtime

    async def run():
        adapter, original, _, _, _ = bound
        authority = SimpleNamespace(db=object(), waiters={})
        monkeypatch.setattr(hermes_state_runtime, "get_session_admission",
            lambda *args, **kwargs: {"status": "started"})
        completed = AsyncMock()
        monkeypatch.setattr(adapter, "on_processing_complete", completed)
        native_envelope._watch_receipt(adapter, authority, "sample-admission", original)
        task = adapter._inkbox_receipt_tasks["sample-admission"]
        await asyncio.sleep(0)
        if ending == "unknown":
            authority.waiters["sample-admission"].set_exception(RuntimeError("unknown_execution"))
            await task
        else:
            await adapter._cleanup()
        completed.assert_awaited_once_with(original, "failure" if ending == "unknown" else "cancelled")
        assert not adapter._inkbox_receipt_tasks
        assert task.done()
        assert not getattr(original, "_inkbox_delivery_results", [])
    asyncio.run(run())
