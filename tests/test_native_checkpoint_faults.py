"""Primary checkpoint failures retain the last durable native side-effect proof."""
import asyncio
import json

import pytest

from inkbox_plugin.native_turns import NativeTurns
from tests import test_companion as base_harness
from tests.test_native_turns import harness, receipt, settle

factory = base_harness.factory


@pytest.mark.parametrize("boundary", ["before_publish", "after_publish"])
@pytest.mark.parametrize("stage", ["answer", "send_admission", "accepted_text", "accepted_media", "final_commit"])
def test_primary_checkpoint_fault_restart_never_replays_an_unknown_effect(factory, tmp_path, monkeypatch, stage, boundary):
    async def run():
        value = await harness(factory, tmp_path)
        # Reserve a real receipt without starting the native model. The fixture
        # then starts at the corresponding already-admitted worker boundary.
        monkeypatch.setattr(value.queue, "_kick", lambda row: None)
        await value.queue.accept(receipt())
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        turn["state"] = "running"
        value.queue.active[row["chat_id"]] = turn
        event = value.queue._event_from_turn(row, turn)
        value.queue._save(row)
        path = value.queue.root / (row["key"] + ".json")
        answer = "checkpointed answer"
        if stage != "answer":
            value.queue.capture_result(event, answer)
        if stage == "final_commit":
            # The accepted send is durable before the final queue settlement.
            assert (await value.queue._send(row, turn, answer)).success
            turn["state"] = "answer_ready"
            value.queue._save(row)
        save = value.queue._save
        faulted = []

        def target():
            return {
                "answer": turn["state"] == "answer_ready",
                "send_admission": turn["state"] == "sending",
                "accepted_text": bool(turn.get("sent")),
                "accepted_media": any(d.get("state") == "sent" for d in turn.get("media_deliveries", {}).values()),
                "final_commit": turn["state"] == "done",
            }[stage]

        def failing_save(candidate):
            if candidate is row and (faulted or target()):
                if not faulted:
                    faulted.append(True)
                    if boundary == "after_publish":
                        save(candidate)
                # Storage remains unavailable through error handling. This
                # prevents a later cleanup write from erasing the crash window.
                raise OSError("synthetic primary checkpoint unavailable")
            save(candidate)

        monkeypatch.setattr(value.queue, "_save", failing_save)
        with pytest.raises(OSError):
            if stage == "answer":
                value.queue.capture_result(event, answer)
            elif stage == "accepted_media":
                await value.queue.send_media(row, turn, value.adapter._reply_identity,
                    {"text": "picture", "media_urls": ["https://example.com/synthetic.png"]}, "synthetic.png")
            elif stage == "final_commit":
                await value.queue._recover_answer(row, turn)
            else:
                await value.queue._send(row, turn, answer)
        assert faulted
        disk = json.loads(path.read_text())["turns"][0]
        calls_at_crash = value.adapter._reply_identity.send_imessage.call_count
        assert calls_at_crash == (1 if stage in {"accepted_text", "accepted_media", "final_commit"} else 0)
        if stage == "answer":
            assert ("answer" in disk) is (boundary == "after_publish")
        elif stage == "accepted_text":
            assert bool(disk.get("sent")) is (boundary == "after_publish")
        elif stage == "accepted_media":
            assert next(iter(disk["media_deliveries"].values()))["state"] == ("sent" if boundary == "after_publish" else "sending")
        # No background worker exists; close releases the real process lease
        # without saving the volatile row. Recovery reads only published bytes.
        await value.queue.close()
        resumed = NativeTurns(value.adapter, tmp_path / "native")
        value.adapter._native_turns = resumed
        await resumed.start()
        await settle(resumed)
        restored = next(iter(resumed.rows.values()))["turns"][0]
        safely_unsent = ((stage == "answer" and boundary == "after_publish")
                        or (stage == "send_admission" and boundary == "before_publish"))
        # Media's accepted receipt is independently durable; a saved unsent
        # textual answer may still be sent, but the media itself cannot repeat.
        accepted_media = stage == "accepted_media" and boundary == "after_publish"
        expected = calls_at_crash + int(safely_unsent or accepted_media)
        assert value.adapter._reply_identity.send_imessage.call_count == expected
        assert value.inputs == [], "Recovery must never create another model turn"
        if safely_unsent or accepted_media or stage == "final_commit" or (stage == "accepted_text" and boundary == "after_publish"):
            assert restored["state"] == "done"
        else:
            assert restored["state"] == "uncertain"
        await resumed.close()
        again = NativeTurns(value.adapter, tmp_path / "native")
        value.adapter._native_turns = again
        await again.start()
        await settle(again)
        assert value.adapter._reply_identity.send_imessage.call_count == expected
        assert value.inputs == []
        await again.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("effect", ["media", "explicit"])
