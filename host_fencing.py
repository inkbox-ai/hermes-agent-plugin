"""Conclusive cancellation of an exact native Hermes worker generation."""
from __future__ import annotations

import asyncio
from collections.abc import MutableMapping


async def cancel_pending_permissions(owner, session_key):
    """Deny unresolved requests without changing remembered session grants."""
    try:
        from tools.approval import list_gateway_approvals, resolve_gateway_approval
        for request in list_gateway_approvals(session_key):
            resolve_gateway_approval(session_key, "deny", request_id=request["request_id"])
    except ImportError:
        pass
    pending = getattr(owner, "_pending_approvals", None)
    if isinstance(pending, MutableMapping):
        pending.pop(session_key, None)


async def fence_turn(adapter, source, message_id, *, timeout=5.0):
    """Release successors only after this host worker's finalizer has run.

    A canceled asyncio task or released session guard alone is not sufficient:
    Hermes may still have an executor thread inside a synchronous model/tool call.
    The current native worker clears its process-task ownership marker in its
    finally block. Missing interfaces/markers are deliberately not proof of exit.
    """
    owner = getattr(adapter, "gateway_runner", None) or getattr(getattr(adapter, "_message_handler", None), "__self__", None)
    peek = getattr(owner, "_peek_session_state", None)
    interrupt = getattr(owner, "_interrupt_running_turn", None)
    evict = getattr(owner, "_evict_cached_agent", None)
    if not all(callable(method) for method in (peek, interrupt, evict)):
        return False
    key = adapter._conversation_session_key(source)
    state = peek(key)
    turn = getattr(state, "turn", None)
    event, agent = getattr(turn, "event", None), getattr(turn, "agent", None)
    if event is None:
        event = getattr(getattr(adapter, "_active_sessions", {}).get(key), "_inkbox_event", None)
    if (event is None or str(event.message_id) != str(message_id) or agent is None
            or not hasattr(agent, "_gateway_turn_process_task_id")):
        return False
    baseline = getattr(agent, "_gateway_turn_process_baseline", None)
    process_id = agent._gateway_turn_process_task_id
    event.metadata = {**(event.metadata or {}), "inkbox_interrupted": True}
    # The native method invalidates generation-bound callbacks and issues a hard
    # interrupt. It does not clear unrelated approval preferences or host config.
    generation = interrupt(key, interrupt_reason="Superseded request", invalidation_reason="inkbox_turn_superseded")
    await cancel_pending_permissions(owner, key)
    task = None
    try:
        async with asyncio.timeout(timeout):
            while agent._gateway_turn_process_task_id:
                if getattr(getattr(peek(key), "turn", None), "agent", None) not in (None, agent):
                    return False
                await asyncio.sleep(.02)
        if process_id and baseline is not None:
            from gateway.run import _reap_gateway_turn_processes
            await asyncio.to_thread(_reap_gateway_turn_processes, process_id, baseline,
                source="inkbox_turn_superseded", is_still_current=lambda: owner._is_session_run_current(key, generation))
            from tools.process_registry import process_registry
            if process_registry.snapshot_running_ids(process_id) - set(baseline):
                return False
        if not owner._is_session_run_current(key, generation):
            return False
        evict(key)
        task = getattr(adapter, "_session_tasks", {}).get(key)
        await adapter.cancel_session_processing(key, discard_pending=False)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        return True
    except asyncio.CancelledError:
        if task is not None and task.cancelled():
            return True
        raise
    except TimeoutError:
        return False


def observe_native_workers(owner):
    """Attach completion evidence at the real worker creation boundary once.

    Hermes clears its turn slot before adapter delivery. Retaining this exact
    worker handle avoids mistaking a missing slot for proof after that cleanup.
    """
    start = getattr(owner, "_run_agent_start_turn_worker", None)
    if not callable(start) or getattr(start, "_inkbox_observer", False):
        return
    from functools import wraps
    @wraps(start)
    def observed(context, run_sync):
        state = owner._peek_session_state(context.session_key)
        event = getattr(getattr(state, "turn", None), "event", None)
        worker = start(context, run_sync)
        if event is not None and str(event.message_id or "") == str(context.inbound_message_id or ""):
            from .imessage_state import observe_host_turn
            observe_host_turn(str(context.session_id), str(event.message_id or ""))
            event._inkbox_native_worker = (worker, context.session_key, context.run_generation, str(event.message_id))
        return worker
    observed._inkbox_observer = True
    owner._run_agent_start_turn_worker = observed


def completed_worker_proof(owner, event):
    receipt = getattr(event, "_inkbox_native_worker", None)
    if receipt is None:
        return None
    worker, key, generation, source_id = receipt
    future = getattr(worker, "executor_task", None)
    done = getattr(worker, "worker_done", None)
    current = getattr(owner, "_is_session_run_current", None)
    if (str(event.message_id) != source_id or future is None or not future.done() or future.cancelled()
            or done is None or not done.is_set() or not callable(current) or not current(key, generation)):
        return None
    return {"kind": "native_worker_finalizer", "source_id": source_id, "session_key": key, "generation": generation}
