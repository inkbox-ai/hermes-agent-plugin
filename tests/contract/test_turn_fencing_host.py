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


def test_completed_real_worker_survives_native_slot_cleanup_and_restores_forward_progress(tmp_path, monkeypatch):
    """An uncertain send can quarantine its answer after positive worker completion."""
    async def run():
        from gateway.run import GatewayRunner
        from gateway.run_turn import GatewayTurnMixin
        from gateway.turn_context import TurnContext
        from gateway.platforms.base import MessageEvent, MessageType
        from inkbox_plugin.native_turns import NativeTurns
        from inkbox_plugin.host_fencing import observe_native_workers
        from tests.contract.test_native_turn_host import uid
        state = SessionState()
        class Runner(GatewayAgentCacheMixin, GatewayTurnMixin):
            _RunAgentWorker = GatewayRunner._RunAgentWorker
            _run_in_executor_with_context = GatewayRunner._run_in_executor_with_context
            def _get_executor(self):
                return None
            def _peek_session_state(self, _):
                return state
            def _session_state(self, _):
                return state
            def _is_user_authorized(self, _):
                return True
        owner = Runner()
        observe_native_workers(owner)
        source = NS(chat_id="conversation", chat_type="group", thread_id="conversation", user_id="owner",
                    user_id_alt="owner", user_name="Owner", chat_name="Conversation")
        original = MessageEvent(source=source, message_type=MessageType.TEXT, message_id=uid(1), text="First",
            metadata={"inkbox_reply_route": {"mode": "imessage", "chat_id": "conversation", "conversation_id": uid(30),
                "message_id": uid(1), "imessage_reply_target": uid(1), "author": "owner"}})
        state.turn.event = original
        state.persistent.run_generation = 1
        agent = NS(_gateway_turn_process_task_id="test-worker", _gateway_turn_process_baseline=frozenset())
        state.turn.agent = agent
        context = TurnContext(session_key="session-key", session_id="session-id", run_generation=1,
            inbound_message_id=uid(1), agent_holder=[agent])
        monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
        worker = owner._run_agent_start_turn_worker(context, lambda: "Finished answer")
        response = await worker.executor_task
        assert worker.worker_done.is_set()
        state.turn.clear()  # actual native handler drops this slot before adapter send
        identity = NS(send_imessage=Mock(side_effect=TimeoutError("ambiguous delivery")))
        adapter = NS(gateway_runner=owner, _message_handler=None, _imessage_threaded_replies=True, _slack_enabled=False,
                     _reply_identity=identity, build_source=lambda **kwargs: NS(**kwargs))
        queue = NativeTurns(adapter, tmp_path / "native")
        queue._session_key = lambda _: "session-key"
        await queue.start()
        row = {"version": 1, "key": "original", "state": "ordinary", "chat_id": "conversation", "turns": []}
        turn = {"id": uid(1), "state": "running", "source_ids": [uid(1)], "route": original.metadata["inkbox_reply_route"],
                "event": queue._serialize(original)}
        row["turns"].append(turn)
        queue.rows[row["key"]] = row
        queue.active["conversation"] = turn
        original.raw_message = {"_inkbox_native_turn": uid(1), "_inkbox_native_key": row["key"]}
        queue.capture_result(original, response)
        assert turn["worker_completion"] == {"kind": "native_worker_finalizer", "source_id": uid(1), "session_key": "session-key", "generation": 1}
        result = await queue._send(row, turn, response)
        assert not result.success and turn["state"] == "uncertain"
        queue.active.clear()
        await queue.close()
        for _ in range(2):
            recovered = NativeTurns(adapter, tmp_path / "native")
            await recovered.start()
            await asyncio.gather(*list(recovered.tasks.values()))
            restored = recovered.rows["original"]
            assert not restored.get("blocked")
            assert restored["turns"][0]["fenced"] is True
            assert restored["turns"][0]["state"] == "uncertain"
            identity.send_imessage.assert_called_once()
            await recovered.close()
    asyncio.run(run())
