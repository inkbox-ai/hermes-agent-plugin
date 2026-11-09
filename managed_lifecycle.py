"""Activity and admission fences for a supervised plugin."""


def _uncertain(value):
    if isinstance(value, dict):
        from .managed_inbox import execution_stopped
        if execution_stopped(value.get("execution_boot_id"), value.get("execution_owner_id")):
            return False
        if value.get("fenced"):
            return False
        return value.get("state") in {"uncertain", "submitting", "control_submitting"} or any(_uncertain(v) for v in value.values())
    return isinstance(value, list) and any(_uncertain(v) for v in value)


def status(adapter):
    active = int(getattr(adapter, "_managed_ingress", 0))
    unknown = False
    failure = None
    receiver = getattr(adapter, "_managed_receiver", None)
    if receiver is not None:
        summary = receiver.inbox.summary()
        failure = receiver.inbox.latest_failure()
        active += summary["pending"] + summary["active"]
        unknown |= bool(summary["unknown"] or any(task.done() for task in receiver.tasks))
    for name in ("_pending_sms_text_batch_tasks", "_a2a_admission_tasks"):
        tasks = getattr(adapter, name, {})
        if isinstance(tasks, dict):
            tasks = tasks.values()
        active += sum(not task.done() for task in tasks)
    active += sum(getattr(adapter, "_hosted_post_call_active_chats", {}).values())
    for name in ("_companion", "_native_turns"):
        receiver = getattr(adapter, name, None)
        if receiver is None:
            continue
        active += sum(not task.done() for task in receiver.tasks.values())
        active += sum(not task.done() for task in receiver.outbound)
        unknown |= _uncertain(list(receiver.rows.values()))
        if name == "_native_turns":
            for row in receiver.rows.values():
                for turn in row.get("turns", []):
                    if (turn.get("error_code") == "native_sender_not_authorized"
                            and turn.get("rejected_unix_ms", 0) > (failure or {}).get("finished_unix_ms", 0)):
                        failure = {"event_id": None, "code": turn["error_code"], "finished_unix_ms": turn["rejected_unix_ms"]}
    return {"ready": bool(adapter.is_connected) and not getattr(adapter, "_managed_draining", False),
            "active": active, "unknown": unknown, **({"last_delivery_failure": failure} if failure else {})}
