"""Stop races use synthetic receipts and deterministic await boundaries."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tests import test_native_turns as native_harness
from tests.test_native_turns import harness, receipt, settle, started, uid

factory = native_harness.factory


def stop_receipt(mode):
    event = receipt(10, mode=mode)
    event.text = event.metadata["inkbox_reply_route"]["raw_text"] = "/stop"
    return event


@pytest.mark.parametrize("mode", ["slack", "imessage"])
@pytest.mark.parametrize("failure", ["callback", "exception"])
@pytest.mark.parametrize("finalizer_first", [False, True])
def test_stop_fence_racing_finalizer_releases_only_new_work(factory, tmp_path, monkeypatch, mode, failure, finalizer_first):
    async def run():
        value = await harness(factory, tmp_path)
        queue = value.queue
        finish = asyncio.Event()
        original_handle = value.adapter.handle_message

        async def handle(event):
            if event.message_id != uid(1):
                return await original_handle(event)
            event._gateway_accepted = True
            value.inputs.append(event)
            await value.adapter.on_processing_start(event)
            await finish.wait()
            if failure == "exception":
                raise RuntimeError("synthetic interrupted worker")
            await value.adapter.on_processing_complete(event, "failure")

        monkeypatch.setattr(value.adapter, "handle_message", handle)
        await queue.accept(receipt(1, mode=mode))
        await started(value)
        await queue.accept(receipt(2, mode=mode))
        row = next(iter(queue.rows.values()))

        async def fence(*args, **kwargs):
            if finalizer_first:
                finish.set()
                await settle(queue)
            return True

        async def forward(*args):
            finish.set()
            await settle(queue)
            # A new receipt arriving while the stop is forwarding must not run
            # until the control has finished, nor be swept into the old stop.
            await queue.accept(receipt(3, mode=mode))
            await asyncio.sleep(.03)
            assert [event.message_id for event in value.inputs] == [uid(1)]

        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", fence)
        monkeypatch.setattr(queue, "_forward_control", forward)
        try:
            await queue.accept(stop_receipt(mode))
            await settle(queue)
            assert not row.get("blocked")
            assert [event.message_id for event in value.inputs] == [uid(1), uid(3)]
            assert [turn["state"] for turn in row["turns"]] == ["cancelled", "cancelled", "done"]
            assert row["turns"][0]["fenced"] is True
            await queue.accept(receipt(3, mode=mode))
            await settle(queue)
            assert len(value.inputs) == 2
            saved = json.loads((queue.root / (row["key"] + ".json")).read_text())
            assert not saved.get("blocked")
        finally:
            finish.set()
            await queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["slack", "imessage"])
@pytest.mark.parametrize("unsafe", [None, "unfenced", "explicit", "media", "other"])
def test_stale_fenced_stop_recovers_without_replaying_cancelled_turn(factory, tmp_path, monkeypatch, mode, unsafe):
    async def run():
        value = await harness(factory, tmp_path)
        queue = value.queue
        with monkeypatch.context() as patch:
            patch.setattr(queue, "_kick", lambda row: None)
            await queue.accept(receipt(1, mode=mode))
        row = next(iter(queue.rows.values()))
        turn = row["turns"][0]
        turn.update(state="cancelled", fenced=unsafe != "unfenced")
        row.update(blocked=True, error="Original native outcome is uncertain; verify worker ownership before continuing")
        if unsafe == "explicit":
            turn["explicit_delivery_state"] = "uncertain"
        elif unsafe == "media":
            turn["media_deliveries"] = {"synthetic": {"state": "sending"}}
        elif unsafe == "other":
            with monkeypatch.context() as patch:
                patch.setattr(queue, "_kick", lambda row: None)
                await queue.accept(receipt(2, mode=mode))
            row["turns"][-1].update(state="uncertain", fenced=False)
            row["blocked"] = True
        queue._save(row)
        fence = AsyncMock(return_value=False)
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", fence)
        try:
            await queue.accept(receipt(3, mode=mode))
            await settle(queue)
            assert bool(row.get("blocked")) is bool(unsafe)
            assert [event.message_id for event in value.inputs] == ([] if unsafe else [uid(3)])
            assert turn["state"] == "cancelled"
            if not unsafe:
                fence.assert_not_awaited()
                assert "error" not in row
        finally:
            await queue.close()
        # The same safety decision survives loading the durable checkpoint.
        restored = native_harness.NativeTurns(value.adapter, queue.root)
        monkeypatch.setattr(restored, "_kick", lambda row: None)
        await restored.start()
        try:
            assert bool(next(iter(restored.rows.values())).get("blocked")) is bool(unsafe)
        finally:
            await restored.close()
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["batch", "idle"])
def test_stop_during_pending_admission_never_resurrects_cancelled_turn(factory, tmp_path, monkeypatch, boundary):
    async def run():
        value = await harness(factory, tmp_path)
        queue = value.queue
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(*args):
            entered.set()
            await release.wait()

        monkeypatch.setattr(queue, "_batch" if boundary == "batch" else "_wait_for_idle", pause)
        monkeypatch.setattr(queue, "_forward_control", AsyncMock())
        try:
            await queue.accept(receipt(1))
            await asyncio.wait_for(entered.wait(), 1)
            await queue.accept(stop_receipt("imessage"))
            release.set()
            await settle(queue)
            assert value.inputs == []
            assert next(iter(queue.rows.values()))["turns"][0]["state"] == "cancelled"
        finally:
            release.set()
            await queue.close()
    asyncio.run(run())
