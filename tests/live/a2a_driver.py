#!/usr/bin/env python3
"""Drive one real A2A scenario between the plugin and a remote identity."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from inkbox import Inkbox

STOPPED_WIRE_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
}


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _identity(client: Inkbox):
    mailboxes = client.mailboxes.list()
    if len(mailboxes) != 1:
        raise RuntimeError("Live A2A credentials must resolve to exactly one mailbox")
    handle = mailboxes[0].email_address.split("@", 1)[0]
    return client.get_identity(handle), handle


def _parts_text(parts: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(part["text"])
        for part in parts
        if isinstance(part, dict) and part.get("text") is not None
    )


def _wire_history_text(task: Any) -> str:
    return "\n".join(
        _parts_text(message.get("parts", []))
        for message in task.raw.get("history", [])
        if isinstance(message, dict)
    )


def _rest_history_text(task: Any) -> str:
    return "\n".join(_parts_text(message.parts) for message in task.messages)


def _wait_protocol_task(
    a2a: Any,
    target: Any,
    task_id: str,
    *,
    expected: set[str],
    timeout: float,
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = a2a.get_task(target, task_id, history_length=50)
        state = _enum_value(task.state)
        if state in expected:
            return task
        if state in STOPPED_WIRE_STATES:
            raise AssertionError(f"A2A task stopped in unexpected state {state}")
        time.sleep(1)
    raise TimeoutError(f"A2A task did not reach {sorted(expected)} before timeout")


def _send_task(a2a: Any, target: Any, text: str) -> Any:
    result = a2a.send(target, text=text, message_id=str(uuid.uuid4()))
    if result.kind != "task" or result.task is None:
        raise AssertionError("A2A SendMessage did not return a task")
    return result.task


def _find_inbound_task(identity: Any, token: str, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = identity.a2a_tasks(direction="inbound", q=token, limit=10)
        for item in page.items:
            task = identity.a2a_task(item.id)
            if token in _rest_history_text(task):
                return task
        time.sleep(1)
    raise TimeoutError("Plugin did not create the expected outbound A2A task")


def _wait_for_caller_message(
    identity: Any,
    task_id: str,
    token: str,
    timeout: float,
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = identity.a2a_task(task_id)
        for message in task.messages:
            if _enum_value(message.role) == "caller" and token in _parts_text(message.parts):
                return task
        state = _enum_value(task.state)
        if state in {"completed", "failed", "canceled", "rejected"}:
            raise AssertionError(
                f"Outbound A2A task stopped before caller follow-up: {state}"
            )
        time.sleep(1)
    raise TimeoutError("Plugin did not answer the worker's input request")


def _assert_outer_is_open(a2a: Any, target: Any, task_id: str) -> None:
    state = _enum_value(a2a.get_task(target, task_id).state)
    if state in STOPPED_WIRE_STATES:
        raise AssertionError("Plugin completed the outer task before its delegation")


def _cancel_if_open(a2a: Any, target: Any, task_id: str) -> None:
    try:
        state = _enum_value(a2a.get_task(target, task_id).state)
        if state not in STOPPED_WIRE_STATES:
            a2a.cancel(target, task_id)
    except Exception:
        pass


def _fail_if_open(identity: Any, task_id: str) -> None:
    try:
        task = identity.a2a_task(task_id)
        if _enum_value(task.state) not in {
            "completed",
            "failed",
            "canceled",
            "rejected",
        }:
            identity.a2a_reply(
                task_id,
                intent="fail",
                text="Automated A2A test cleanup.",
            )
    except Exception:
        pass


def _inbound_single(a2a: Any, target: Any, timeout: float, run: str) -> None:
    completion = f"a2a-ci-inbound-single-{run}"
    task = _send_task(
        a2a,
        target,
        "Complete this A2A task without requesting input. "
        f"The final answer must contain `{completion}`.",
    )
    try:
        final = _wait_protocol_task(
            a2a,
            target,
            task.id,
            expected={"TASK_STATE_COMPLETED"},
            timeout=timeout,
        )
        if completion not in _wire_history_text(final):
            raise AssertionError("Inbound single-turn completion token is missing")
    finally:
        _cancel_if_open(a2a, target, task.id)


def _inbound_multi(a2a: Any, target: Any, timeout: float, run: str) -> None:
    answer = f"a2a-ci-answer-{run}"
    completion = f"a2a-ci-inbound-multi-{run}"
    task = _send_task(
        a2a,
        target,
        "First call inkbox_a2a_ask_caller to request the access code. "
        "Do not complete or fail the task before the caller responds. "
        "After the caller replies, call inkbox_a2a_complete and include both "
        f"the supplied code and `{completion}` in the final answer.",
    )
    try:
        waiting = _wait_protocol_task(
            a2a,
            target,
            task.id,
            expected={"TASK_STATE_INPUT_REQUIRED"},
            timeout=timeout,
        )
        a2a.send(
            target,
            text=answer,
            message_id=str(uuid.uuid4()),
            context_id=waiting.context_id,
            task_id=waiting.id,
        )
        final = _wait_protocol_task(
            a2a,
            target,
            task.id,
            expected={"TASK_STATE_COMPLETED"},
            timeout=timeout,
        )
        history = _wire_history_text(final)
        if answer not in history or completion not in history:
            raise AssertionError("Inbound multi-turn history is incomplete")
    finally:
        _cancel_if_open(a2a, target, task.id)


def _outbound_single(
    a2a: Any,
    target: Any,
    remote_identity: Any,
    remote_card_url: str,
    timeout: float,
    run: str,
) -> None:
    inner_token = f"a2a-ci-outbound-single-task-{run}"
    worker_result = f"a2a-ci-outbound-single-result-{run}"
    outer = _send_task(
        a2a,
        target,
        "Delegate a new task with inkbox_a2a_call to the Agent Card at "
        f"{remote_card_url}. The delegated task text must contain "
        f"`{inner_token}`. Wait for it with inkbox_a2a_check and do not "
        "complete this outer task first. After the worker completes, call "
        "inkbox_a2a_complete and include the worker's response in the answer.",
    )
    inner = None
    try:
        inner = _find_inbound_task(remote_identity, inner_token, timeout)
        _assert_outer_is_open(a2a, target, outer.id)
        remote_identity.a2a_reply(
            inner.id,
            intent="complete",
            text=worker_result,
        )
        final = _wait_protocol_task(
            a2a,
            target,
            outer.id,
            expected={"TASK_STATE_COMPLETED"},
            timeout=timeout,
        )
        if worker_result not in _wire_history_text(final):
            raise AssertionError("Outbound single-turn worker result is missing")
    finally:
        if inner is not None:
            _fail_if_open(remote_identity, inner.id)
        _cancel_if_open(a2a, target, outer.id)


def _outbound_multi(
    a2a: Any,
    target: Any,
    remote_identity: Any,
    remote_card_url: str,
    timeout: float,
    run: str,
) -> None:
    inner_token = f"a2a-ci-outbound-multi-task-{run}"
    answer = f"a2a-ci-outbound-multi-answer-{run}"
    worker_result = f"a2a-ci-outbound-multi-result-{run}"
    outer = _send_task(
        a2a,
        target,
        "Delegate a new task with inkbox_a2a_call to the Agent Card at "
        f"{remote_card_url}. The delegated task text must contain "
        f"`{inner_token}`. The worker will request input; reply with "
        f"`{answer}` using inkbox_a2a_reply, then wait again with "
        "inkbox_a2a_check. Do not complete this outer task before the worker. "
        "Finally call inkbox_a2a_complete with the worker's response.",
    )
    inner = None
    try:
        inner = _find_inbound_task(remote_identity, inner_token, timeout)
        _assert_outer_is_open(a2a, target, outer.id)
        remote_identity.a2a_reply(
            inner.id,
            intent="ask_caller",
            text="Provide the requested verification value.",
        )
        _wait_for_caller_message(remote_identity, inner.id, answer, timeout)
        _assert_outer_is_open(a2a, target, outer.id)
        remote_identity.a2a_reply(
            inner.id,
            intent="complete",
            text=worker_result,
        )
        final = _wait_protocol_task(
            a2a,
            target,
            outer.id,
            expected={"TASK_STATE_COMPLETED"},
            timeout=timeout,
        )
        if worker_result not in _wire_history_text(final):
            raise AssertionError("Outbound multi-turn worker result is missing")
    finally:
        if inner is not None:
            _fail_if_open(remote_identity, inner.id)
        _cancel_if_open(a2a, target, outer.id)


def main() -> None:
    scenario = _required_env("A2A_SCENARIO")
    timeout = float(os.environ.get("A2A_TIMEOUT_S", "240"))
    base_url = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai").rstrip("/")
    aut = Inkbox(api_key=_required_env("AUT_INKBOX_API_KEY"), base_url=base_url)
    remote = Inkbox(api_key=_required_env("REMOTE_INKBOX_API_KEY"), base_url=base_url)
    _, aut_handle = _identity(aut)
    remote_identity, remote_handle = _identity(remote)
    a2a = remote_identity.a2a_client()
    target = a2a.fetch_card(f"{base_url}/a2a/{aut_handle}/card")
    remote_card_url = f"{base_url}/a2a/{remote_handle}/card"
    run = uuid.uuid4().hex[:12]
    try:
        if scenario == "inbound-single":
            _inbound_single(a2a, target, timeout, run)
        elif scenario == "inbound-multi":
            _inbound_multi(a2a, target, timeout, run)
        elif scenario == "outbound-single":
            _outbound_single(
                a2a, target, remote_identity, remote_card_url, timeout, run
            )
        elif scenario == "outbound-multi":
            _outbound_multi(
                a2a, target, remote_identity, remote_card_url, timeout, run
            )
        else:
            raise RuntimeError(f"Unknown A2A_SCENARIO: {scenario}")
    finally:
        a2a.close()


if __name__ == "__main__":
    main()
