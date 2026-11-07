import asyncio
from types import SimpleNamespace

from inkbox_plugin.managed_lifecycle import status


def test_idle_daemon_and_future_jobs_are_not_active():
    adapter = SimpleNamespace(is_connected=True)
    assert status(adapter) == {"ready": True, "active": 0, "unknown": False}


def test_conversation_quarantine_does_not_disable_transport():
    receiver = SimpleNamespace(tasks={}, outbound=set(), rows={"contact": {"turns": [{"state": "uncertain"}]}})
    adapter = SimpleNamespace(is_connected=True, _native_turns=receiver)
    assert status(adapter) == {"ready": True, "active": 0, "unknown": True}
    adapter._managed_draining = True
    assert not status(adapter)["ready"]


def test_ingress_and_delivery_remain_active_until_finished():
    async def scenario():
        gate = asyncio.Event()
        task = asyncio.create_task(gate.wait())
        receiver = SimpleNamespace(tasks={}, outbound={task}, rows={})
        adapter = SimpleNamespace(is_connected=True, _managed_ingress=1, _companion=receiver)
        assert status(adapter)["active"] == 2
        gate.set()
        await task
        adapter._managed_ingress = 0
        assert status(adapter)["active"] == 0
    asyncio.run(scenario())


def test_fenced_or_previous_boot_unknown_does_not_pin_unrelated_work(monkeypatch):
    from inkbox_plugin import managed_inbox
    monkeypatch.setattr(managed_inbox, 'boot_id', lambda: 'current-boot')
    turn = {'state': 'uncertain', 'execution_boot_id': 'old-boot'}
    receiver = SimpleNamespace(tasks={}, outbound=set(), rows={'contact': {'turns': [turn]}})
    adapter = SimpleNamespace(is_connected=True, _native_turns=receiver)
    assert status(adapter)['unknown'] is False
    assert turn['state'] == 'uncertain'
    turn['execution_boot_id'] = 'current-boot'
    assert status(adapter)['unknown'] is True
    turn['fenced'] = True
    assert status(adapter)['unknown'] is False
