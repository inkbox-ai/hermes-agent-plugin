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


@pytest.mark.parametrize('unavailable', [False, True])
def test_inline_reply_context_reaches_initialization_and_live_companion_turns(factory, unavailable):
    async def run():
        instance = factory('imessage')
        item = setup(instance)
        item['data']['message'].update(content='That is odd', reply_to_message_id=uid(800))
        instance.identity.get_whatsapp_message = Mock(return_value={
            'id': uid(800), 'conversation_id': uid(30), 'agent_identity_id': uid(100),
            'direction': 'outbound', 'content': 'Delivery is still pending.', 'media': [],
        }, side_effect=PermissionError('sensitive failure') if unavailable else None)
        try:
            await instance.receiver.accept(item)
            await idle(instance)
            live = whatsapp_event('live')
            live['companion']['sequence'] = 4
            live['data']['message'].update(id=uid(4), content='And this reply?', reply_to_message_id=uid(800))
            await instance.receiver.accept(live)
            await idle(instance)
            assert len(instance.inputs) == 2
            for current in instance.inputs:
                assert '"reply_to_message_id": "' + uid(800) + '"' in current.text
                if unavailable:
                    assert '"status": "unavailable"' in current.text
                    assert 'Delivery is still pending.' not in current.text and 'sensitive failure' not in current.text
                else:
                    assert '"author": "sample-agent"' in current.text
                    assert 'Delivery is still pending.' in current.text
            assert instance.identity.get_whatsapp_message.call_count == 2
        finally:
            await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize('quiet_mode', ['safe', 'mention'])
def test_context_only_inline_reply_retains_parent_for_later_wake(factory, quiet_mode):
    async def run():
        instance = factory('imessage')
        item = setup(instance)
        instance.identity.get_whatsapp_message = Mock(return_value={
            'id': uid(800), 'conversation_id': uid(30), 'agent_identity_id': uid(100),
            'direction': 'outbound', 'content': 'Earlier quoted answer', 'media': [],
        })
        try:
            await instance.receiver.accept(item)
            await idle(instance)
            assert len(instance.inputs) == 1
            instance.adapter.config.extra.update(group_reply_mode='mention' if quiet_mode == 'mention' else 'auto',
                                                 companion_response_mode='safe')
            quiet = whatsapp_event('live')
            quiet['companion']['sequence'] = 4
            quiet['data']['message'].update(id=uid(4), content='That is odd', reply_to_message_id=uid(800),
                agent_mentioned=False, sender_access='sponsored' if quiet_mode == 'safe' else 'direct')
            await instance.receiver.accept(quiet)
            await idle(instance)
            assert len(instance.inputs) == 1
            row = next(iter(instance.receiver.rows.values()))
            assert row['turns'][-1]['state'] == 'context_only'
            assert 'Earlier quoted answer' in row['turns'][-1]['text']
            wake = whatsapp_event('live')
            wake['companion']['sequence'] = 5
            wake['data']['message'].update(id=uid(5), content='Please explain', agent_mentioned=True)
            await instance.receiver.accept(wake)
            await idle(instance)
            assert len(instance.inputs) == 2
            assert 'Earlier quoted answer' in instance.inputs[-1].text
            assert 'sample-agent' in instance.inputs[-1].text
            assert uid(800) in instance.inputs[-1].text
            instance.identity.get_whatsapp_message.assert_called_once_with(uid(800))
        finally:
            await instance.receiver.close()
    asyncio.run(run())


def test_quoted_companion_source_failure_does_not_read_parent_or_consume_receipt(factory):
    async def run():
        instance = factory('imessage')
        item = setup(instance)
        item['data']['message']['reply_to_message_id'] = uid(800)
        instance.identity.get_whatsapp_message = Mock()
        instance.identity.get_whatsapp_conversation.side_effect = PermissionError('source denied')
        try:
            await instance.receiver.accept(item)
            await idle(instance)
            instance.identity.get_whatsapp_message.assert_not_called()
            assert not instance.inputs
            row = next(iter(instance.receiver.rows.values()))
            assert row['state'] == 'failed'
            assert 'whatsapp_quote' not in row['turns'][0]
        finally:
            await instance.receiver.close()
    asyncio.run(run())


