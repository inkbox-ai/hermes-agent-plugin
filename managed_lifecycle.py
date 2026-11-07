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
    receiver = getattr(adapter, "_managed_receiver", None)
    if receiver is not None:
        summary = receiver.inbox.summary()
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
    return {"ready": bool(adapter.is_connected) and not getattr(adapter, "_managed_draining", False),
            "active": active, "unknown": unknown}
