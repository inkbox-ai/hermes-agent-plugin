"""WhatsApp grants retain the canonical native Companion lifecycle."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_companion import factory as companion_factory, event, snapshot, uid, idle

factory = companion_factory
from inkbox_plugin.companion import metadata


def whatsapp_event(phase="initialization"):
    value = event("imessage", phase=phase)
    value["event_type"] = "whatsapp.received"
    value["companion"]["channel"] = "whatsapp"
    value["data"]["message"].update(
        agent_identity_id=uid(100), is_group=True, conversation_kind="group",
        participants=["+15555550101", "+15555550102"], local_number="+15555550100",
        unresolved_participant_count=0, media=[], content="A useful current question",
        agent_mentioned=True, mentions=[],
    )
    return value


def setup(instance):
    item = whatsapp_event()
    instance.identity.id = uid(100)
    instance.identity.get_whatsapp_conversation = Mock(return_value={
        **item["data"]["message"], "id": uid(30),
    })
    instance.identity.send_whatsapp = Mock(return_value=SimpleNamespace(id=uid(800)))
    instance.resource.load_initialization.side_effect = lambda *a, **kw: snapshot(item)
    instance.resource.activation_messages.side_effect = lambda *a, **kw: snapshot(item)
    return item


def test_group_initialization_uses_native_host_and_current_grant(factory):
    async def run():
        instance = factory("imessage")
        item = setup(instance)
        await instance.receiver.accept(item)
        await idle(instance)
        assert len(instance.inputs) == 1
        assert instance.inputs[0].source.thread_id.startswith("whatsapp:")
        assert "[SILENT]" in instance.inputs[0].text
        instance.resource.activation_messages.assert_called_with("sample-agent", uid(20), limit=1)
        assert not instance.identity.send_imessage.called
        await instance.receiver.close()


    asyncio.run(run())

@pytest.mark.parametrize("change", [
    {"is_group": False}, {"sender_number": None}, {"agent_identity_id": uid(999)},
    {"unresolved_participant_count": 1}, {"direction": "outbound"},
])
def test_invalid_source_never_enters_companion_host(factory, change):
    async def run():
        instance = factory("imessage")
        item = setup(instance)
        item["data"]["message"].update(change)
        with pytest.raises(ValueError):
            await instance.receiver.accept(item)
        assert not instance.inputs
        assert not instance.resource.load_initialization.called
        await instance.receiver.close()


    asyncio.run(run())

def test_replaced_activation_cannot_submit_native_turn(factory):
    async def run():
        instance = factory("imessage")
        item = setup(instance)
        instance.resource.activation_messages.side_effect = lambda *a, **kw: {
            **snapshot(item), "activation_id": uid(999),
        }
        await instance.receiver.accept(item)
        await idle(instance)
        assert not instance.inputs
        assert not instance.identity.send_whatsapp.called
        await instance.receiver.close()


    asyncio.run(run())

def test_whatsapp_reaction_is_not_new_companion_message():
    value = whatsapp_event()
    value["event_type"] = "whatsapp.reaction_received"
    with pytest.raises(ValueError, match="channel"):
        metadata(value)


def test_live_grant_checks_sponsor_and_actual_destination(factory):
    async def run():
        from inkbox_plugin.imessage_state import active_context, clear_context
        instance = factory("imessage")
        item = setup(instance)
        await instance.receiver.accept(item)
        await idle(instance)
        row = next(iter(instance.receiver.rows.values()))
        turn = row["turns"][0]
        instance.receiver._bind_whatsapp_context(row, turn)
        context = active_context(row["host_session_id"])
        context["authorize_whatsapp"]()
        instance.host.allow_sponsor = False
        with pytest.raises(PermissionError, match="not permitted"):
            context["authorize_whatsapp"]()
        instance.host.allow_sponsor = True
        instance.identity.get_whatsapp_conversation.side_effect = lambda target: {
            **item["data"]["message"], "id": target,
            "participants": item["data"]["message"]["participants"] if target == uid(30) else ["+15555550103"],
        }
        with pytest.raises(PermissionError, match="participant"):
            context["authorize_whatsapp_destination"](instance.identity, uid(99))
        clear_context(row["host_session_id"], turn["id"])
        await instance.receiver.close()
    asyncio.run(run())


def test_plain_reply_and_uncertain_explicit_attempt_no_replacement(factory):
    async def run():
        instance = factory("imessage")
        item = setup(instance)
        await instance.receiver.accept(item)
        await idle(instance)
        row = next(iter(instance.receiver.rows.values()))
        turn = row["turns"][0]
        result = await instance.receiver.send("companion:" + row["key"], "A plain answer", turn["id"])
        assert result.success
        instance.identity.send_whatsapp.assert_called_once_with(conversation_id=uid(30), text="A plain answer",
            idempotency_key=turn["delivery"]["idempotency_key"])
        turn["whatsapp_send_attempts"] = {"retained": {"key": "retained", "state": "uncertain"}}
        result = await instance.receiver.send("companion:" + row["key"], "Another answer", turn["id"])
        assert not result.success
        assert instance.identity.send_whatsapp.call_count == 1
        await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure_kind", ["503", "transport"])
def test_transient_send_preflight_preserves_answer_without_post(factory, failure_kind):
    async def run():
        instance = factory("imessage")
        item = setup(instance)
        await instance.receiver.accept(item)
        await idle(instance)
        row = next(iter(instance.receiver.rows.values()))
        turn = row["turns"][0]
        if failure_kind == "transport":
            from httpx import ReadTimeout
            error = ReadTimeout("temporarily unavailable")
        else:
            error = RuntimeError("temporarily unavailable")
            error.status_code = 503
        instance.resource.activation_messages.side_effect = error
        result = await instance.receiver.send("companion:" + row["key"], "Retained answer", turn["id"])
        assert not result.success
        assert turn["delivery"]["state"] == "pending"
        assert turn["preflight_failure"]["retryable"] is True
        instance.identity.send_whatsapp.assert_not_called()
        saved_key = turn["delivery"]["idempotency_key"]
        instance.resource.activation_messages.side_effect = lambda *a, **kw: snapshot(item)
        result = await instance.receiver.send("companion:" + row["key"], "Retained answer", turn["id"])
        assert result.success
        instance.identity.send_whatsapp.assert_called_once()
        assert instance.identity.send_whatsapp.call_args.kwargs["idempotency_key"] == saved_key
        assert len(instance.inputs) == 1
        await instance.receiver.close()
    asyncio.run(run())


def test_duplicate_source_retains_first_projection_rejects_changed_content(factory):
    async def run():
        import copy
        instance = factory("imessage")
        item = setup(instance)
        await instance.receiver.accept(item)
        await idle(instance)
        refreshed = copy.deepcopy(item)
        refreshed["data"]["message"].update(group_name="Renamed", agent_mentioned=False, mentions=[])
        await instance.receiver.accept(refreshed)
        await idle(instance)
        row = next(iter(instance.receiver.rows.values()))
        assert row["trigger_envelope"]["data"]["message"]["agent_mentioned"] is True
        assert len(instance.inputs) == 1
        refreshed["data"]["message"]["content"] = "Conflicting source"
        with pytest.raises(ValueError, match="conflicting source"):
            await instance.receiver.accept(refreshed)
        assert len(instance.inputs) == 1
        await instance.receiver.close()
    asyncio.run(run())


def test_ambiguous_conflict_retains_original_receipt_until_proof_recovers(factory):
    async def run():
        import copy
        instance = factory("imessage")
        item = setup(instance)
        receiver = instance.receiver
        receiver.retry_delay = 0
        error = RuntimeError("membership proof temporarily unavailable")
        error.status_code = 409
        observed = []
        def proof(*args, **kwargs):
            row = next(iter(receiver.rows.values()))
            observed.append(copy.deepcopy(row))
            if len(observed) == 1:
                raise error
            return snapshot(item)
        instance.resource.activation_messages.side_effect = proof
        await receiver.accept(item)
        await idle(instance)
        assert len(instance.inputs) == 1
        assert len(observed) >= 2
        assert observed[1]["turns"][0]["envelope"] == item
        assert observed[1]["state"] != "revoked"
        assert next(iter(receiver.rows.values()))["state"] == "initialized"
        await receiver.close()
    asyncio.run(run())


def test_new_activation_progresses_without_replaying_old_ambiguous_grant(factory):
    async def run():
        import copy
        instance = factory("imessage")
        first = setup(instance)
        replacement = copy.deepcopy(first)
        replacement["companion"].update(activation_id=uid(21), sequence=4)
        replacement["data"]["message"]["id"] = uid(4)
        error = RuntimeError("old activation proof unavailable")
        error.status_code = 409
        def initialization(handle, activation, **kwargs):
            if activation == uid(20):
                raise error
            value = snapshot(replacement)
            trigger = next(entry for entry in value["entries"] if entry["is_trigger"])
            trigger["id"] = uid(4)
            return value
        instance.resource.load_initialization.side_effect = initialization
        instance.resource.activation_messages.side_effect = lambda *a, **kw: snapshot(replacement)
        await instance.receiver.accept(first)
        await asyncio.sleep(0.02)
        await instance.receiver.accept(replacement)
        for _ in range(100):
            if instance.inputs:
                break
            await asyncio.sleep(0.01)
        assert len(instance.inputs) == 1
        assert instance.inputs[0].metadata["companion"]["activation_id"] == uid(21)
        old = next(row for row in instance.receiver.rows.values() if row["meta"]["activation_id"] == uid(20))
        assert old["state"] != "revoked"
        assert old["trigger_envelope"] == first
        await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize("status,code", [(429, "whatsapp_daily_limit_exceeded"), (409, "whatsapp_group_reply_required")])
def test_definitive_send_rejection_settles_only_original_answer(factory, status, code):
    async def run():
        instance = factory("imessage")
        item = setup(instance)
        await instance.receiver.accept(item)
        await idle(instance)
        row = next(iter(instance.receiver.rows.values()))
        turn = row["turns"][0]
        error = RuntimeError("rejected")
        error.status_code, error.detail = status, {"error": code}
        instance.identity.send_whatsapp.side_effect = error
        first = await instance.receiver.send("companion:" + row["key"], "Rejected answer", turn["id"])
        assert first.success and first.raw_response["inkbox_suppressed"] is True
        assert first.message_id is None
        assert turn["delivery"]["state"] == "rejected"
        repeated = await instance.receiver.send("companion:" + row["key"], "Rejected answer", turn["id"])
        assert repeated.success and repeated.raw_response["error"] == code
        assert instance.identity.send_whatsapp.call_count == 1
        assert row["state"] == "initialized"
        await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize("failed_read", [1, 2, 3, 4, 5])
def test_companion_media_checks_off_loop_and_reuses_upload_after_late_read_failure(factory, tmp_path, failed_read):
    async def run():
        import threading
        from inkbox_plugin.whatsapp import send_media
        instance = factory("imessage")
        item = setup(instance)
        await instance.receiver.accept(item)
        await idle(instance)
        receiver = instance.receiver
        row = next(iter(receiver.rows.values()))
        turn = row["turns"][0]
        receiver._bind_whatsapp_context(row, turn)
        chat = "companion:" + row["key"]
        receiver.active[chat] = turn
        picture = tmp_path / "picture.png"
        picture.write_bytes(b"png")
        instance.adapter.validate_media_delivery_path = lambda path: path
        instance.identity.upload_whatsapp_media = Mock(return_value=SimpleNamespace(id=uid(700)))
        loop_thread = threading.get_ident()
        threads = []
        def proof(*args, **kwargs):
            threads.append(threading.get_ident())
            if len(threads) == failed_read:
                raise TimeoutError("membership read unavailable")
            return snapshot(item)
        instance.resource.activation_messages.side_effect = proof
        first = await send_media(instance.adapter, chat, picture, "Picture")
        assert not first.success and first.raw_response["inkbox_preflight_failure"]
        assert threads and loop_thread not in threads
        instance.identity.send_whatsapp.assert_not_called()
        delivery = next(iter(turn.get("media_deliveries", {}).values()), {})
        assert delivery.get("state") == (None if failed_read == 1 else "uploaded" if failed_read >= 4 else "uploading")
        assert instance.identity.upload_whatsapp_media.call_count == (1 if failed_read >= 4 else 0)
        assert turn["state"] == "completed"
        key = delivery.get("idempotency_key")
        second = await send_media(instance.adapter, chat, picture, "Picture")
        assert second.success
        assert loop_thread not in threads
        instance.identity.upload_whatsapp_media.assert_called_once()
        instance.identity.send_whatsapp.assert_called_once()
        if key:
            assert instance.identity.send_whatsapp.call_args.kwargs["idempotency_key"] == key
        await receiver.close()
    asyncio.run(run())


def test_rejected_explicit_quote_suppresses_only_captured_final_not_intermediate(factory):
    async def run():
        instance = factory("imessage")
        item = setup(instance)
        await instance.receiver.accept(item)
        await idle(instance)
        row = next(iter(instance.receiver.rows.values()))
        turn = row["turns"][0]
        turn.update(explicit_delivery_state="rejected", result_ready=True, result="Final answer")
        intermediate = await instance.receiver.send("companion:" + row["key"], "Other body", turn["id"])
        assert not intermediate.success and intermediate.raw_response["inkbox_no_retry"]
        final = await instance.receiver.send("companion:" + row["key"], "Final answer", turn["id"])
        assert final.success and final.raw_response["inkbox_suppressed"]
        assert final.message_id is None
        instance.identity.send_whatsapp.assert_not_called()
        await instance.receiver.close()
    asyncio.run(run())
