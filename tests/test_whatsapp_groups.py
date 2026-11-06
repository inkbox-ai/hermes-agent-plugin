"""Group routing and native mention behavior through the Hermes lifecycle."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from inkbox_plugin.whatsapp_groups import check_destination, render_mentions, attention_text, control_text
from inkbox_plugin.conversation import wakes
from inkbox_plugin.whatsapp import receive
from tests import test_whatsapp as whatsapp_harness
from tests.test_whatsapp import harness, real_identity, inbound, conversation_wire, uid

factory = whatsapp_harness.factory


def group(**changes):
    return conversation_wire(uid(30), is_group=True, remote_number=None,
        participants=['+15555550101', '+15555550102'], group_name='Example group',
        unresolved_participant_count=0, **changes)


def test_names_self_handle_and_unknown_numbers_do_not_change_raw_mentions():
    async def run():
        adapter = NS(_identity_handle='sample-agent', _resolve_contact_full=AsyncMock(return_value={'name': 'Taylor', 'memories': ['private']}))
        source = {'content': '@123 @1234 @234 @345 @123', 'mentions': [
            {'text': '@123', 'phone_number': '+15555550100', 'is_agent': True},
            {'text': '@234', 'phone_number': '+15555550102', 'is_agent': False},
            {'text': '@345', 'phone_number': None, 'is_agent': False}]}
        text, mentions = await render_mentions(adapter, source)
        assert text == '@sample-agent @1234 @Taylor @unresolved mention @sample-agent'
        assert source['content'] == '@123 @1234 @234 @345 @123'
        assert 'private' not in repr(mentions)
        adapter._resolve_contact_full.assert_awaited_once_with(kind='phone', value='+15555550102')
        assert control_text({**source, 'content': '@123 yes'}, 'sample-agent') == 'yes'
    asyncio.run(run())


def test_failed_contact_lookup_falls_back_to_verified_phone():
    adapter = NS(_identity_handle='agent', _resolve_contact_full=AsyncMock(side_effect=TimeoutError('private')))
    text, _ = asyncio.run(render_mentions(adapter, {'content': '@123', 'mentions': [
        {'text': '@123', 'phone_number': '+15555550102', 'is_agent': False}]}))
    assert text == '@+15555550102'


def test_auto_is_contextual_and_native_mention_gate_uses_current_agent_only():
    adapter = NS(config=NS(extra={'group_reply_mode': 'mention', 'companion_response_mode': 'relaxed'}), _identity_handle='123')
    item = {'content': '@123 hello', 'sender_access': 'sponsored', 'mentions': [
        {'text': '@123', 'phone_number': '+15555550102', 'is_agent': False}]}
    assert not wakes(adapter, 'whatsapp', item)
    assert wakes(adapter, 'whatsapp', {**item, 'agent_mentioned': True})
    adapter.config.extra['group_reply_mode'] = 'auto'
    assert wakes(adapter, 'whatsapp', item)
    adapter.config.extra['companion_response_mode'] = 'safe'
    assert not wakes(adapter, 'whatsapp', item)
    assert wakes(adapter, 'whatsapp', {**item, 'sender_access': 'direct'})
    assert '@123' not in attention_text(item)


def test_current_group_roster_rejects_new_blocked_member():
    adapter = NS(build_source=lambda **kw: NS(**kw), gateway_runner=NS(_is_user_authorized=lambda s: s.user_id != '+15555550102'))
    identity = NS(id=uid(100), get_whatsapp_conversation=Mock(return_value=group()))
    with pytest.raises(PermissionError, match='participant'):
        check_destination(adapter, identity, uid(30))


@pytest.mark.parametrize('native,expected', [(False, 0), (True, 1)])
def test_group_receipt_isolated_and_no_contact_memory(factory, tmp_path, native, expected):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.adapter._reply_identity.get_whatsapp_conversation = Mock(return_value=NS(**group()))
        value.adapter._message_handler.__self__._is_user_authorized = lambda source: True
        value.adapter.config.extra['group_reply_mode'] = 'mention'
        accepted = []
        value.adapter._accept_native_imessage = AsyncMock(side_effect=lambda event: accepted.append(event))
        source = inbound(**{**group(), "id": uid(1)}, direction='inbound', sender_number='+15555550101', content='@123 hello', agent_mentioned=native,
                         mentions=[{'text': '@123', 'phone_number': '+15555550100', 'is_agent': True}])
        try:
            response = await receive(value.adapter, source)
            assert response.status == 202
            assert len(accepted) == expected
            value.adapter._contact_memories.assert_not_called()
            if accepted:
                event = accepted[0]
                assert event.source.chat_id == 'whatsapp:' + uid(30)
                assert event.source.user_id == '+15555550101'
                assert event.source.chat_type == 'group'
                route = event.metadata['inkbox_reply_route']
                assert route['remote_number'] is None and route['is_group'] is True
                assert route['agent_mentioned'] is native
                assert '@sample-agent' in event.text
                assert 'phone_number' in event.text and 'original_text' in event.text
                assert route['whatsapp_sources'][0]['message_id'] == uid(1)
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_group_wire_metadata_survives_compatible_sdk():
    from inkbox.whatsapp import WhatsAppConversation
    if 'is_group' not in getattr(WhatsAppConversation, '__dataclass_fields__', {}):
        pytest.skip('group wire decoding requires the compatible SDK lane')
    from inkbox import Inkbox
    client = Inkbox('test-only', base_url='http://127.0.0.1:9')
    client.whatsapp._http = Mock()
    client.whatsapp._http.get.return_value = group()
    result = client.whatsapp.get_conversation(uid(30), agent_identity_id=uid(100))
    assert result.is_group is True and result.remote_number is None
    assert result.group_name == 'Example group' and len(result.participants) == 2
    client.close()


def test_group_automatic_reply_keeps_conversation_after_native_host_turn(factory, tmp_path):
    from tests.test_native_turns import settle
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.adapter._reply_identity.get_whatsapp_conversation = Mock(return_value=NS(**group()))
        value.adapter._message_handler.__self__._is_user_authorized = lambda source: True
        try:
            source = inbound(**{**group(), 'id': uid(1)}, direction='inbound', sender_number='+15555550101', content='Can you help?')
            assert (await receive(value.adapter, source)).status == 202
            await settle(value.queue)
            assert len(value.inputs) == 1
            assert value.inputs[0].source.chat_type == 'group'
            client.whatsapp._http.post.assert_called_once()
            payload = client.whatsapp._http.post.call_args.kwargs['json']
            assert payload['conversation_id'] == uid(30)
            assert 'to' not in payload and 'reply_to_message_id' not in payload
            assert next(iter(value.queue.rows.values()))['turns'][0]['state'] == 'done'
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_direct_native_mentions_render_without_group_attention_gate(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        real_identity(value.adapter)
        value.adapter.config.extra['group_reply_mode'] = 'mention'
        accepted = []
        value.adapter._accept_native_imessage = AsyncMock(side_effect=lambda event: accepted.append(event))
        try:
            source = inbound(content='Ask @234 please', mentions=[
                {'text': '@234', 'phone_number': '+15555550102', 'is_agent': False}])
            assert (await receive(value.adapter, source)).status == 202
            assert len(accepted) == 1
            assert '@Saved contact' in accepted[0].text
            assert accepted[0].source.chat_type == 'dm'
            assert accepted[0].metadata['inkbox_reply_route']['original_text'] == 'Ask @234 please'
        finally:
            await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize('rejected', [False, True])
def test_standalone_explicit_group_send_preserves_server_authority(factory, monkeypatch, rejected):
    import json
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    from inkbox_plugin.whatsapp_tools import invoke
    value = factory('imessage')
    client = real_identity(value.adapter)
    identity = value.adapter._reply_identity
    identity.get_whatsapp_conversation = Mock(return_value=NS(**group()))
    monkeypatch.setattr(tools, '_client_and_identity', lambda: (NS(base_url=value.adapter._base_url), client, identity))
    if rejected:
        client.whatsapp._http.post.side_effect = InkboxAPIError(409, {'error': 'whatsapp_group_reply_required'})
    try:
        result = json.loads(invoke('send_whatsapp', {'conversation_id': uid(30), 'text': 'Requested reply', 'idempotency_key': 'standalone-group-request'}))
        assert ('error' in result) is rejected
        client.whatsapp._http.post.assert_called_once()
        assert client.whatsapp._http.post.call_args.kwargs['json'] == {'conversation_id': uid(30), 'text': 'Requested reply'}
        assert client.whatsapp._http.post.call_args.kwargs['headers']['Idempotency-Key'] == 'standalone-group-request'
    finally:
        client.close()


def test_burst_preserves_each_source_mention_context(factory, tmp_path):
    from tests.test_native_turns import settle
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.queue.quiet_seconds = .1
        value.queue.max_burst_seconds = 1
        value.adapter._reply_identity.get_whatsapp_conversation = Mock(return_value=NS(**group()))
        value.adapter._message_handler.__self__._is_user_authorized = lambda source: True
        try:
            for number, token, agent in [(1, '@234', False), (2, '@123', True)]:
                source = inbound(**{**group(), 'id': uid(number)}, direction='inbound', sender_number='+15555550101',
                                 content='Hello ' + token, agent_mentioned=agent, mentions=[
                    {'text': token, 'phone_number': '+15555550102' if not agent else '+15555550100', 'is_agent': agent}])
                assert (await receive(value.adapter, source)).status == 202
            await settle(value.queue)
            assert len(value.inputs) == 1
            route = value.inputs[0].metadata['inkbox_reply_route']
            assert route['agent_mentioned'] is True
            assert [item['original_text'] for item in route['whatsapp_sources']] == ['Hello @234', 'Hello @123']
            assert '@Saved contact' in value.inputs[0].text and '@sample-agent' in value.inputs[0].text
            assert all(source['message_id'] in value.inputs[0].text for source in route['whatsapp_sources'])
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())
