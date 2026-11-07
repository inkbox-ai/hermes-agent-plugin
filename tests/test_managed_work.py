import asyncio

from inkbox_plugin.managed_work import DeliveryWork, adopt, replace, scope, spawn


def test_debounce_replacement_settles_all_inputs_from_the_merged_turn():
    async def scenario():
        gate = asyncio.Event()
        first, second = DeliveryWork(), DeliveryWork()
        with scope((first,)):
            timer = spawn(gate.wait())
        first.seal()
        with scope((second,)):
            merged = replace(gate.wait(), timer)
        second.seal()
        await asyncio.sleep(0)
        assert not first.done.done() and not second.done.done()
        gate.set()
        await merged
        assert await first.done is True and await second.done is True
    asyncio.run(scenario())


def test_immediate_flush_adopts_timer_without_false_failure():
    async def scenario():
        work = DeliveryWork()
        with scope((work,)):
            timer = spawn(asyncio.Event().wait())
        work.seal()
        with adopt(timer):
            timer.cancel()
            await asyncio.sleep(0)
            assert not work.done.done()
        assert await work.done is True
    asyncio.run(scenario())


def test_actual_turn_cancellation_remains_uncertain():
    async def scenario():
        work = DeliveryWork()
        with scope((work,)):
            task = spawn(asyncio.Event().wait())
        work.seal()
        task.cancel()
        assert await work.done is False
    asyncio.run(scenario())
