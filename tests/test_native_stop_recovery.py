"""Stop races use synthetic receipts and deterministic await boundaries."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tests import test_native_turns as native_harness
from tests.test_native_turns import harness, receipt, settle, started, uid

factory = native_harness.factory
isolated_home = native_harness.isolated_home


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


@pytest.mark.parametrize("mode", ["slack", "imessage"])
@pytest.mark.parametrize("evidence", [None, "negative_fence", "session", "answer", "media", "explicit"])
def test_legacy_cancelled_checkpoint_preserves_admission_evidence(factory, tmp_path, monkeypatch, mode, evidence):
    async def run():
        value = await harness(factory, tmp_path)
        with monkeypatch.context() as patch:
            patch.setattr(value.queue, "_kick", lambda row: None)
            await value.queue.accept(receipt(1, mode=mode))
            await value.queue.accept(receipt(2, mode=mode))
        row = next(iter(value.queue.rows.values()))
        original = row["turns"][0]
        # Persist the earlier checkpoint schema: a pending stop did not write
        # fenced, while an attempted active stop always wrote its boolean result.
        original["state"] = "cancelled"
        if evidence == "negative_fence":
            original["fenced"] = False
        elif evidence == "session":
            original["session_id"] = "synthetic-worker-session"
        elif evidence == "answer":
            original["answer"] = "Saved answer"
        elif evidence == "media":
            original["media_deliveries"] = {"synthetic": {"state": "sending"}}
        elif evidence == "explicit":
            original["explicit_delivery_state"] = "uncertain"
        value.queue._save(row)
        await value.queue.close()
        for _ in range(2):
            restored = native_harness.NativeTurns(value.adapter, value.queue.root)
            value.adapter._native_turns = restored
            await restored.start()
            await settle(restored)
            saved = next(iter(restored.rows.values()))
            assert saved["turns"][0]["state"] == "cancelled"
            assert saved["turns"][0]["fenced"] is (evidence is None)
            assert bool(saved.get("blocked")) is (evidence is not None)
            assert [event.message_id for event in value.inputs] == ([] if evidence else [uid(2)])
            await restored.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["slack", "imessage"])
def test_rejected_admission_is_safely_cancelled_across_restart(factory, tmp_path, monkeypatch, mode):
    async def run():
        value = await harness(factory, tmp_path)
        handle = value.adapter.handle_message

        async def reject(event):
            event._gateway_accepted = False

        monkeypatch.setattr(value.adapter, "handle_message", reject)
        await value.queue.accept(receipt(1, mode=mode))
        await settle(value.queue)
        await value.queue.close()
        monkeypatch.setattr(value.adapter, "handle_message", handle)
        restored = native_harness.NativeTurns(value.adapter, value.queue.root)
        value.adapter._native_turns = restored
        await restored.start()
        await restored.accept(receipt(2, mode=mode))
        await settle(restored)
        row = next(iter(restored.rows.values()))
        assert [turn["state"] for turn in row["turns"]] == ["cancelled", "done"]
        assert row["turns"][0]["fenced"] is True
        assert not row.get("blocked")
        assert [event.message_id for event in value.inputs] == [uid(2)]
        await restored.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown", "in_progress"])
def test_confirmed_stop_preserves_slack_upload_outcome(factory, tmp_path, monkeypatch, status):
    from inkbox_plugin.slack import file_payload
    from tests.test_slack_media import operation

    async def run():
        finish = asyncio.Event()
        value = await harness(factory, tmp_path, gate=finish)
        await value.queue.accept(receipt(1, mode="slack"))
        await started(value)
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        path = tmp_path / "attachment.txt"
        path.write_text("Synthetic attachment")
        value.adapter._inkbox.slack.upload_file.return_value = operation(status)
        result = await value.queue.send_slack_media(row, turn, file_payload(str(path)))
        assert result.success is (status == "succeeded")

        async def fence(*args, **kwargs):
            finish.set()
            await settle(value.queue)
            return True

        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", fence)
        monkeypatch.setattr(value.queue, "_forward_control", AsyncMock())
        await value.queue.accept(stop_receipt("slack"))
        await value.queue.accept(receipt(2, mode="slack"))
        await settle(value.queue)
        assert turn["state"] == "cancelled" and turn["fenced"]
        settled = status in {"succeeded", "failed"}
        assert bool(row.get("blocked")) is (not settled)
        assert [event.message_id for event in value.inputs] == ([uid(1), uid(2)] if settled else [uid(1)])
        assert next(iter(turn["media_deliveries"].values()))["operation"]["status"] == status
        value.adapter._inkbox.slack.upload_file.assert_called_once()
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["slack", "imessage"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_confirmed_stop_forward_failure_does_not_retain_submission_gate(factory, tmp_path, monkeypatch, mode, failure):
    async def run():
        finish = asyncio.Event()
        value = await harness(factory, tmp_path, gate=finish)
        await value.queue.accept(receipt(1, mode=mode))
        await started(value)
        await value.queue.accept(receipt(2, mode=mode))

        async def fence(*args, **kwargs):
            finish.set()
            await settle(value.queue)
            return True

        forward = AsyncMock(side_effect=failure("synthetic control forwarding failure"))
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", fence)
        monkeypatch.setattr(value.queue, "_forward_control", forward)
        try:
            with pytest.raises(failure):
                await value.queue.accept(stop_receipt(mode))
            await value.queue.accept(stop_receipt(mode))  # Ambiguous control is acknowledged, never replayed.
            await value.queue.accept(receipt(3, mode=mode))
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            assert row["controls"][uid(10)] == "uncertain"
            assert not row.get("blocked")
            assert [event.message_id for event in value.inputs] == [uid(1), uid(3)]
            assert [turn["state"] for turn in row["turns"]] == ["cancelled", "cancelled", "done"]
            forward.assert_awaited_once()
        finally:
            finish.set()
            await value.queue.close()
            await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["slack", "imessage"])
def test_stop_before_host_dispatch_records_positive_fence(factory, tmp_path, monkeypatch, mode):
    async def run():
        value = await harness(factory, tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        notify = value.queue._notify

        async def delayed_notify(row, turn, state):
            if turn["id"] == uid(1) and state == "accepted":
                entered.set()
                await release.wait()
            await notify(row, turn, state)

        fence = AsyncMock(return_value=False)
        monkeypatch.setattr(value.queue, "_notify", delayed_notify)
        monkeypatch.setattr(value.queue, "_forward_control", AsyncMock())
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", fence)
        try:
            await value.queue.accept(receipt(1, mode=mode))
            await asyncio.wait_for(entered.wait(), 1)
            await value.queue.accept(stop_receipt(mode))
            fence.assert_not_awaited()  # The host never received this owned event.
            release.set()
            await settle(value.queue)
            await value.queue.accept(receipt(2, mode=mode))
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            assert row["turns"][0]["state"] == "cancelled"
            assert row["turns"][0]["fenced"] is True
            assert not row.get("blocked")
            assert [event.message_id for event in value.inputs] == [uid(2)]
        finally:
            release.set()
            await value.queue.close()
            await value.adapter._slack_activity.close()
    asyncio.run(run())
