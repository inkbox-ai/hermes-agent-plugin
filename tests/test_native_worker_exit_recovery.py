"""Synthetic exact-worker Stop regressions (no live gateway or sends)."""
import asyncio
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tests import test_native_turns as native_harness
from tests.test_native_turns import harness, receipt, settle, started, uid
from tests.test_native_stop_recovery import stop_receipt
from inkbox_plugin.host_fencing import observe_native_workers

factory = native_harness.factory
isolated_home = native_harness.isolated_home


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("unsafe", [None, "explicit", "media", "reply"])
@pytest.mark.parametrize("mode", ["slack", "imessage"])
def test_stop_recovers_only_exact_exited_worker(factory, tmp_path, monkeypatch, startup, detached, unsafe, mode):
    async def run():
        value = await harness(factory, tmp_path)
        # Offline unit suite has no host process registry; model its read-only API.
        import sys
        monkeypatch.setitem(sys.modules, "tools.process_registry", NS(
            process_registry=NS(snapshot_running_ids=lambda task_id: set())))
        queue = value.queue
        queue.max_burst_sources = 1  # Keep successor receipts separate in both channels.
        completion_timeout = queue.completion_timeout
        if detached:
            queue.completion_timeout = .06
        owner = queue._host_owner()
        state = NS(turn=NS(event=None))
        future = asyncio.get_running_loop().create_future()
        worker = NS(executor_task=future, worker_done=threading.Event(), task_id="synthetic-session", process_baseline=set())
        owner._peek_session_state = lambda key: state
        owner._is_session_run_current = lambda key, generation: False  # Stop invalidated it.
        owner._run_agent_start_turn_worker = lambda context, run_sync: worker
        observe_native_workers(owner)
        original = value.adapter.handle_message
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handle(event):
            if event.message_id != uid(1):
                if detached:
                    await asyncio.sleep(.1)  # Successors may outlive the forced detach deadline.
                return await original(event)
            event._gateway_accepted = True
            value.inputs.append(event)
            await value.adapter.on_processing_start(event)
            if startup:
                await release.wait()  # Worker creation can follow Stop admission.
            state.turn.event = event
            owner._run_agent_start_turn_worker(NS(session_key="actual-host-key", session_id="synthetic-session",
                run_generation=1, inbound_message_id=event.message_id), lambda: None)
            entered.set()
            await asyncio.shield(future)
            state.turn.event = None
            # The host discards stale output, so capture_result is never called.
            await value.adapter.on_processing_complete(event, "success")

        monkeypatch.setattr(value.adapter, "handle_message", handle)
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", AsyncMock(return_value=False))
        monkeypatch.setattr(queue, "_forward_control", AsyncMock())
        try:
            await queue.accept(receipt(1, mode=mode))
            await started(value)
            await queue.accept(stop_receipt(mode))
            row = next(iter(queue.rows.values()))
            turn = row["turns"][0]
            assert row.get("blocked") and turn["fenced"] is False
            if unsafe == "explicit":
                turn["explicit_delivery_state"] = "uncertain"
            elif unsafe == "media":
                turn["media_deliveries"] = {"upload": {"state": "uncertain"}}
            elif unsafe == "reply":
                turn["reply_delivery_state"] = "uncertain"
            await queue.accept(receipt(2, mode=mode))
            await queue.accept(receipt(3, mode=mode))
            release.set()
            await entered.wait()
            await asyncio.sleep(.02)
            assert len(value.inputs) == 1  # Invalidated generation is not exit proof.
            if detached:
                await settle(queue)  # The queue has dropped its active event.
                assert not queue.active and turn["state"] == "cancelled"
                queue.completion_timeout = completion_timeout
            worker.worker_done.set()
            future.set_result(None)
            await asyncio.sleep(.02)
            await settle(queue)
            assert turn["state"] == "cancelled"
            assert turn["fenced"] is True
            assert bool(row.get("blocked")) is bool(unsafe)
            expected = [uid(1)] if unsafe else [uid(1), uid(2), uid(3)]
            assert [event.message_id for event in value.inputs] == expected
            await queue.accept(receipt(2, mode=mode))
            await queue.accept(receipt(3, mode=mode))
            await settle(queue)
            assert [event.message_id for event in value.inputs] == expected
            assert "worker_completion" not in turn  # Exit is NOT delivery reconciliation.
            await queue.close()
            restored = native_harness.NativeTurns(value.adapter, queue.root)
            monkeypatch.setattr(restored, "_kick", lambda row: None)
            await restored.start()
            assert bool(next(iter(restored.rows.values())).get("blocked")) is bool(unsafe)
            await restored.close()
        finally:
            release.set()
            if not future.done():
                future.set_result(None)
            await queue.close()
            await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("missing", ["receipt", "owner", "source", "future", "finalizer", "cancelled", "children"])
def test_stopped_worker_proof_fails_closed(monkeypatch, missing):
    import sys
    from inkbox_plugin.host_fencing import stopped_worker_proof

    async def run():
        owner = object()
        future = asyncio.get_running_loop().create_future()
        done = threading.Event()
        done.set()
        worker = NS(executor_task=future, worker_done=done, task_id="original", process_baseline=set())
        event = NS(message_id="source", _inkbox_native_worker=(worker, "original-thread", 1, "source"),
                   _inkbox_native_worker_owner=owner)
        monkeypatch.setitem(sys.modules, "tools.process_registry", NS(process_registry=NS(
            snapshot_running_ids=lambda task_id: {"still-running"} if missing == "children" else set())))
        if missing == "cancelled":
            future.cancel()
        elif missing != "future":
            future.set_result(None)
        if missing == "receipt":
            event._inkbox_native_worker = None
        elif missing == "owner":
            event._inkbox_native_worker_owner = object()
        elif missing == "source":
            event.message_id = "other-thread-source"
        elif missing == "finalizer":
            done.clear()
        assert stopped_worker_proof(owner, event) is None
    asyncio.run(run())
