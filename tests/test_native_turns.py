"""Durable native Hermes admission, answer checkpoint, and source routing."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from gateway.platforms.base import MessageEvent, MessageType

from tests import test_companion as base_harness
from tests.test_companion import uid

factory = base_harness.factory
from inkbox_plugin.native_turns import NativeTurns
from inkbox_plugin.slack_activity import SlackActivity


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


def receipt(number=1, *, mode="imessage", sender="+15555550101", thread=None, media=False):
    source = NS(chat_id="imessage:conversation", chat_type="group", thread_id="imessage:conversation",
                user_id=sender, user_id_alt=sender, user_name=sender, chat_name="Conversation")
    route = {"mode": mode, "chat_id": source.chat_id, "message_id": uid(number), "author": sender,
             "conversation_id": uid(30), "imessage_reply_target": uid(number), "raw_text": f"question {number}",
             "imessage_event_ids": [uid(number)], "imessage_sources": [{"id": uid(number)}],
             "reply_to_message_id": thread, "thread_id": thread, "thread_root_message_id": thread}
    if mode == "slack":
        route.update(connection_id=uid(40), conversation_id="C123", workspace_id="T123", actor_id="U123",
                     message_ts=f"1234567890.{number:06}", thread_ts=thread, source_event_id=uid(number))
    return MessageEvent(text=f"question {number}", message_type=MessageType.TEXT, source=source,
                        message_id=uid(number), metadata={"inkbox_prepared": True, "inkbox_reply_route": route},
                        media_urls=["https://example.com/picture.png"] if media else [])


async def harness(factory, tmp_path, *, gate=None, failure=None, mode="imessage"):
    base = factory("imessage")
    adapter = base.adapter
    adapter._reply_identity = base.identity
    adapter._native_turns = queue = NativeTurns(adapter, tmp_path / "native")
    adapter._slack_enabled = True
    adapter._imessage_threaded_replies = True
    adapter._inkbox.slack = Mock()
    adapter._inkbox.slack.list_connections.return_value = NS(connections=[NS(id=uid(40), identity_id=uid(100), workspace_id="T123", status="connected")])
    adapter._inkbox.slack.send_message.return_value = NS(id=uid(70), status="sent")
    queue.quiet_seconds = .015
    queue.max_burst_seconds = .04
    queue.completion_timeout = 2
    queue.retry_delay = .01
    adapter._slack_activity = SlackActivity(adapter._inkbox.slack, tmp_path / "activity.json")
    for name in ("set_processing_status", "add_reaction", "remove_reaction"):
        getattr(adapter._inkbox.slack, name).return_value = NS(status="succeeded")
    inputs = []
    async def handle(event):
        event._gateway_accepted = True
        inputs.append(event)
        await adapter.on_processing_start(event)
        if gate:
            await gate.wait()
        if failure == "model":
            await adapter.on_processing_complete(event, "failure")
            return
        response = f"answer {event.message_id}"
        queue.capture_result(event, response)
        if failure == "send":
            adapter._reply_identity.send_imessage.side_effect = TimeoutError("unknown")
        result = await adapter.send(event.source.chat_id, response, reply_to=event.message_id)
        await adapter.on_processing_complete(event, "success" if result.success else "failure")
    adapter.handle_message = handle
    await queue.start()
    return NS(base=base, adapter=adapter, queue=queue, inputs=inputs)


async def settle(queue):
    for _ in range(300):
        if not queue.tasks:
            return
        await asyncio.sleep(.01)
    raise AssertionError("native queue did not settle")


async def started(value, count=1):
    for _ in range(100):
        if len(value.inputs) == count:
            return
        await asyncio.sleep(.01)
    raise AssertionError("native host input missing")


def test_batch_first_source_and_followups_do_not_interrupt(factory, tmp_path):
    async def run():
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate)
        await value.queue.accept(receipt(1))
        await value.queue.accept(receipt(2))
        await started(value)
        assert value.inputs[0].text == "question 1\nquestion 2"
        await value.queue.accept(receipt(3))
        await asyncio.sleep(.05)
        assert len(value.inputs) == 1
        gate.set()
        await settle(value.queue)
        assert len(value.inputs) == 2
        calls = value.adapter._reply_identity.send_imessage.call_args_list
        assert [call.kwargs["reply_to_message_id"] for call in calls] == [uid(1), uid(3)]
        assert all(call.kwargs["plain_reply_fallback"] is True for call in calls)
        assert value.inputs[0].source.thread_id == value.inputs[1].source.thread_id
        assert [turn["state"] for row in value.queue.rows.values() for turn in row["turns"]] == ["done", "done"]
        await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("different", ["sender", "thread", "media"])
def test_incompatible_fragments_never_batch(factory, tmp_path, different):
    async def run():
        value = await harness(factory, tmp_path)
        value.base.host._is_user_authorized = lambda source: True
        first, second = receipt(1), receipt(2)
        if different == "sender":
            second = receipt(2, sender="+15555550102")
        elif different == "thread":
            second = receipt(2, thread=uid(99))
        else:
            second = receipt(2, media=True)
        await value.queue.accept(first)
        await value.queue.accept(second)
        await settle(value.queue)
        assert len(value.inputs) == 2
        await value.queue.close()
    asyncio.run(run())


def test_receipt_persisted_before_ack_and_duplicate_no_second_model(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        await value.queue.accept(receipt())
        row = json.loads(next(value.queue.root.glob("*.json")).read_text())
        assert row["turns"][0]["state"] == "pending"
        await value.queue.accept(receipt())
        await settle(value.queue)
        await value.queue.accept(receipt())
        assert len(value.inputs) == 1
        assert value.adapter._reply_identity.send_imessage.call_count == 1
        await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["model", "send"])
def test_uncertain_never_replays_or_falls_back_plainly(factory, tmp_path, failure):
    async def run():
        value = await harness(factory, tmp_path, failure=failure)
        await value.queue.accept(receipt())
        await settle(value.queue)
        assert next(iter(value.queue.rows.values()))["turns"][0]["state"] == "uncertain"
        await value.queue.close()
        recovered = NativeTurns(value.adapter, tmp_path / "native")
        await recovered.start()
        await settle(recovered)
        assert len(value.inputs) == 1
        assert value.adapter._reply_identity.send_imessage.call_count == (1 if failure == "send" else 0)
        await recovered.close()
    asyncio.run(run())


def test_completed_answer_is_saved_before_sdk_send(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        def sending(**kwargs):
            saved = json.loads(next(value.queue.root.glob("*.json")).read_text())["turns"][0]
            assert saved["answer"] == "answer " + uid(1)
            assert saved["state"] == "sending"
            assert saved["route"]["imessage_reply_target"] == uid(1)
            return NS(id=uid(80))
        value.adapter._reply_identity.send_imessage.side_effect = sending
        await value.queue.accept(receipt())
        await settle(value.queue)
        await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("thread", [None, "1234567890.000001"])
def test_slack_activity_tracks_native_outgoing_route(factory, tmp_path, thread):
    async def run():
        value = await harness(factory, tmp_path, mode="slack")
        await value.queue.accept(receipt(mode="slack", thread=thread))
        await settle(value.queue)
        await value.adapter._slack_activity.flush()
        sdk = value.adapter._inkbox.slack
        if thread:
            assert [call.args[3] for call in sdk.set_processing_status.call_args_list] == ["processing", "active"]
            sdk.add_reaction.assert_not_called()
        else:
            assert [call.args[3] for call in sdk.add_reaction.call_args_list] == ["eyes"]
            sdk.set_processing_status.assert_not_called()
        assert sdk.send_message.call_args.kwargs["thread_ts"] == thread
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_native_media_keeps_original_target_and_deduplicates(factory, tmp_path):
    async def run():
        from inkbox_plugin.conversation import reply_route
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate)
        await value.queue.accept(receipt(1))
        await started(value)
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        token = reply_route.set(turn["route"])
        try:
            assert value.queue.media_owner(row["chat_id"]) == (row, turn)
            payload = {"conversation_id": uid(999), "text": "Picture", "media_urls": ["https://example.com/photo.png"]}
            first = await value.queue.send_media(row, turn, value.adapter._reply_identity, payload, "local-photo.png")
            second = await value.queue.send_media(row, turn, value.adapter._reply_identity, payload, "local-photo.png")
            assert first.success and second.message_id == first.message_id
            assert value.adapter._reply_identity.send_imessage.call_count == 1
            sent = value.adapter._reply_identity.send_imessage.call_args.kwargs
            assert sent["conversation_id"] == uid(30)
            assert sent["reply_to_message_id"] == uid(1)
            assert sent["plain_reply_fallback"] is True
            assert sent["idempotency_key"].startswith("hermes:media:")
            turn["state"] = "cancelled"
            with pytest.raises(RuntimeError):
                value.queue.check_media_authority(row, turn)
        finally:
            reply_route.reset(token)
            gate.set()
            await settle(value.queue)
            await value.queue.close()
    asyncio.run(run())


def test_native_controls_are_actor_and_thread_bound_and_consumed_once(factory, tmp_path):
    async def run():
        from unittest.mock import AsyncMock
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate, mode="slack")
        value.base.host._is_user_authorized = lambda source: True
        await value.queue.accept(receipt(1, mode="slack", thread="1234567890.000001"))
        await started(value)
        value.adapter._pending_conversation_control = lambda _: True
        value.adapter._conversation_prompt_reply = lambda _, text: "allow_once" if text == "yes" else None
        value.queue._forward_control = AsyncMock()
        right = receipt(2, mode="slack", thread="1234567890.000001")
        right.metadata["inkbox_reply_route"]["raw_text"] = "yes"
        await value.queue.accept(right)
        await value.queue.accept(right)
        assert value.queue._forward_control.await_count == 1
        assert value.queue._forward_control.call_args.args[1] == "allow_once"
        wrong = receipt(3, mode="slack", thread="1234567890.000009")
        wrong.metadata["inkbox_reply_route"]["raw_text"] = "yes"
        await value.queue.accept(wrong)
        assert value.queue._forward_control.await_count == 1
        value.adapter._pending_conversation_control = lambda _: False
        gate.set()
        await settle(value.queue)
        assert len(value.inputs) == 2  # wrong-thread answer was ordinary queued input, not permission
        await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("proved", [False, True])
def test_fresh_instruction_cancels_only_pending_permission_and_requires_fence(factory, tmp_path, monkeypatch, proved):
    async def run():
        from unittest.mock import AsyncMock
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate)
        await value.queue.accept(receipt(1))
        await started(value)
        value.adapter._pending_conversation_control = lambda _: True
        value.adapter._conversation_prompt_reply = lambda *_: None
        cancel = AsyncMock()
        fence = AsyncMock(return_value=proved)
        monkeypatch.setattr("inkbox_plugin.host_fencing.cancel_pending_permissions", cancel)
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", fence)
        await value.queue.accept(receipt(2))
        row = next(iter(value.queue.rows.values()))
        cancel.assert_awaited_once()
        fence.assert_awaited_once()
        assert row["turns"][1]["state"] == "pending"
        assert bool(row.get("blocked")) is not proved
        assert row["turns"][0]["state"] == ("cancelled" if proved else "running")
        value.adapter._pending_conversation_control = lambda _: False
        gate.set()
        await settle(value.queue)
        await value.queue.close()
    asyncio.run(run())


def test_repeated_restart_never_releases_successor_of_uncertain_turn(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path, failure="send")
        await value.queue.accept(receipt(1))
        await settle(value.queue)
        await value.queue.accept(receipt(2))
        assert len(value.inputs) == 1
        await value.queue.close()
        for _ in range(2):
            recovered = NativeTurns(value.adapter, tmp_path / "native")
            await recovered.start()
            await asyncio.sleep(.02)
            row = next(iter(recovered.rows.values()))
            assert row["blocked"]
            assert [turn["state"] for turn in row["turns"]] == ["uncertain", "pending"]
            assert len(value.inputs) == 1
            await recovered.close()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["sources", "characters"])
def test_text_burst_has_source_and_character_caps(factory, tmp_path, kind, monkeypatch):
    async def run():
        value = await harness(factory, tmp_path)
        count = 9 if kind == "sources" else 2
        # Exercise a queued burst independently of disk/fsync speed. The quiet
        # window is covered separately; all these receipts precede dispatch.
        with monkeypatch.context() as patch:
            patch.setattr(value.queue, "_kick", lambda row: None)
            for number in range(1, count + 1):
                event = receipt(number)
                if kind == "characters":
                    event.text = "x" * 2500
                await value.queue.accept(event)
        row = next(iter(value.queue.rows.values()))
        first_at = row["turns"][0]["first_at"]
        for turn in row["turns"]:
            turn["first_at"] = turn["last_at"] = first_at
        value.queue._save(row)
        value.queue._kick(row)
        await settle(value.queue)
        turns = next(iter(value.queue.rows.values()))["turns"]
        assert len(turns) == 2
        assert len(turns[0]["source_ids"]) == (8 if kind == "sources" else 1)
        assert turns[0]["route"]["imessage_reply_target"] == uid(1)
        assert turns[1]["route"]["imessage_reply_target"] == uid(count)
        await value.queue.close()
    asyncio.run(run())


def test_slack_connection_revoked_while_working_blocks_reply(factory, tmp_path):
    async def run():
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate, mode="slack")
        await value.queue.accept(receipt(1, mode="slack"))
        await started(value)
        value.adapter._inkbox.slack.list_connections.return_value = NS(connections=[])
        gate.set()
        await settle(value.queue)
        value.adapter._inkbox.slack.send_message.assert_not_called()
        await value.queue.close()
    asyncio.run(run())
