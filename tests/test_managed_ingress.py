import asyncio
from types import SimpleNamespace
from uuid import uuid4

from aiohttp import web

from inkbox_plugin.managed_ingress import ManagedIngress
from inkbox_plugin.managed_work import spawn


def envelope(activation):
    event = 'evt_' + 'a' * 64
    return {'id': event, 'event_type': 'message.received', 'data': {}, 'hosted_delivery': {
        'version': 1, 'activation_id': activation, 'event_id': event, 'starts_agent': True,
    }}


def test_receipt_waits_for_background_native_admission(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    async def scenario():
        admitted, release = asyncio.Event(), asyncio.Event()
        async def native_admit():
            admitted.set()
            await release.wait()
        async def route(*_):
            spawn(native_admit())
            return web.Response(status=200)
        adapter = SimpleNamespace(_base_url='https://api.example.test', _identity_id='example', _route_inkbox_event=route)
        activation = str(uuid4())
        receiver = ManagedIngress(adapter, activation)
        try:
            receiver.inbox.accept(envelope(activation))
            receiver.start()
            await asyncio.wait_for(admitted.wait(), 2)
            assert receiver.inbox.summary()['active'] == 1
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            assert receiver.inbox.accept(envelope(activation))['state'] == 'completed'
        finally:
            await receiver.close()
    asyncio.run(scenario())


def test_failed_native_admission_is_not_reported_as_complete(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    async def scenario():
        async def native_admit():
            raise RuntimeError('Synthetic admission failure')
        async def route(*_):
            spawn(native_admit())
            return web.Response(status=200)
        adapter = SimpleNamespace(_base_url='https://api.example.test', _identity_id='example', _route_inkbox_event=route)
        activation = str(uuid4())
        receiver = ManagedIngress(adapter, activation)
        try:
            receiver.inbox.accept(envelope(activation))
            receiver.start()
            for _ in range(10):
                await asyncio.sleep(0)
            assert receiver.inbox.accept(envelope(activation))['state'] == 'unknown'
            assert receiver.inbox.claim() is None
        finally:
            await receiver.close()
    asyncio.run(scenario())


async def _cancelled_progress_indicator_does_not_fail_delivery():
    from inkbox_plugin.managed_work import DeliveryWork, background, scope, spawn

    work = DeliveryWork()
    async def progress():
        # Even descendants of the progress task must not inherit delivery ownership.
        await spawn(asyncio.sleep(60))
    with scope((work,)):
        task = background(progress())
    await asyncio.sleep(0)
    work.seal()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert await work.done


def test_cancelled_progress_indicator_does_not_fail_delivery():
    asyncio.run(_cancelled_progress_indicator_does_not_fail_delivery())


def test_hosted_legacy_subscription_is_ignored_only_after_signature_check(monkeypatch):
    import json
    from unittest.mock import AsyncMock
    import inkbox_plugin.adapter as module
    monkeypatch.setenv('INKBOX_HOSTED_ACTIVATION_ID', str(uuid4()))
    async def scenario():
        adapter = object.__new__(module.InkboxAdapter)
        adapter._require_signature = False
        adapter._provider_secret = lambda _: 'synthetic'
        adapter._route_inkbox_event = AsyncMock()
        provider = SimpleNamespace(name='inkbox', verify=lambda **_: True)
        monkeypatch.setattr(module, 'match_provider', lambda _: provider)
        request = SimpleNamespace(read=AsyncMock(return_value=b'{"event_type":"message.received"}'), headers={}, url='https://receiver.example.test/webhook')
        response = await adapter._accept_webhook(request)
        assert json.loads(response.body)['ignored'] == 'managed-delivery-required'
        adapter._route_inkbox_event.assert_not_awaited()
        provider.verify = lambda **_: False
        assert (await adapter._accept_webhook(request)).status == 401
    asyncio.run(scenario())


def test_proven_pre_execution_denial_settles_without_replay_or_drain_pin(tmp_path, monkeypatch):
    from inkbox_plugin.managed_work import AdmissionRejected
    from inkbox_plugin.managed_lifecycle import status
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    async def scenario():
        calls = []
        async def native_admit():
            calls.append('denied')
            raise AdmissionRejected('native_sender_not_authorized')
        async def route(*_):
            spawn(native_admit())
            return web.Response(status=200)
        adapter = SimpleNamespace(_base_url='https://api.example.test', _identity_id='example',
            _route_inkbox_event=route, is_connected=True)
        activation = str(uuid4())
        receiver = ManagedIngress(adapter, activation)
        adapter._managed_receiver = receiver
        try:
            value = envelope(activation)
            receiver.inbox.accept(value)
            receiver.start()
            for _ in range(20):
                await asyncio.sleep(0)
            assert receiver.inbox.summary() == {'pending': 0, 'active': 0, 'unknown': 0, 'review_required': 0}
            observation = status(adapter)
            assert observation['active'] == 0 and observation['unknown'] is False
            assert observation['last_delivery_failure']['code'] == 'native_sender_not_authorized'
            assert observation['last_delivery_failure']['event_id'] == value['id']
            assert receiver.inbox.accept(value)['state'] == 'unknown'
            assert receiver.inbox.claim() is None
            assert calls == ['denied']
        finally:
            await receiver.close()
    asyncio.run(scenario())