@pytest.mark.parametrize("effect_state", ["sending", "uncertain"])
@pytest.mark.parametrize("fenced", [False, True])
def test_nested_effect_restart_preserves_receipts_and_positive_fence(factory, tmp_path, monkeypatch, effect, effect_state, fenced):
    async def run():
        value = await harness(factory, tmp_path)
        monkeypatch.setattr(value.queue, "_kick", lambda row: None)
        await value.queue.accept(receipt(1))
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        turn.update(state="answer_ready", answer="saved original answer")
        if effect == "media":
            turn["media_deliveries"] = {"original": {"state": effect_state}}
        else:
            turn["explicit_delivery_state"] = effect_state
        if fenced:
            # Same durable proof shape emitted by the real native worker
            # finalizer contract, not an absent owner/process-lock assumption.
            turn["worker_completion"] = {"kind": "native_worker_finalizer", "source_id": turn["id"],
                "session_key": "original-native-session", "generation": 1}
        value.queue._save(row)
        await value.queue.accept(receipt(2))
        await value.queue.close()
        for _ in range(2):
            resumed = NativeTurns(value.adapter, tmp_path / "native")
            value.adapter._native_turns = resumed
            await resumed.start()
            await settle(resumed)
            saved = next(iter(resumed.rows.values()))
            original = saved["turns"][0]
            assert original["state"] == "uncertain" and original["answer"] == "saved original answer"
            assert original["source_ids"] == [receipt(1).message_id]
            if effect == "media":
                assert original["media_deliveries"] == {"original": {"state": effect_state}}
            else:
                assert original["explicit_delivery_state"] == effect_state
            assert bool(original.get("fenced")) is fenced
            assert bool(saved.get("blocked")) is not fenced
            assert len(value.inputs) == int(fenced)
            assert value.adapter._reply_identity.send_imessage.call_count == int(fenced)
            if fenced:
                assert value.inputs[0].message_id == receipt(2).message_id
                assert value.adapter._reply_identity.send_imessage.call_args.kwargs["reply_to_message_id"] == receipt(2).message_id
            await resumed.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("state", ["done", "cancelled", "quarantined"])
@pytest.mark.parametrize("effect", ["media", "explicit"])
def test_nested_uncertainty_does_not_rewrite_terminal_turn(factory, tmp_path, monkeypatch, state, effect):
    async def run():
        value = await harness(factory, tmp_path)
        monkeypatch.setattr(value.queue, "_kick", lambda row: None)
        await value.queue.accept(receipt())
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        turn.update(state=state, answer="retained terminal answer", sent={"content": "retained terminal answer", "message_id": "accepted-id"})
        if effect == "media":
            turn["media_deliveries"] = {"original": {"state": "sending"}}
        else:
            turn["explicit_delivery_state"] = "sending"
        value.queue._save(row)
        await value.queue.close()
        resumed = NativeTurns(value.adapter, tmp_path / "native")
        value.adapter._native_turns = resumed
        await resumed.start()
        await settle(resumed)
        restored = next(iter(resumed.rows.values()))["turns"][0]
        assert restored["state"] == state
        assert restored["sent"] == {"content": "retained terminal answer", "message_id": "accepted-id"}
        assert value.inputs == []
        value.adapter._reply_identity.send_imessage.assert_not_called()
        await resumed.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())
