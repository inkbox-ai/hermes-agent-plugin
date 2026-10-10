"""Native callbacks retain context, never wake, replay, or retarget a turn."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from inkbox_plugin.native_turns import NativeTurns
from tests import test_companion as base_harness
from tests.test_companion import uid
from tests.test_native_turns import harness, receipt, settle

factory = base_harness.factory


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


def callback(number=800, *, conversation=30, identity=100, kind="imessage.delivery_failed", direction="outbound"):
    return {"event_type": kind, "data": {"identity_id": uid(identity), "message": {
        "id": uid(number), "conversation_id": uid(conversation) if conversation else None,
        "direction": direction, "error_detail": "Provider text must not become instructions"}}}


def test_completed_send_failure_is_once_only_context_not_a_retry(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        identity = value.adapter._reply_identity
        await value.queue.accept(receipt(1))
        await settle(value.queue)
        turn = next(iter(value.queue.rows.values()))["turns"][0]
        before = copy.deepcopy(turn)
        for _ in range(2):
            response = await value.adapter._on_imessage_lifecycle(callback())
            assert response.status == 200
        assert len(value.inputs) == 1 and identity.send_imessage.call_count == 1
        assert turn == before and not value.queue.tasks
        identity.send_imessage.return_value = NS(id=uid(801))
        await value.queue.accept(receipt(2))
        await settle(value.queue)
        assert len(value.inputs) == 2 and identity.send_imessage.call_count == 2
        assert value.inputs[1].text.count(uid(800)) == 1
        assert "Do not automatically resend" in value.inputs[1].text
        assert "Provider text" not in value.inputs[1].text
        identity.send_imessage.return_value = NS(id=uid(802))
        await value.queue.accept(receipt(3))
        await settle(value.queue)
        assert uid(800) not in value.inputs[2].text
        assert [call.kwargs["reply_to_message_id"] for call in identity.send_imessage.call_args_list] == [uid(1), uid(2), uid(3)]
        await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("conversation", [None, 30])
def test_callback_before_send_response_preserves_failure_and_actual_nullable_ancestry(factory, tmp_path, conversation):
    async def run():
        value = await harness(factory, tmp_path)
        def send(**kwargs):
            assert value.queue.record_delivery_failure(callback(conversation=conversation))
            return NS(id=uid(800), reply_to_message_id=None, thread_id=None, thread_root_message_id=None)
        value.adapter._reply_identity.send_imessage.side_effect = send
        await value.queue.accept(receipt(1))
        await settle(value.queue)
        status = value.queue.delivery_status[uid(800)]
        assert status["failed"] and status["source_id"] == uid(1)
        assert status["conversation_id"] == uid(30) and status["reply_target"] == uid(1)
        assert status["ancestry"] == {"id": uid(800), "reply_to_message_id": None, "thread_id": None, "thread_root_message_id": None}
        assert len(value.inputs) == 1 and value.adapter._reply_identity.send_imessage.call_count == 1
        assert uid(800) not in value.inputs[0].text
        await value.queue.close()
    asyncio.run(run())


def test_unmatched_proactive_failure_survives_restart_without_wake_and_is_conversation_scoped(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        for envelope in (callback(900), callback(900), callback(900, kind="imessage.delivered"), callback(901, conversation=31)):
            value.queue.record_delivery_failure(envelope)
        assert not value.inputs and not value.queue.rows and not value.queue.tasks
        value.adapter._reply_identity.send_imessage.assert_not_called()
        await value.queue.close()
        value.adapter._native_turns = queue = NativeTurns(value.adapter, tmp_path / "native")
        value.queue = queue
        async def restarted_handler(event):
            event._gateway_accepted = True
            value.inputs.append(event)
            await value.adapter.on_processing_start(event)
            queue.capture_result(event, "Answer after restart")
            result = await value.adapter.send(event.source.chat_id, "Answer after restart", reply_to=event.message_id)
            await value.adapter.on_processing_complete(event, "success" if result.success else "failure")
        value.adapter.handle_message = restarted_handler
        queue.quiet_seconds = .01
        await queue.start()
        await settle(queue)
        assert not value.inputs and not queue.rows
        await queue.accept(receipt(1))
        await settle(queue)
        assert uid(900) in value.inputs[0].text and uid(901) not in value.inputs[0].text
        assert queue.delivery_status[uid(900)]["context_source_id"] == uid(1)
        assert "context_source_id" not in queue.delivery_status[uid(901)]
        await queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["identity", "inbound", "read", "delivered", "conversation", "bad_id"])
def test_invalid_or_nonfailure_callbacks_never_create_context(factory, tmp_path, change):
    async def run():
        value = await harness(factory, tmp_path)
        await value.queue.accept(receipt(1))
        await settle(value.queue)
        envelope = callback()
        if change == "identity":
            envelope["data"]["identity_id"] = uid(101)
        elif change == "inbound":
            envelope["data"]["message"]["direction"] = "inbound"
        elif change in {"read", "delivered"}:
            envelope["event_type"] = "imessage." + change
        elif change == "conversation":
            envelope["data"]["message"]["conversation_id"] = uid(31)
        else:
            envelope["data"]["message"]["id"] = "unsafe\nidentifier"
        value.queue.record_delivery_failure(envelope)
        assert not any(status.get("failed") for status in value.queue.delivery_status.values())
        assert len(value.inputs) == 1 and value.adapter._reply_identity.send_imessage.call_count == 1
        await value.queue.close()
    asyncio.run(run())


def test_retained_native_failure_stays_context_only_when_feature_disabled_and_delivered_cleans_baseline(factory, tmp_path, monkeypatch):
    async def run():
        value = await harness(factory, tmp_path)
        await value.queue.accept(receipt(1))
        await settle(value.queue)
        value.adapter._imessage_threaded_replies = False
        value.adapter._note_outbound_delivery_failure = AsyncMock()
        value.adapter._clear_outbound_failures = Mock()
        value.adapter._find_outbound_context = Mock(return_value=None)
        remove = Mock()
        monkeypatch.setattr("inkbox_plugin.adapter.remove_outbound_context", remove)
        await value.adapter._on_imessage_lifecycle(callback())
        await value.adapter._on_imessage_lifecycle(callback(kind="imessage.delivered"))
        value.adapter._note_outbound_delivery_failure.assert_not_called()
        value.adapter._clear_outbound_failures.assert_called_once_with("imessage", uid(30), "")
        remove.assert_called_once_with(uid(800))
        assert len(value.inputs) == 1 and not value.queue.tasks
        await value.queue.close()
    asyncio.run(run())


def test_failure_context_is_bounded_and_remaining_notices_wait_for_next_real_input(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        for number in range(900, 910):
            value.queue.record_delivery_failure(callback(number))
        await value.queue.accept(receipt(1))
        await settle(value.queue)
        assert all(uid(number) in value.inputs[0].text for number in range(900, 908))
        assert all(uid(number) not in value.inputs[0].text for number in (908, 909))
        await value.queue.accept(receipt(2))
        await settle(value.queue)
        assert all(uid(number) not in value.inputs[1].text for number in range(900, 908))
        assert all(uid(number) in value.inputs[1].text for number in (908, 909))
        assert len(value.inputs) == 2 and value.adapter._reply_identity.send_imessage.call_count == 2
        checkpoint = json.loads((tmp_path / "native" / "delivery-status").read_text())
        assert checkpoint["version"] == 1 and all("error_detail" not in item for item in checkpoint["messages"].values())
        await value.queue.close()
    asyncio.run(run())


def test_saved_answer_reconciles_notice_consumption_after_journal_checkpoint_crash(factory, tmp_path, monkeypatch):
    async def run():
        value = await harness(factory, tmp_path)
        value.queue.record_delivery_failure(callback(900))
        # Model completion saves its answer first. Simulate process death before
        # the separate notice-consumption write, without rerunning that answer.
        monkeypatch.setattr(value.queue, "_complete_delivery_context", lambda turn: None)
        await value.queue.accept(receipt(1))
        await settle(value.queue)
        assert uid(900) in value.inputs[0].text
        assert "context_source_id" not in value.queue.delivery_status[uid(900)]
        await value.queue.close()
        recovered = NativeTurns(value.adapter, tmp_path / "native")
        await recovered.start()
        await settle(recovered)
        assert recovered.delivery_status[uid(900)]["context_source_id"] == uid(1)
        assert len(value.inputs) == 1 and value.adapter._reply_identity.send_imessage.call_count == 1
        await recovered.close()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["automatic", "media", "explicit"])
def test_auxiliary_checkpoint_failure_preserves_accepted_send_and_repairs_from_primary(factory, tmp_path, monkeypatch, kind):
    from inkbox_plugin import tools
    from tests.test_native_turns import started
    async def run():
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate)
        identity = value.adapter._reply_identity
        identity.send_imessage.return_value = NS(id=uid(800), reply_to_message_id=None, thread_id=uid(77), thread_root_message_id=None)
        await value.queue.accept(receipt(1))
        await started(value)
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        # Only the auxiliary checkpoint fails: the primary accepted-send proof
        # must already exist before this write is attempted.
        observed = []
        def failed_secondary():
            saved = json.loads((tmp_path / "native" / (row["key"] + ".json")).read_text())["turns"][0]
            accepted = saved.get("sent") if kind == "automatic" else next(iter(saved["media_deliveries"].values())) if kind == "media" else saved["explicit_sends"][0]
            assert accepted["message_id"] == uid(800)
            assert accepted["ancestry"]["thread_id"] == uid(77)
            observed.append(True)
            raise OSError("synthetic auxiliary storage failure")
        monkeypatch.setattr(value.queue, "_save_delivery_status", failed_secondary)
        if kind == "media":
            result = await value.queue.send_media(row, turn, identity, {"text": "Photo", "media_urls": ["https://example.com/photo.png"]}, "photo.png")
            assert result.success
            # Finish normally without another send: the accepted media remains
            # the sole effect whose recovery proof we inspect.
            identity.send_imessage.return_value = NS(id=uid(801))
        elif kind == "explicit":
            # Keep the fixture's two-second host timeout: delivery settles on
            # the first status read while source-validation reads stay intact.
            source_read = identity.get_imessage.side_effect
            identity.get_imessage.side_effect = lambda message_id: (
                NS(id=message_id, status="delivered") if message_id == uid(800) else source_read(message_id)
            )
            monkeypatch.setenv("INKBOX_IMESSAGE_THREADED_REPLIES", "true")
            monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, None, identity))
            result = json.loads(tools.inkbox_send_imessage({"conversationId": uid(30), "text": "answer " + uid(1)}, session_id=turn["session_id"]))
            assert result["ok"]
        gate.set()
        await settle(value.queue)
        assert turn["state"] == "done" and observed
        assert identity.send_imessage.call_count == (2 if kind == "media" else 1)
        assert not row.get("blocked")
        await value.queue.close()
        calls = identity.send_imessage.call_count
        recovered = NativeTurns(value.adapter, tmp_path / "native")
        await recovered.start()
        await settle(recovered)
        assert recovered.delivery_status[uid(800)]["source_id"] == uid(1)
        assert recovered.delivery_status[uid(800)]["ancestry"]["thread_id"] == uid(77)
        assert identity.send_imessage.call_count == calls and len(value.inputs) == 1
        await recovered.close()
    asyncio.run(run())


def test_callback_write_failure_is_not_acknowledged_or_deduplicated_until_durable(factory, tmp_path, monkeypatch):
    async def run():
        value = await harness(factory, tmp_path)
        save = value.queue._save_delivery_status
        writes = []
        def flaky_write():
            writes.append(True)
            if len(writes) == 1:
                raise OSError("synthetic failed receipt write")
            save()
        monkeypatch.setattr(value.queue, "_save_delivery_status", flaky_write)
        with pytest.raises(OSError):
            await value.adapter._on_imessage_lifecycle(callback(900))
        assert uid(900) not in value.queue.delivery_status
        assert (await value.adapter._on_imessage_lifecycle(callback(900))).status == 200
        assert len(writes) == 2
        assert json.loads((tmp_path / "native" / "delivery-status").read_text())["messages"][uid(900)]["failed"]
        assert not value.inputs and not value.queue.rows and not value.queue.tasks
        await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("unmatched_status", [False, True])
def test_disabled_native_mode_preserves_new_baseline_failures_in_historical_conversation(factory, tmp_path, unmatched_status):
    async def run():
        value = await harness(factory, tmp_path)
        await value.queue.accept(receipt(1))
        await settle(value.queue)
        if unmatched_status:
            value.queue.record_delivery_failure(callback(901))
        await value.queue.close()
        value.adapter._imessage_threaded_replies = False
        value.adapter._native_turns = recovered = NativeTurns(value.adapter, tmp_path / "native")
        await recovered.start()
        value.adapter._last_inbound_modality = {}
        value.adapter._last_inbound_imessage = {}
        value.adapter._find_outbound_context = Mock(return_value=None)
        value.adapter._note_outbound_delivery_failure = AsyncMock()
        assert recovered.owns_delivery({"id": uid(800), "conversation_id": uid(30)})
        assert not recovered.owns_delivery({"id": uid(901), "conversation_id": uid(30)})
        await value.adapter._on_imessage_lifecycle(callback(901))
        value.adapter._note_outbound_delivery_failure.assert_awaited_once()
        # Accepted old native sends retain context-only behavior, while new
        # baseline sends in that conversation use their existing lifecycle.
        await value.adapter._on_imessage_lifecycle(callback(800))
        value.adapter._note_outbound_delivery_failure.assert_awaited_once()
        assert len(value.inputs) == 1 and value.adapter._reply_identity.send_imessage.call_count == 1
        await recovered.close()
    asyncio.run(run())
