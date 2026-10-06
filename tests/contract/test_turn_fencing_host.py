"""Exact-worker fencing through current native Hermes generation/process APIs."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway.run_agent_cache import GatewayAgentCacheMixin
from gateway.session_state import SessionState
from inkbox_plugin.host_fencing import fence_turn


@pytest.mark.parametrize("settles", [False, True])
def test_native_fence_waits_for_worker_finalizer_and_preserves_grants(settles):
    async def run():
        state = SessionState()
        event = NS(message_id="original-event", metadata={})
        interrupted = asyncio.Event()
        class Agent:
            _gateway_turn_process_task_id = "native-contract-worker"
            _gateway_turn_process_baseline = frozenset()
            def interrupt(self, *args, **kwargs):
                interrupted.set()
        agent = Agent()
        state.turn.agent, state.turn.event = agent, event
        class Runner(GatewayAgentCacheMixin):
            _agent_cache = {}
            _pending_approvals = {"conversation": {"command": "pending"}}
            def _peek_session_state(self, _):
                return state
            def _session_state(self, _):
                return state
            def _restore_pending_one_turn_model_override(self, _):
                return None
        owner = Runner()
        adapter = NS(gateway_runner=owner, _conversation_session_key=lambda _: "conversation", _session_tasks={},
                     cancel_session_processing=AsyncMock())
        from tools import approval
        # Existing remembered approvals and global policy are deliberately not
        # touched by cancellation of this original pending permission.
        original_clear = approval.clear_session
        approval.clear_session = Mock(side_effect=AssertionError("must not clear session grants"))
        async def worker_finalizer():
            await interrupted.wait()
            await asyncio.sleep(.03)
            agent._gateway_turn_process_task_id = ""
        finalizer = asyncio.create_task(worker_finalizer()) if settles else None
        try:
            assert await fence_turn(adapter, NS(), "another-event", timeout=.1) is False
            assert state.persistent.run_generation == 0
            result = await fence_turn(adapter, NS(), "original-event", timeout=.15)
            assert result is settles
            assert state.persistent.run_generation == 1
            assert event.metadata["inkbox_interrupted"]
            if settles:
                adapter.cancel_session_processing.assert_awaited_once_with("conversation", discard_pending=False)
            else:
                adapter.cancel_session_processing.assert_not_awaited()
            assert "conversation" not in owner._pending_approvals
            approval.clear_session.assert_not_called()
        finally:
            approval.clear_session = original_clear
            if finalizer:
                finalizer.cancel()
                await asyncio.gather(finalizer, return_exceptions=True)
    asyncio.run(run())
