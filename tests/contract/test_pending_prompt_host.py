"""Real Hermes request registries and thread/loop prompt delivery through Inkbox."""
import asyncio
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionStore
from gateway.turn_context import TurnContext
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.host_fencing import cancel_pending_permissions
from tools import approval, approval_context, clarify_gateway
from tools.approval_gateway_wait import _await_gateway_decision


def prompt_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(PlatformEntry(name="inkbox", label="Inkbox", adapter_factory=InkboxAdapter,
                                            check_fn=lambda: True))
    adapter = InkboxAdapter(PlatformConfig(extra={"identity": "synthetic-agent", "require_signature": False}))
    adapter.set_session_store(SessionStore(tmp_path / "sessions", GatewayConfig()))
    identity = NS(send_text=Mock(return_value=NS(id="synthetic-delivery", delivery_status="queued")))
    adapter._inkbox = NS(get_identity=lambda _: identity)
    adapter._reply_identity = identity
    source = adapter.build_source(chat_id="sms:synthetic-conversation", chat_type="dm", user_id="+15555550101",
                                  thread_id="synthetic-conversation", message_id="synthetic-source")
    owner = NS(_pending_approvals={})
    adapter.gateway_runner = owner
    key = adapter._conversation_session_key(source)
    ctx = TurnContext(source=source, session_key=key, session_id="synthetic-session",
        _loop_for_step=asyncio.get_running_loop(), _status_adapter=adapter, _status_chat_id=source.chat_id,
        _status_thread_metadata={"mode": "sms", "conversation_id": "synthetic-conversation", "to": "+15555550101"})
    ctx._run_still_current = lambda: False  # completed ownership cannot post a late timeout notice
    return NS(adapter=adapter, identity=identity, owner=owner, source=source, key=key, runner=TurnRunner(owner, ctx))


async def pending(value, kind):
    for _ in range(200):
        rows = approval.list_gateway_approvals(value.key) if kind == "approval" else clarify_gateway.get_pending_for_session(value.key, include_choice_prompts=True)
        if rows:
            return rows[0]["request_id"] if kind == "approval" else rows.clarify_id
        await asyncio.sleep(.005)
    raise AssertionError("Actual native request was not registered")


def request(value, kind, number=1):
    if kind == "approval":
        return _await_gateway_decision(value.key, value.runner._approval_notify_sync,
            {"command": f"synthetic-operation-{number}", "description": "Synthetic permission boundary", "pattern_key": f"synthetic-{number}"})
    return value.runner._ask_clarify_question(f"Synthetic question {number}", ["First", "Second"], False)


def cleanup(value):
    approval.unregister_gateway_notify(value.key)
    clarify_gateway.clear_session(value.key)


@pytest.mark.parametrize("kind", ["approval", "clarify"])
@pytest.mark.parametrize("failure", [False, True])
def test_real_pending_prompt_timeout_or_failed_delivery_releases_ownership(tmp_path, monkeypatch, kind, failure):
    async def run():
        value = prompt_host(tmp_path, monkeypatch)
        # Test-local native configuration, not replacement of registry/wait/
        # resolution methods or a change to production/model task deadlines.
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: .04)
        monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: .04)
        if failure:
            value.identity.send_text.side_effect = RuntimeError("synthetic transport rejected")
        task = asyncio.create_task(asyncio.to_thread(request, value, kind))
        try:
            result = await asyncio.wait_for(task, 3)
            assert value.identity.send_text.call_count >= 1
            assert not value.adapter._pending_conversation_control(value.source)
            assert not approval.list_gateway_approvals(value.key)
            assert not clarify_gateway.has_pending(value.key)
            if kind == "approval":
                assert result["choice"] is None and not result["resolved"]
            else:
                assert result[1] is False
                assert ("could not be delivered" in result[0]) is failure
            # An old human answer cannot resolve an already retired request.
            assert approval.resolve_gateway_approval(value.key, "once") == 0
            assert not clarify_gateway.resolve_text_response_for_session(value.key, "1")
        finally:
            cleanup(value)
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["approval", "clarify"])
def test_real_pending_shutdown_and_old_waiter_cleanup_preserve_new_request(tmp_path, monkeypatch, kind):
    async def run():
        value = prompt_host(tmp_path, monkeypatch)
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 2)
        monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: 2)
        first = asyncio.create_task(asyncio.to_thread(request, value, kind, 1))
        second = None
        gate, entered = threading.Event(), threading.Event()
        try:
            original_id = await pending(value, kind)
            assert value.adapter._pending_conversation_control(value.source)
            if kind == "approval":
                # Delay only the observer after native removal. The registry's
                # lock, request lookup and waiter cleanup remain real.
                def old_settled(_reason):
                    entered.set()
                    assert gate.wait(3)
                assert approval.register_gateway_settle(value.key, original_id, old_settled)
                approval.approve_session(value.key, "existing-approved-pattern")
                await cancel_pending_permissions(value.owner, value.key)
                assert await asyncio.to_thread(entered.wait, 2)
                assert approval.is_approved(value.key, "existing-approved-pattern")
            else:
                entry = clarify_gateway.get_pending_for_session(value.key, include_choice_prompts=True)
                # Observe the real native waiter, rather than racing request
                # registration before the delivery future has reached its wait.
                for _ in range(200):
                    if entry.event._cond._waiters:
                        break
                    await asyncio.sleep(.005)
                assert entry.event._cond._waiters
                # This is the exact native run-end/shutdown registry operation.
                assert clarify_gateway.clear_session(value.key) == 1
            second = asyncio.create_task(asyncio.to_thread(request, value, kind, 2))
            replacement_id = await pending(value, kind)
            assert replacement_id != original_id
            gate.set()
            old = await asyncio.wait_for(first, 3)
            assert (old["choice"] == "deny") if kind == "approval" else (old == (clarify_gateway.CANCELLED, False))
            assert value.adapter._pending_conversation_control(value.source)
            if kind == "approval":
                assert approval.resolve_gateway_approval(value.key, "once", request_id=original_id) == 0
                assert [r["request_id"] for r in approval.list_gateway_approvals(value.key)] == [replacement_id]
                assert approval.resolve_gateway_approval(value.key, "once", request_id=replacement_id) == 1
                assert (await asyncio.wait_for(second, 3))["choice"] == "once"
            else:
                assert not clarify_gateway.resolve_gateway_clarify(original_id, "First")
                assert clarify_gateway.get_pending_for_session(value.key, include_choice_prompts=True).clarify_id == replacement_id
                assert clarify_gateway.resolve_gateway_clarify(replacement_id, "First")
                assert await asyncio.wait_for(second, 3) == ("First", True)
            assert not value.adapter._pending_conversation_control(value.source)
        finally:
            gate.set()
            cleanup(value)
            approval.clear_session(value.key)  # test isolation only, not product cancellation
            await asyncio.gather(*([first] + ([second] if second else [])), return_exceptions=True)
    asyncio.run(run())