def test_quiet_initialization_quote_is_retained_without_duplicate_lookup(factory):
    async def run():
        instance = factory('imessage')
        item = setup(instance)
        instance.adapter.config.extra['group_reply_mode'] = 'mention'
        item['data']['message'].update(agent_mentioned=False, reply_to_message_id=uid(800))
        instance.identity.get_whatsapp_message = Mock(return_value={
            'id': uid(800), 'conversation_id': uid(30), 'agent_identity_id': uid(100),
            'direction': 'outbound', 'content': 'Quoted before initialization', 'media': [],
        })
        try:
            await instance.receiver.accept(item)
            await idle(instance)
            assert not instance.inputs
            wake = whatsapp_event('live')
            wake['companion']['sequence'] = 4
            wake['data']['message'].update(id=uid(4), content='Please explain')
            await instance.receiver.accept(wake)
            await idle(instance)
            assert len(instance.inputs) == 1
            assert 'Quoted before initialization' in instance.inputs[0].text
            instance.identity.get_whatsapp_message.assert_called_once_with(uid(800))
        finally:
            await instance.receiver.close()
    asyncio.run(run())


def test_inline_reply_source_timeout_retries_before_parent_lookup(factory):
    async def run():
        instance = factory('imessage')
        item = setup(instance)
        item['data']['message']['reply_to_message_id'] = uid(800)
        conversation = instance.identity.get_whatsapp_conversation.return_value
        source_reads = 0

        def read_source(*args):
            nonlocal source_reads
            source_reads += 1
            if source_reads == 1:
                raise TimeoutError('temporary source failure')
            return conversation

        def read_parent(*args):
            assert source_reads >= 2
            return {'id': uid(800), 'conversation_id': uid(30), 'agent_identity_id': uid(100),
                    'direction': 'outbound', 'content': 'Recovered quote', 'media': []}

        instance.identity.get_whatsapp_conversation.side_effect = read_source
        instance.identity.get_whatsapp_message = Mock(side_effect=read_parent)
        instance.receiver.retry_delay = .001
        try:
            await instance.receiver.accept(item)
            await idle(instance)
            assert len(instance.inputs) == 1
            assert 'Recovered quote' in instance.inputs[0].text
            instance.identity.get_whatsapp_message.assert_called_once_with(uid(800))
        finally:
            await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize('explicit_group_reply', [False, True])
def test_rejected_independent_dm_never_borrows_companion_scope_or_blocks_group(factory, monkeypatch, explicit_group_reply):
    import json
    from inkbox import InkboxAPIError
    from inkbox_plugin.imessage_state import clear_context
    from inkbox_plugin.whatsapp_tools import invoke
    async def run():
        instance = factory('imessage')
        item = setup(instance)
        session = None
        try:
            await instance.receiver.accept(item)
            await idle(instance)
            row = next(iter(instance.receiver.rows.values()))
            turn = row['turns'][0]
            session = row['host_session_id']
            instance.receiver._bind_whatsapp_context(row, turn)
            monkeypatch.setattr('inkbox_plugin.tools._client_and_identity', lambda: (
                SimpleNamespace(base_url='https://inkbox.ai'), SimpleNamespace(close=lambda: None), instance.identity))

            def send(**kwargs):
                if 'to' in kwargs:
                    assert set(kwargs) == {'to', 'text', 'idempotency_key'}
                    assert kwargs['to'] == turn['author']
                    raise InkboxAPIError(403, {'error': 'whatsapp_contact_not_allowed'})
                assert kwargs['conversation_id'] == uid(30)
                return SimpleNamespace(id=uid(900))

            instance.identity.send_whatsapp.side_effect = send
            denied = json.loads(invoke('send_whatsapp', {'to': turn['author'], 'text': 'Independent DM'}, session))
            assert denied['error'] == 'whatsapp_contact_not_allowed'
            if explicit_group_reply:
                sent = json.loads(invoke('send_whatsapp', {'conversation_id': uid(30), 'text': 'Group answer'}, session))
                assert sent.get('ok'), sent
            turn.update(result_ready=True, result='Group answer')
            result = await instance.receiver.send('companion:' + row['key'], 'Group answer', turn['id'])
            assert result.success and result.message_id
            assert instance.identity.send_whatsapp.call_count == 2
            assert instance.identity.send_whatsapp.call_args.kwargs['conversation_id'] == uid(30)
        finally:
            if session:
                clear_context(session, turn["id"])
            await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize('http_error', [False, True])
