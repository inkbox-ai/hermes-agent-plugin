"""Channel ingress reaches the durable queue only after source-aware admission."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from tests import test_companion as base_harness
from tests.test_companion import uid
from tests.test_native_turns import harness, settle, started

factory = base_harness.factory


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


def slack_event(number=1, *, thread=None, mentioned=True, stop=False):
    return {"id": uid(number), "event_type": "slack.session_stopped" if stop else
            "slack.mention_received" if mentioned else "slack.thread_reply_received" if thread else "slack.channel_message_received",
            "data": {"identity_id": uid(100), "connection_id": uid(40), "workspace_id": "T123",
                     "conversation_id": "C123", "actor_id": "UALICE", "message_ts": f"1234567890.{number:06}",
                     "thread_ts": thread, "sender_access": "direct", "message_kinds": ["channel"] + (["mention"] if mentioned else ["thread"] if thread else []),
                     "event": {"type": "agent_session_stopped" if stop else "message", "text": f"<@UBOT> question {number}" if mentioned else f"quiet {number}"}}}


async def slack_host(factory, tmp_path, *, mode="auto", gate=None):
    value = await harness(factory, tmp_path, mode="slack", gate=gate)
    value.adapter.config.extra["group_reply_mode"] = mode
    value.base.host._is_user_authorized = lambda source: source.user_id == "T123:UALICE" or bool(getattr(source, "role_authorized", False))
    value.adapter._inkbox.slack.list_connections.return_value.connections[0].bot_user_id = "UBOT"
    return value


@pytest.mark.parametrize("mode", ["auto", "mention"])
@pytest.mark.parametrize("thread", [None, "1234567890.000099"])
def test_unaddressed_unwatched_slack_has_no_model_or_orphan_context(factory, tmp_path, mode, thread):
    async def run():
        value = await slack_host(factory, tmp_path, mode=mode)
        for number in range(1, 11):
            response = await value.adapter._on_slack_event(slack_event(number, thread=thread, mentioned=False))
            assert response.status == 200
        assert not value.inputs and not value.queue.rows
        assert not value.adapter._conversation_state().root.exists()
        value.adapter._inkbox.slack.send_message.assert_not_called()
        value.adapter._inkbox.slack.add_reaction.assert_not_called()
        value.adapter._inkbox.slack.set_processing_status.assert_not_called()
        await value.adapter._on_slack_event(slack_event(11, thread=thread))
        await settle(value.queue)
        assert len(value.inputs) == 1
        assert "Earlier Slack context" not in value.inputs[0].text
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["auto", "mention"])
def test_followup_requires_existing_exact_thread_and_preserves_reply_route(factory, tmp_path, mode):
    async def run():
        value = await slack_host(factory, tmp_path, mode=mode)
        await value.adapter._on_slack_event(slack_event())
        await settle(value.queue)
        await value.adapter._on_slack_event(slack_event(2, thread="1234567890.000001", mentioned=False))
        await settle(value.queue)
        assert len(value.inputs) == (2 if mode == "auto" else 1)
        await value.adapter._on_slack_event(slack_event(3, thread="1234567890.000001"))
        await settle(value.queue)
        assert len(value.inputs) == (3 if mode == "auto" else 2)
        assert ("quiet 2" in value.inputs[-1].text) is (mode == "mention")
        assert len(value.queue.rows) == 1
        assert all(call.kwargs["thread_ts"] == "1234567890.000001" for call in value.adapter._inkbox.slack.send_message.call_args_list)
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("dropped", ["stop", "duplicate"])
def test_dropped_slack_control_or_duplicate_does_not_reserve_quiet_context(factory, tmp_path, dropped):
    async def run():
        value = await slack_host(factory, tmp_path, mode="mention")
        await value.adapter._on_slack_event(slack_event())
        await settle(value.queue)
        await value.adapter._on_slack_event(slack_event(2, thread="1234567890.000001", mentioned=False))
        await value.adapter._on_slack_event(slack_event(3, thread="1234567890.000001", stop=True) if dropped == "stop" else slack_event())
        assert len(value.inputs) == 1
        await value.adapter._on_slack_event(slack_event(4, thread="1234567890.000001"))
        await settle(value.queue)
        assert len(value.inputs) == 2
        assert value.inputs[-1].text.count("quiet 2") == 1
        assert not value.adapter._conversation_state().row(value.inputs[-1].source.chat_id)["quiet"]
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_stop_releases_context_reserved_by_cancelled_queued_slack_turn(factory, tmp_path, monkeypatch):
    async def run():
        gate = asyncio.Event()
        value = await slack_host(factory, tmp_path, mode="mention", gate=gate)
        await value.adapter._on_slack_event(slack_event())
        await started(value)
        await value.adapter._on_slack_event(slack_event(2, thread="1234567890.000001", mentioned=False))
        await value.adapter._on_slack_event(slack_event(3, thread="1234567890.000001"))
        row = next(iter(value.queue.rows.values()))
        assert row["turns"][1]["state"] == "pending"
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", AsyncMock(return_value=True))
        monkeypatch.setattr(value.queue, "_forward_control", AsyncMock())
        await value.adapter._on_slack_event(slack_event(4, thread="1234567890.000001", stop=True))
        gate.set()
        await settle(value.queue)
        assert row["turns"][1]["state"] == "cancelled"
        await value.adapter._on_slack_event(slack_event(5, thread="1234567890.000001"))
        await settle(value.queue)
        assert len(value.inputs) == 2
        assert value.inputs[-1].text.count("quiet 2") == 1
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_duplicate_active_slack_receipt_neither_releases_owned_context_nor_consumes_new_quiet(factory, tmp_path):
    async def run():
        value = await slack_host(factory, tmp_path, mode="mention")
        await value.adapter._on_slack_event(slack_event())
        await settle(value.queue)
        await value.adapter._on_slack_event(slack_event(2, thread="1234567890.000001", mentioned=False))
        gate = asyncio.Event()
        original_handle = value.adapter.handle_message
        async def delayed(event):
            await gate.wait()
            await original_handle(event)
        value.adapter.handle_message = delayed
        await value.adapter._on_slack_event(slack_event(3, thread="1234567890.000001"))
        await value.adapter._on_slack_event(slack_event(4, thread="1234567890.000001", mentioned=False))
        assert (await value.adapter._on_slack_event(slack_event(3, thread="1234567890.000001"))).status == 200
        row = next(iter(value.queue.rows.values()))
        quiet = value.adapter._conversation_state().row(row["chat_id"])["quiet"]
        assert [(item["id"], item.get("turn")) for item in quiet] == [(uid(2), uid(3)), (uid(4), None)]
        gate.set()
        await settle(value.queue)
        await value.adapter._on_slack_event(slack_event(5, thread="1234567890.000001"))
        await settle(value.queue)
        assert len(value.inputs) == 3
        assert "quiet 2" in value.inputs[1].text and "quiet 4" not in value.inputs[1].text
        assert "quiet 4" in value.inputs[2].text and "quiet 2" not in value.inputs[2].text
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def imessage_event(number=1, *, conversation=30, thread=None, group=False):
    return {"id": uid(number + 1000), "event_type": "imessage.received", "data": {"identity_id": uid(100), "message": {
        "id": uid(number), "direction": "inbound", "sender_number": "+15555550101", "conversation_id": uid(conversation),
        "content": f"@sample-agent question {number}", "created_at": "2026-10-06T10:00:00Z", "is_group": group,
        "participants": ["+15555550101", "+15555550102"] if group else [],
        "reply_to_message_id": uid(thread) if thread else None, "thread_id": uid(thread) if thread else None,
        "thread_root_message_id": uid(thread) if thread else None}}}


@pytest.mark.parametrize("group", [False, True])
def test_native_imessage_ingress_preserves_source_and_one_noninterrupting_conversation_queue(factory, tmp_path, group):
    async def run():
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate)
        adapter = value.adapter
        adapter._last_inbound_modality = {}
        adapter._last_inbound_imessage = {}
        adapter._resolve_contact_full = AsyncMock(return_value=None)
        adapter._contact_memories_enabled = False
        value.base.host._is_user_authorized = lambda source: source.user_id_alt == "+15555550101"
        first = imessage_event(group=group)
        assert (await adapter._on_imessage_received(first)).status == 202
        await started(value)
        assert (await adapter._on_imessage_received(imessage_event(2, thread=900, group=group))).status == 202
        assert (await adapter._on_imessage_received(first)).status == 200  # webhook receipt dedup
        await asyncio.sleep(.04)
        assert len(value.inputs) == 1
        gate.set()
        await settle(value.queue)
        assert len(value.inputs) == 2 and len(value.queue.rows) == 1
        routes = [event.metadata["inkbox_reply_route"] for event in value.inputs]
        assert [event.source.message_id for event in value.inputs] == [uid(1), uid(2)]
        assert [route["imessage_reply_target"] for route in routes] == [uid(1), uid(2)]
        assert routes[1]["imessage_sources"] == [{"id": uid(2), "reply_to_message_id": uid(900),
            "thread_id": uid(900), "thread_root_message_id": uid(900)}]
        assert all(event.source.thread_id == "imessage:" + uid(30) for event in value.inputs)
        assert [call.kwargs["reply_to_message_id"] for call in adapter._reply_identity.send_imessage.call_args_list] == [uid(1), uid(2)]
        await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("rejected", ["cancelled", "denied"])
def test_native_imessage_group_releases_unprocessed_quiet_context(factory, tmp_path, monkeypatch, rejected):
    async def run():
        gate = asyncio.Event()
        if rejected == "denied":
            gate.set()
        value = await harness(factory, tmp_path, gate=gate)
        adapter = value.adapter
        adapter.config.extra["group_reply_mode"] = "mention"
        adapter._last_inbound_modality = {}
        adapter._last_inbound_imessage = {}
        adapter._resolve_contact_full = AsyncMock(return_value=None)
        adapter._contact_memories_enabled = False
        value.base.host._is_user_authorized = lambda source: source.user_id_alt == "+15555550101"
        await adapter._on_imessage_received(imessage_event(group=True))
        if rejected == "denied":
            await settle(value.queue)
        else:
            await started(value)
        quiet = imessage_event(2, group=True)
        quiet["data"]["message"]["content"] = "Unaddressed group context"
        await adapter._on_imessage_received(quiet)
        following = imessage_event(3, group=True)
        if rejected == "denied":
            following["data"]["message"]["sender_number"] = "+15555550199"
        await adapter._on_imessage_received(following)
        row = next(iter(value.queue.rows.values()))
        if rejected == "cancelled":
            assert row["turns"][1]["state"] == "pending"
            stop = imessage_event(4, group=True)
            stop["data"]["message"]["content"] = "/stop"
            monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", AsyncMock(return_value=True))
            monkeypatch.setattr(value.queue, "_forward_control", AsyncMock())
            await adapter._on_imessage_received(stop)
            gate.set()
            await settle(value.queue)
            assert row["turns"][1]["state"] == "cancelled"
        assert len(value.inputs) == 1
        assert all("turn" not in item for item in adapter._conversation_state().row(row["chat_id"])["quiet"])
        await adapter._on_imessage_received(imessage_event(5, group=True))
        await settle(value.queue)
        assert len(value.inputs) == 2 and value.inputs[-1].text.count("Unaddressed group context") == 1
        assert not adapter._conversation_state().row(row["chat_id"])["quiet"]
        await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("channel", ["slack", "imessage"])
@pytest.mark.parametrize("boundary", ["before_publish", "after_publish", "scheduling"])
def test_native_admission_failure_retries_durably_without_losing_reserved_context(factory, tmp_path, monkeypatch, channel, boundary):
    async def run():
        if channel == "slack":
            value = await slack_host(factory, tmp_path, mode="mention")
            def incoming(number, quiet=False):
                return slack_event(number, thread="1234567890.000001", mentioned=not quiet)
            deliver = value.adapter._on_slack_event
        else:
            value = await harness(factory, tmp_path)
            value.adapter.config.extra["group_reply_mode"] = "mention"
            value.adapter._last_inbound_modality = {}
            value.adapter._last_inbound_imessage = {}
            value.adapter._resolve_contact_full = AsyncMock(return_value=None)
            value.adapter._contact_memories_enabled = False
            value.base.host._is_user_authorized = lambda source: source.user_id_alt == "+15555550101"
            def incoming(number, quiet=False):
                result = imessage_event(number, group=True)
                if quiet:
                    result["data"]["message"]["content"] = "quiet 2"
                return result
            deliver = value.adapter._on_imessage_received
        await deliver(incoming(1))
        await settle(value.queue)
        await deliver(incoming(2, quiet=True))
        save, kick = value.queue._save, value.queue._kick
        failed = []
        def fault(row):
            return any(turn["id"] == uid(3) and turn["state"] == "pending" for turn in row["turns"])
        def save_with_failure(row):
            if boundary != "scheduling" and not failed and fault(row):
                failed.append(True)
                if boundary == "after_publish":
                    save(row)
                raise OSError("synthetic checkpoint boundary failure")
            save(row)
        def kick_with_failure(row):
            if boundary == "scheduling" and not failed and fault(row):
                failed.append(True)
                raise OSError("synthetic scheduling failure after durable admission")
            kick(row)
        monkeypatch.setattr(value.queue, "_save", save_with_failure)
        monkeypatch.setattr(value.queue, "_kick", kick_with_failure)
        with pytest.raises(OSError):
            await deliver(incoming(3))
        assert failed and len(value.inputs) == 1
        row = next(iter(value.queue.rows.values()))
        assert value.queue.contains_source(uid(3)) is (boundary != "before_publish")
        reservation = value.adapter._conversation_state().row(row["chat_id"])["quiet"][0]
        assert reservation.get("turn") == (None if boundary == "before_publish" else uid(3))
        # Retry must finish the real primary checkpoint before it can ACK a
        # retained pending source or let the native worker execute it.
        writes = []
        def repaired_save(row):
            save(row)
            if fault(row):
                writes.append(True)
        monkeypatch.setattr(value.queue, "_save", repaired_save)
        response = await deliver(incoming(3))
        assert response.status in {200, 202} and writes
        await settle(value.queue)
        assert len(value.inputs) == 2 and value.inputs[1].text.count("quiet 2") == 1
        assert not value.adapter._conversation_state().row(row["chat_id"])["quiet"]
        await deliver(incoming(4))
        await settle(value.queue)
        assert len(value.inputs) == 3 and "quiet 2" not in value.inputs[2].text
        sends = value.adapter._inkbox.slack.send_message if channel == "slack" else value.adapter._reply_identity.send_imessage
        assert sends.call_count == 3
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("channel", ["slack", "imessage"])
@pytest.mark.parametrize("kind,boundary,restart", [
    (kind, boundary, restart) for kind in ("idle_command", "active_command", "prompt_answer")
    for boundary, restart in (("before_publish", False), ("after_publish", False),
        ("after_publish", True), ("native_effect", False), ("native_effect", True))
] + [("withdrawn_prompt", "after_publish", False), ("idle_command", "completion_checkpoint", False)])
def test_control_checkpoint_retry_is_exact_and_never_replays_ambiguous_native_effect(factory, tmp_path, monkeypatch, channel, kind, boundary, restart):
    from inkbox_plugin.native_turns import NativeTurns
    async def run():
        gate = asyncio.Event() if kind != "idle_command" else None
        control_text = "yes" if kind in {"prompt_answer", "withdrawn_prompt"} else "/clear"
        effect_text = "allow_once" if kind in {"prompt_answer", "withdrawn_prompt"} else "/clear"
        if channel == "slack":
            value = await slack_host(factory, tmp_path, mode="mention", gate=gate)
            def incoming(number, text=None, quiet=False):
                result = slack_event(number, thread="1234567890.000001", mentioned=not quiet)
                if text is not None:
                    result["data"]["event"]["text"] = "<@UBOT> " + text
                return result
            deliver = value.adapter._on_slack_event
        else:
            value = await harness(factory, tmp_path, gate=gate)
            value.adapter.config.extra["group_reply_mode"] = "mention"
            value.adapter._last_inbound_modality = {}
            value.adapter._last_inbound_imessage = {}
            value.adapter._resolve_contact_full = AsyncMock(return_value=None)
            value.adapter._contact_memories_enabled = False
            value.base.host._is_user_authorized = lambda source: source.user_id_alt == "+15555550101"
            def incoming(number, text=None, quiet=False):
                result = imessage_event(number, group=True)
                if text is not None or quiet:
                    result["data"]["message"]["content"] = text if text is not None else "quiet 2"
                return result
            deliver = value.adapter._on_imessage_received
        await deliver(incoming(1))
        if gate is None:
            await settle(value.queue)
        else:
            await started(value)
        if kind in {"prompt_answer", "withdrawn_prompt"}:
            value.adapter._pending_conversation_control = lambda _: True
            value.adapter._conversation_prompt_reply = lambda _, text: "allow_once" if text == "yes" else None
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", AsyncMock(return_value=True))
        await deliver(incoming(2, quiet=True))
        save = value.queue._save
        failures, effects = [], []
        def save_with_failure(row):
            if (boundary != "native_effect" and not failures and row.get("controls", {}).get(uid(3))
                    == ("consumed" if boundary == "completion_checkpoint" else "submitting")):
                failures.append(True)
                if boundary == "after_publish":
                    save(row)
                raise OSError("synthetic first control checkpoint failure")
            save(row)
        async def control(event, text, active):
            effects.append(text)
            if boundary == "native_effect":
                # The original control is still inside its native boundary.
                # A concurrent identical receipt is ACKed, never forwarded again.
                duplicate = await deliver(incoming(3, control_text))
                assert duplicate.status in {200, 202} and effects == [effect_text]
                raise OSError("synthetic ambiguous native control completion")
        monkeypatch.setattr(value.queue, "_save", save_with_failure)
        monkeypatch.setattr(value.queue, "_forward_control", control)
        with pytest.raises(OSError):
            await deliver(incoming(3, control_text))
        assert effects == ([effect_text] if boundary in {"native_effect", "completion_checkpoint"} else [])
        assert bool(value.queue.unconfirmed_controls) is (boundary == "after_publish")
        if boundary == "after_publish" and not restart:
            with pytest.raises(RuntimeError, match="receipt changed"):
                await deliver(incoming(3, "/stop"))
            assert not effects
            if gate is not None:
                active = next(iter(value.queue.active.values()))
                original_id = active["id"]
                active["id"] = uid(90)
                with pytest.raises(RuntimeError, match="receipt changed"):
                    await deliver(incoming(3, control_text))
                active["id"] = original_id
                assert not effects
        if boundary == "completion_checkpoint":
            import json
            row = next(iter(value.queue.rows.values()))
            path = value.queue.root / (row["key"] + ".json")
            saved = json.loads(path.read_text())
            assert saved["controls"][uid(3)] == "submitting"
            assert saved["turns"][-1]["state"] == "control_submitting"
        monkeypatch.setattr(value.queue, "_save", save)
        withdrawn = kind == "withdrawn_prompt" and boundary == "after_publish" and not restart
        if withdrawn:
            value.adapter._pending_conversation_control = lambda _: False
            if channel == "imessage":
                value.adapter._imessage_threaded_replies = False
                held = await deliver(incoming(3, control_text))
                assert held.status == 503 and uid(3) in value.queue.unconfirmed_controls
                value.adapter._imessage_threaded_replies = True
        if restart:
            await value.queue.close()
            value.adapter._native_turns = value.queue = NativeTurns(value.adapter, tmp_path / "native")
            del value.adapter._conversation_journal
            monkeypatch.setattr(value.queue, "_forward_control", control)
            await value.queue.start()
            row = next(iter(value.queue.rows.values()))
            assert row["controls"][uid(3)] == "uncertain" and not value.queue.unconfirmed_controls
        for _ in range(2):
            response = await deliver(incoming(3, control_text))
            assert response.status in {200, 202}
        assert effects == ([] if (restart and boundary == "after_publish") or withdrawn else [effect_text])
        assert len(value.inputs) == 1  # Controls never become model turns.
        row = next(iter(value.queue.rows.values()))
        quiet = value.adapter._conversation_state().row(row["chat_id"])["quiet"]
        assert all("turn" not in item for item in quiet)
        if withdrawn:
            assert row["controls"][uid(3)] == "not_owner" and not value.queue.unconfirmed_controls
            assert all(item["id"] != uid(3) for item in quiet)
            await value.queue.close()
            value.adapter._native_turns = value.queue = NativeTurns(value.adapter, tmp_path / "native")
            del value.adapter._conversation_journal
            await value.queue.start()
            assert (await deliver(incoming(3, control_text))).status == 200
            quiet = value.adapter._conversation_state().row(row["chat_id"])["quiet"]
            assert all(item["id"] != uid(3) for item in quiet)
        if boundary == "completion_checkpoint":
            saved = json.loads(path.read_text())
            assert saved["controls"][uid(3)] == "consumed" and saved["turns"][-1]["state"] == "done"
            await value.queue.close()
            value.adapter._native_turns = value.queue = NativeTurns(value.adapter, tmp_path / "native")
            await value.queue.start()
            row = next(iter(value.queue.rows.values()))
            assert not row.get("blocked") and row["controls"][uid(3)] == "consumed"
            assert (await deliver(incoming(3, control_text))).status == 200
            assert effects == [effect_text] and len(value.inputs) == 1
        if gate is not None:
            gate.set()
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("retained_empty", [False, True])
def test_empty_slack_row_never_establishes_watched_thread(factory, tmp_path, monkeypatch, retained_empty):
    async def run():
        value = await slack_host(factory, tmp_path)
        save = value.queue._save
        original = []
        def fail(row):
            original.append((row["key"], row["chat_id"]))
            raise OSError("synthetic unpublished first receipt")
        monkeypatch.setattr(value.queue, "_save", fail)
        with pytest.raises(OSError):
            await value.adapter._on_slack_event(slack_event())
        assert not value.queue.rows and not value.inputs
        monkeypatch.setattr(value.queue, "_save", save)
        if retained_empty:
            from inkbox_plugin.native_turns import NativeTurns
            key, chat_id = original[0]
            row = {"version": 1, "key": key, "state": "ordinary", "turns": [], "chat_id": chat_id}
            save(row)
            await value.queue.close()
            value.adapter._native_turns = value.queue = NativeTurns(value.adapter, tmp_path / "native")
            await value.queue.start()
        response = await value.adapter._on_slack_event(slack_event(2, thread="1234567890.000001", mentioned=False))
        assert response.status == 200 and not value.inputs
        value.adapter._inkbox.slack.send_message.assert_not_called()
        await value.adapter._on_slack_event(slack_event(3, thread="1234567890.000001"))
        await settle(value.queue)
        assert len(value.inputs) == 1
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_proven_pre_admission_retry_keeps_its_embedded_quiet_context(factory, tmp_path, monkeypatch):
    async def run():
        value = await slack_host(factory, tmp_path, mode="mention")
        await value.adapter._on_slack_event(slack_event())
        await settle(value.queue)
        await value.adapter._on_slack_event(slack_event(2, thread="1234567890.000001", mentioned=False))
        handle, kick = value.adapter.handle_message, value.queue._kick
        failed, retry_ready = [], asyncio.Event()
        paused = True
        async def fail_before_admission(event):
            if event.message_id == uid(3) and not failed:
                failed.append(True)
                event._gateway_accepted = False
                raise RuntimeError("synthetic proven pre-admission failure")
            await handle(event)
        def pause_retry(row):
            if failed and paused:
                retry_ready.set()
                return
            kick(row)
        monkeypatch.setattr(value.adapter, "handle_message", fail_before_admission)
        monkeypatch.setattr(value.queue, "_kick", pause_retry)
        await value.adapter._on_slack_event(slack_event(3, thread="1234567890.000001"))
        await asyncio.wait_for(retry_ready.wait(), 2)
        row = next(iter(value.queue.rows.values()))
        pending = row["turns"][1]
        assert pending["state"] == "pending" and pending["event"]["text"].count("quiet 2") == 1
        assert value.adapter._conversation_state().row(row["chat_id"])["quiet"][0]["turn"] == uid(3)
        await value.adapter._on_slack_event(slack_event(4, thread="1234567890.000001"))
        assert "quiet 2" not in row["turns"][2]["event"]["text"]
        paused = False
        kick(row)
        await settle(value.queue)
        assert len(value.inputs) == 3
        assert sum(event.text.count("quiet 2") for event in value.inputs) == 1
        assert not value.adapter._conversation_state().row(row["chat_id"])["quiet"]
        assert value.adapter._inkbox.slack.send_message.call_count == 3
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["before_publish", "after_publish", "native_effect"])
@pytest.mark.parametrize("restart", [False, True])
def test_idle_stop_checkpoints_cancellation_before_native_effect(factory, tmp_path, monkeypatch, boundary, restart):
    from inkbox_plugin.native_turns import NativeTurns
    import json
    async def run():
        value = await slack_host(factory, tmp_path, mode="mention")
        await value.adapter._on_slack_event(slack_event())
        await settle(value.queue)
        await value.adapter._on_slack_event(slack_event(2, thread="1234567890.000001", mentioned=False))
        monkeypatch.setattr(value.queue, "_kick", lambda row: None)
        await value.adapter._on_slack_event(slack_event(3, thread="1234567890.000001"))
        row = next(iter(value.queue.rows.values()))
        path = value.queue.root / (row["key"] + ".json")
        save, failed, effects = value.queue._save, [], []
        def cancelling(row):
            return row.get("controls", {}).get(uid(4)) == "submitting" and row["turns"][1]["state"] == "cancelled"
        def save_with_failure(row):
            if boundary != "native_effect" and not failed and cancelling(row):
                failed.append(True)
                if boundary == "after_publish":
                    save(row)
                raise OSError("synthetic idle cancellation checkpoint failure")
            save(row)
        async def control(event, text, turn):
            assert json.loads(path.read_text())["turns"][1]["state"] == "cancelled"
            assert all("turn" not in item for item in value.adapter._conversation_state().row(row["chat_id"])["quiet"])
            effects.append(text)
            if boundary == "native_effect":
                raise OSError("synthetic ambiguous native effect")
        monkeypatch.setattr(value.queue, "_save", save_with_failure)
        monkeypatch.setattr(value.queue, "_forward_control", control)
        incoming = slack_event(4, thread="1234567890.000001")
        incoming["data"]["event"]["text"] = "<@UBOT> /stop"
        with pytest.raises(OSError):
            await value.adapter._on_slack_event(incoming)
        saved = json.loads(path.read_text())
        assert saved["turns"][1]["state"] == ("pending" if boundary == "before_publish" else "cancelled")
        assert effects == (["/stop"] if boundary == "native_effect" else [])
        assert bool(value.queue.unconfirmed_controls) is (boundary != "native_effect")
        quiet_before_restart = value.adapter._conversation_state().row(row["chat_id"])["quiet"]
        assert quiet_before_restart[0].get("turn") == (None if boundary == "native_effect" else uid(3))
        monkeypatch.setattr(value.queue, "_save", save)
        if restart:
            await value.queue.close()
            value.adapter._native_turns = value.queue = NativeTurns(value.adapter, tmp_path / "native")
            del value.adapter._conversation_journal
            monkeypatch.setattr(value.queue, "_forward_control", control)
            await value.queue.start()
            row = next(iter(value.queue.rows.values()))
            assert row["controls"][uid(4)] == "uncertain" and row["blocked"]
            assert not value.queue.unconfirmed_controls
        for _ in range(2):
            assert (await value.adapter._on_slack_event(incoming)).status in {200, 202}
        assert effects == ([] if restart and boundary != "native_effect" else ["/stop"])
        assert len(value.inputs) == 1 and value.adapter._inkbox.slack.send_message.call_count == 1
        quiet = value.adapter._conversation_state().row(row["chat_id"])["quiet"]
        # ConversationState deliberately clears process-local reservations on
        # reload; the uncertain control still blocks all pending native work.
        assert quiet[0].get("turn") is None
        await value.queue.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())