def test_late_failed_native_clarify_delivery_cannot_clear_replacement(tmp_path, monkeypatch):
    async def run():
        from gateway import run_turn_runner_clarify_delivery as delivery
        value = prompt_host(tmp_path, monkeypatch)
        monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: 3)
        monkeypatch.setattr(delivery, "SEND_ACK_WINDOW", .03)
        release = threading.Event()
        calls = []
        scheduled = []
        schedule = value.runner._schedule

        def observe_schedule(*args, **kwargs):
            future = schedule(*args, **kwargs)
            scheduled.append(future)
            return future

        monkeypatch.setattr(value.runner, "_schedule", observe_schedule)

        def send(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                assert release.wait(3)
                raise RuntimeError("synthetic late transport rejection")
            return NS(id="replacement-prompt", delivery_status="queued")

        value.identity.send_text.side_effect = send
        first = asyncio.create_task(asyncio.to_thread(request, value, "clarify", 1))
        second = None
        try:
            old_id = await pending(value, "clarify")
            old_entry = clarify_gateway.get_pending_for_session(value.key, include_choice_prompts=True)
            for _ in range(200):
                if old_entry.event._cond._waiters:
                    break
                await asyncio.sleep(.005)
            assert old_entry.event._cond._waiters and len(calls) == 1
            assert clarify_gateway.clear_session(value.key) == 1
            assert await asyncio.wait_for(first, 2) == (clarify_gateway.CANCELLED, False)
            second = asyncio.create_task(asyncio.to_thread(request, value, "clarify", 2))
            new_id = await pending(value, "clarify")
            assert new_id != old_id
            release.set()
            # Observe the actual scheduled old transport future and its native
            # callbacks, not an arbitrary delay after the SDK thread returns.
            assert not (await asyncio.wait_for(asyncio.wrap_future(scheduled[0]), 2)).success
            assert clarify_gateway.get_pending_for_session(value.key, include_choice_prompts=True).clarify_id == new_id
            assert not clarify_gateway.resolve_gateway_clarify(old_id, "First")
            assert clarify_gateway.resolve_gateway_clarify(new_id, "Second")
            assert await asyncio.wait_for(second, 2) == ("Second", True)
            assert len(calls) == 2, "No retry of the retired prompt"
            assert not value.adapter._pending_conversation_control(value.source)
        finally:
            release.set()
            cleanup(value)
            await asyncio.gather(*([first] + ([second] if second else [])), return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["approval", "clarify"])
def test_actual_native_run_finalizer_retires_pending_request_on_failure(tmp_path, monkeypatch, kind):
    async def run():
        value = prompt_host(tmp_path, monkeypatch)
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 3)
        monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: 3)
        value.owner._consume_pending_native_image_paths = lambda _: None
        task = asyncio.create_task(asyncio.to_thread(request, value, kind))
        try:
            original_id = await pending(value, kind)
            if kind == "clarify":
                entry = clarify_gateway.get_pending_for_session(value.key, include_choice_prompts=True)
                for _ in range(200):
                    if entry.event._cond._waiters:
                        break
                    await asyncio.sleep(.005)
                assert entry.event._cond._waiters

            def failed_model(*args, **kwargs):
                raise RuntimeError("synthetic native run ended")

            # Invoke the real per-run finally: unregister_gateway_notify and
            # clarify clear_session are not replaced by test implementations.
            with pytest.raises(RuntimeError, match="synthetic native run ended"):
                await asyncio.to_thread(value.runner._run_conversation_with_approval,
                    NS(run_conversation=failed_model), [], None, None, None)
            result = await asyncio.wait_for(task, 2)
            assert not value.adapter._pending_conversation_control(value.source)
            if kind == "approval":
                assert result["choice"] is None and result.get("cancelled")
                assert approval.resolve_gateway_approval(value.key, "once", request_id=original_id) == 0
            else:
                assert result == (clarify_gateway.CANCELLED, False)
                assert not clarify_gateway.resolve_gateway_clarify(original_id, "First")
        finally:
            cleanup(value)
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
