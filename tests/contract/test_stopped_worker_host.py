"""Exercise the installed host's real executor-finalizer contract after Stop."""
import asyncio
import threading
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("hermes_cli.plugins")

from gateway.run import GatewayRunner
from gateway.run_agent_cache import GatewayAgentCacheMixin
from gateway.run_turn import GatewayTurnMixin
from gateway.session_state import SessionState
from gateway.turn_context import TurnContext
from inkbox_plugin.host_fencing import completed_worker_proof, observe_native_workers, stopped_worker_proof


def test_stopped_real_worker_exit_survives_generation_and_slot_removal(monkeypatch):
    async def run():
        state = SessionState()

        class Runner(GatewayAgentCacheMixin, GatewayTurnMixin):
            _RunAgentWorker = GatewayRunner._RunAgentWorker
            _run_in_executor_with_context = GatewayRunner._run_in_executor_with_context

            def _get_executor(self):
                return None

            def _peek_session_state(self, key):
                return state

            def _session_state(self, key):
                return state

        owner = Runner()
        observe_native_workers(owner)
        event = NS(message_id="synthetic-source")
        callbacks = []
        event._inkbox_worker_exited = lambda original: callbacks.append(stopped_worker_proof(owner, original))
        state.turn.event = event
        state.persistent.run_generation = 1
        release = threading.Event()
        agent = NS(_gateway_turn_process_task_id="synthetic-session", _gateway_turn_process_baseline=frozenset())
        context = TurnContext(session_key="actual-host-thread", session_id="synthetic-session", run_generation=1,
                              inbound_message_id=event.message_id, agent_holder=[agent])
        monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
        worker = owner._run_agent_start_turn_worker(context, lambda: release.wait(5))
        try:
            state.persistent.run_generation = 2
            state.turn.clear()
            assert stopped_worker_proof(owner, event) is None
            release.set()
            await worker.executor_task
            await asyncio.sleep(0)
            assert completed_worker_proof(owner, event) is None
            proof = stopped_worker_proof(owner, event)
            assert proof == {"kind": "stopped_native_worker_finalizer", "source_id": "synthetic-source",
                             "session_key": "actual-host-thread", "generation": 1}
            assert callbacks == [proof]
            assert agent._gateway_turn_process_task_id == ""
        finally:
            release.set()
            await worker.executor_task
    asyncio.run(run())