def test_transient_parent_read_admits_current_turn_and_later_quote_can_recover(factory, http_error):
    async def run():
        instance = factory('imessage')
        item = setup(instance)
        item['data']['message']['reply_to_message_id'] = uid(800)
        instance.receiver.retry_delay = .001
        import httpx
        failure = httpx.HTTPStatusError('temporary', request=httpx.Request('GET', 'https://example.test/messages/parent'),
            response=httpx.Response(503)) if http_error else TimeoutError('temporary')
        instance.identity.get_whatsapp_message = Mock(side_effect=[failure, {
            'id': uid(800), 'conversation_id': uid(30), 'agent_identity_id': uid(100),
            'direction': 'outbound', 'content': 'Recovered parent', 'media': [],
        }])
        try:
            await instance.receiver.accept(item)
            await idle(instance)
            assert len(instance.inputs) == 1
            assert '"status": "temporarily_unavailable"' in instance.inputs[0].text
            assert 'Recovered parent' not in instance.inputs[0].text
            assert uid(800) in instance.inputs[0].text
            assert instance.identity.get_whatsapp_message.call_count == 1
            followup = whatsapp_event('live')
            followup['companion']['sequence'] = 4
            followup['data']['message'].update(id=uid(4), reply_to_message_id=uid(800), content='Another reply')
            await instance.receiver.accept(followup)
            await idle(instance)
            assert len(instance.inputs) == 2
            assert 'Recovered parent' in instance.inputs[1].text
            assert instance.identity.get_whatsapp_message.call_count == 2
        finally:
            await instance.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure, expected', [(PermissionError('denied'), 'unavailable'),
                                             (TimeoutError('temporary'), 'temporarily_unavailable')])
@pytest.mark.parametrize('initial', [False, True])
def test_quiet_quote_authorization_failure_does_not_block_later_wake(factory, failure, expected, initial):
    async def run():
        from unittest.mock import AsyncMock
        instance = factory('imessage')
        item = setup(instance)
        instance.identity.get_whatsapp_message = Mock()
        original = instance.receiver._authorized_source
        try:
            if not initial:
                await instance.receiver.accept(item)
                await idle(instance)
            instance.adapter.config.extra['group_reply_mode'] = 'mention'
            quiet = item if initial else whatsapp_event('live')
            if not initial:
                quiet['companion']['sequence'] = 4
                quiet['data']['message']['id'] = uid(4)
            quiet['data']['message'].update(content='That is odd', reply_to_message_id=uid(800),
                                            agent_mentioned=False)
            instance.receiver._authorized_source = AsyncMock(side_effect=failure)
            await instance.receiver.accept(quiet)
            await idle(instance)
            row = next(iter(instance.receiver.rows.values()))
            assert row['turns'][-1]['state'] == 'context_only'
            assert f'"status": "{expected}"' in row['turns'][-1]['text']
            instance.identity.get_whatsapp_message.assert_not_called()
            instance.receiver._authorized_source = original
            wake = whatsapp_event('live')
            wake['companion']['sequence'] = 5
            wake['data']['message'].update(id=uid(5), content='Please explain', agent_mentioned=True)
            await instance.receiver.accept(wake)
            await idle(instance)
            assert len(instance.inputs) == (1 if initial else 2)
            assert f'"status\\\": \\\"{expected}' in instance.inputs[-1].text
        finally:
            await instance.receiver.close()
    asyncio.run(run())


def test_managed_whatsapp_delivery_preserves_native_admission_and_dedup(factory, monkeypatch, tmp_path):
    from uuid import uuid4
    from inkbox_plugin.managed_ingress import ManagedIngress
    from tests.test_managed_inbox import event as managed_event

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    async def run():
        instance = factory('imessage')
        item = setup(instance)
        activation = str(uuid4())
        receipt = managed_event(activation)
        item.update(id=receipt['id'], hosted_delivery=receipt['hosted_delivery'])
        instance.adapter._base_url = 'https://api.example.test'
        ingress = ManagedIngress(instance.adapter, activation)
        try:
            assert ingress.inbox.accept(item)['state'] == 'accepted'
            ingress.start()
            async def accepted():
                while ingress.inbox.accept(item)['state'] != 'completed':
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(accepted(), 3)
            await idle(instance)
            assert len(instance.inputs) == 1
            assert instance.inputs[0].source.thread_id.startswith('whatsapp:')
            assert ingress.inbox.accept(item)['state'] == 'completed'
            assert ingress.inbox.claim() is None
            await instance.receiver.accept(item)
            await idle(instance)
            assert len(instance.inputs) == 1
        finally:
            await ingress.close()
            await instance.receiver.close()
    asyncio.run(run())
