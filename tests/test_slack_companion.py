"""Channel-wide Companion history with exact per-source Slack reply ownership."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests import test_companion as base_harness
from tests.test_companion import idle, wait_inputs, uid

factory = base_harness.factory
from inkbox_plugin import slack_companion
from inkbox_plugin.slack_activity import SlackActivity


def incoming(number=3, *, phase="initialization", thread=None, access="direct", mentioned=True, actor="UALICE", text=None):
    return {"id": f"slack-event-{number}", "event_type": "slack.mention_received" if mentioned else "slack.channel_message_received",
            "companion": {"scope_id": uid(10), "activation_id": uid(20), "conversation_id": uid(30),
                          "channel": "slack", "phase": phase, "sequence": number},
            "data": {"identity_id": uid(100), "connection_id": uid(40), "workspace_id": "TINSTALL",
                     "conversation_id": "CEXAMPLE", "message_ts": f"1234567890.{number:06}", "thread_ts": thread,
                     "message_kinds": ["channel"] + (["mention"] if mentioned else []), "sender_access": access,
                     "actor_id": actor, "actor_profile": {"id": actor, "team_id": "THOME"},
                     "event": {"type": "message", "text": text or f"<@UBOT> question {number}"}}}


def normalized(_client, _identity, value):
    value = deepcopy(value)
    value["_hermes_slack_source"] = {"id": uid(value["companion"]["sequence"]),
        "author": "THOME:" + value["data"]["actor_id"], "thread_ts": value["data"]["thread_ts"], "bot_user_id": "UBOT"}
    return value


@pytest.fixture
def slack_host(factory, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(slack_companion, "prepare_envelope", normalized)
    monkeypatch.setattr(slack_companion, "require_sdk_support", lambda: None)
    value = factory()
    value.adapter._slack_enabled = True
    value.adapter.config.extra.update(group_reply_mode="mention", companion_response_mode="safe")
    value.host._is_user_authorized = lambda source: source.user_id == "THOME:UALICE" or bool(getattr(source, "role_authorized", False))
    source = incoming()
    meta = source["companion"]
    entries = [{"id": uid(1), "author": "THOME:UBOB", "text": "Background", "historical": True, "is_trigger": False},
               {"id": uid(3), "author": "THOME:UALICE", "text": "Trigger", "historical": False, "is_trigger": True}]
    value.snapshot = {key: meta[key] for key in ("scope_id", "activation_id", "conversation_id", "channel")} | {
        "entries": entries, "text": json.dumps(entries), "notices": [],
        "reply_context": {"channel": "slack", "conversation_id": uid(30), "connection_id": uid(40), "slack_conversation_id": "CEXAMPLE", "thread_ts": None},
    }
    value.resource.load_initialization.side_effect = lambda *_args, **_kwargs: deepcopy(value.snapshot)
    value.resource.activation_messages.side_effect = lambda *_args, **_kwargs: deepcopy(value.snapshot)
    value.adapter._inkbox.slack = Mock()
    value.adapter._inkbox.slack.send_message.return_value = NS(id=uid(70), status="sent")
    for name in ("set_processing_status", "add_reaction", "remove_reaction"):
        getattr(value.adapter._inkbox.slack, name).return_value = NS(status="succeeded")
    value.adapter._slack_activity = SlackActivity(value.adapter._inkbox.slack, tmp_path / "activity.json")
    return value


def test_channel_scope_and_exact_top_level_subthread_routes(slack_host):
    async def run():
        value = slack_host
        await value.receiver.accept(incoming())
        await idle(value)
        first = value.inputs[0]
        result = await value.adapter.send(first.source.chat_id, "Timeline answer", reply_to=first.message_id)
        assert result.success
        await value.receiver.accept(incoming(4, phase="live", thread="1234567800.000001"))
        await idle(value)
        second = value.inputs[1]
        assert first.source.chat_id == second.source.chat_id
        assert first.source.thread_id == second.source.thread_id
        result = await value.adapter.send(second.source.chat_id, "Thread answer", reply_to=second.message_id)
        assert result.success
        calls = value.adapter._inkbox.slack.send_message.call_args_list
        assert [call.kwargs["thread_ts"] for call in calls] == [None, "1234567800.000001"]
        assert value.resource.load_initialization.call_count == 1
        assert value.resource.activation_messages.call_count == 4  # generation and send rechecks
        await value.receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("access,mentioned,mode,count", [
    ("sponsored", True, "safe", 0), ("sponsored", True, "relaxed", 1),
    ("direct", False, "safe", 0), ("direct", True, "safe", 1),
])
def test_safe_and_mention_are_independent_and_quiet_has_no_activity(slack_host, access, mentioned, mode, count):
    async def run():
        value = slack_host
        value.adapter.config.extra["companion_response_mode"] = mode
        await value.receiver.accept(incoming(access=access, mentioned=mentioned))
        await idle(value)
        await value.adapter._slack_activity.flush()
        assert len(value.inputs) == count
        if not count:
            value.adapter._inkbox.slack.add_reaction.assert_not_called()
            value.adapter._inkbox.slack.set_processing_status.assert_not_called()
        await value.receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_live_first_does_not_wake_old_trigger_and_keeps_current_route(slack_host):
    async def run():
        value = slack_host
        await value.receiver.accept(incoming(4, phase="live", thread="1234567800.000002"))
        await idle(value)
        assert len(value.inputs) == 1
        assert "question 4" in value.inputs[0].text
        assert value.inputs[0].metadata["inkbox_reply_route"]["thread_ts"] == "1234567800.000002"
        row = next(iter(value.receiver.rows.values()))
        assert row["turns"][0]["state"] == "context_only"
        await value.receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_activation_revoked_before_send_blocks_saved_route(slack_host):
    async def run():
        value = slack_host
        await value.receiver.accept(incoming())
        await idle(value)
        value.resource.activation_messages.side_effect = PermissionError("revoked")
        first = value.inputs[0]
        result = await value.adapter.send(first.source.chat_id, "Must not send", reply_to=first.message_id)
        assert not result.success
        value.adapter._inkbox.slack.send_message.assert_not_called()
        await value.receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


@pytest.mark.parametrize("field,bad", [("thread_ts", "1234567800.000002"), ("actor_id", "UBOB"), ("workspace_id", "TOTHER")])
def test_native_stop_requires_exact_current_owner_and_thread(slack_host, field, bad):
    async def run():
        value = slack_host
        value.gate.clear()
        await value.receiver.accept(incoming(thread="1234567800.000001"))
        await wait_inputs(value, 1)
        route = next(iter(value.receiver.rows.values()))["turns"][0]
        meta = value.receiver._slack_route(route)
        assert await value.receiver.slack_stop({**meta, field: bad}) is False
        assert len(value.inputs) == 1
        value.gate.set()
        await idle(value)
        await value.receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())


def test_consumed_native_stop_cannot_cancel_later_same_thread_turn(slack_host, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        value = slack_host
        value.gate.clear()
        await value.receiver.accept(incoming(thread="1234567800.000001"))
        await wait_inputs(value, 1)
        row = next(iter(value.receiver.rows.values()))
        active = row["turns"][0]
        meta = {**value.receiver._slack_route(active), "source_event_id": "stop-control-1"}
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", AsyncMock(return_value=True))
        async def enqueue(_event):
            return asyncio.create_task(asyncio.sleep(0))
        value.adapter._enqueue = AsyncMock(side_effect=enqueue)
        assert await value.receiver.slack_stop(meta)
        assert row["stop_controls"]["stop-control-1"] == "consumed"
        assert active["state"] == "quarantined"
        value.receiver.active.clear()
        assert await value.receiver.slack_stop(meta)
        value.adapter._enqueue.assert_awaited_once()
        value.gate.set()
        await idle(value)
        await value.receiver.close()
        await value.adapter._slack_activity.close()
    asyncio.run(run())
