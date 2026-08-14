from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_mock():
    path = Path(__file__).parent / "live" / "mock_openai.py"
    spec = importlib.util.spec_from_file_location("live_mock_openai", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock = _load_mock()


def _request(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def _with_a2a_tools(req: dict[str, Any]) -> dict[str, Any]:
    req["tools"] = [{
        "type": "function",
        "function": {"name": "inkbox_a2a_complete", "parameters": {"type": "object"}},
    }]
    return req


def _with_tool_bridge(req: dict[str, Any]) -> dict[str, Any]:
    req["tools"] = [{
        "type": "function",
        "function": {"name": "tool_call", "parameters": {"type": "object"}},
    }]
    return req


def test_responses_stream_has_typed_output_and_terminal_events() -> None:
    response = {
        "text": "REPLY_OK smoke-012345",
    }

    events = mock._responses_events(response, "mock-model")

    assert [event["type"] for event in events] == [
        "response.output_text.delta",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[0]["delta"] == "REPLY_OK smoke-012345"
    completed = events[-1]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == response["text"]


def test_responses_tool_history_and_output_are_understood() -> None:
    req = {
        "input": [
            {"role": "user", "content": "complete the task"},
            {
                "id": "fc-1",
                "type": "function_call",
                "call_id": "call-1",
                "name": "inkbox_a2a_check",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": json.dumps(_task_result("task-1", "TASK_STATE_WORKING")),
            },
        ],
        "tools": [{"type": "function", "name": "inkbox_a2a_complete"}],
    }
    response = {
        "id": "call-2",
        "name": "inkbox_a2a_complete",
        "arguments": {"text": "done"},
    }

    assert mock._effective_tool_names(req) == ["inkbox_a2a_check"]
    assert mock._available_tool_names(req) == {"inkbox_a2a_complete"}
    assert mock._task_id(req) == "task-1"
    output = mock._responses_output(response)[0]
    assert output["type"] == "function_call"
    assert output["name"] == "inkbox_a2a_complete"


def test_responses_lazy_tool_bridge_history_uses_underlying_name() -> None:
    req = {
        "input": [{
            "id": "fc-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "tool_call",
            "arguments": json.dumps({
                "name": "inkbox_a2a_check",
                "arguments": {"taskId": "task-1"},
            }),
        }],
    }

    assert mock._tool_names(req) == ["tool_call"]
    assert mock._effective_tool_names(req) == ["inkbox_a2a_check"]


def _record_tool(
    req: dict[str, Any],
    response: dict[str, Any],
    result: dict[str, Any],
) -> None:
    call = {
        "id": f"call-{len(mock._tool_names(req)) + 1}",
        "type": "function",
        "function": {
            "name": response["name"],
            "arguments": json.dumps(response["arguments"]),
        },
    }
    req["messages"].extend([
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)},
    ])


def _task_result(task_id: str, state: str) -> dict[str, Any]:
    return {
        "ok": True,
        "task": {
            "raw": {
                "id": task_id,
                "contextId": "context-1",
                "status": {"state": state},
            }
        },
    }


def test_inbound_a2a_sequences(monkeypatch) -> None:
    single = _request(
        "[inkbox:a2a_task caller=@remote] Finish with "
        "`a2a-ci-inbound-single-012345abcdef`."
    )
    response = mock._a2a_response(single, "inbound-single")
    assert response == {
        "name": "inkbox_a2a_complete",
        "arguments": {"text": "a2a-ci-inbound-single-012345abcdef"},
    }
    monkeypatch.setenv("MOCK_A2A_SCENARIO", "inbound-single")
    assert mock._model_response(_request("auxiliary request"))["text"].startswith("REPLY_OK")
    assert mock._model_response(_with_a2a_tools(single))["name"] == "inkbox_a2a_complete"
    bridged = mock._model_response(_with_tool_bridge(_request(
        "[inkbox:a2a_task caller=@remote] Finish with "
        "`a2a-ci-inbound-single-fedcba543210`."
    )))
    assert bridged["name"] == "tool_call"
    assert bridged["arguments"]["name"] == "inkbox_a2a_complete"
    assert bridged["arguments"]["arguments"] == {
        "text": "a2a-ci-inbound-single-fedcba543210",
    }

    multi = _request("Finish with `a2a-ci-inbound-multi-012345abcdef`.")
    response = mock._a2a_response(multi, "inbound-multi")
    assert response["name"] == "inkbox_a2a_ask_caller"
    _record_tool(multi, response, {"ok": True})
    assert mock._a2a_response(multi, "inbound-multi") == {"text": "[SILENT]"}

    multi["messages"].append({
        "role": "user",
        "content": "a2a-ci-answer-012345abcdef",
    })
    response = mock._a2a_response(multi, "inbound-multi")
    assert response["name"] == "inkbox_a2a_complete"
    assert "a2a-ci-inbound-multi-012345abcdef" in response["arguments"]["text"]
    assert "a2a-ci-answer-012345abcdef" in response["arguments"]["text"]


def test_inbound_progress_sequence_waits_twice_and_returns_total(monkeypatch) -> None:
    sleeps = []
    monkeypatch.setattr(mock.time, "sleep", sleeps.append)
    req = _request(
        "Add two pairs of numbers and return "
        "`a2a-ci-inbound-progress-012345abcdef`."
    )

    response = mock._a2a_response(req, "inbound-progress")

    assert sleeps == [60.0, 60.0, 5.0]
    assert response == {
        "name": "inkbox_a2a_complete",
        "arguments": {
            "text": (
                "2 + 2 = 4; 3 + 3 = 6; 4 + 6 = 10. "
                "a2a-ci-inbound-progress-012345abcdef"
            ),
        },
    }


def test_inbound_progress_auxiliary_summary_does_not_block(monkeypatch) -> None:
    monkeypatch.setenv("MOCK_A2A_SCENARIO", "inbound-progress")
    req = _request("Write one concise progress update for the requester.")

    response = mock._model_response(req)

    assert response == {"text": mock._A2A_PROGRESS_SUMMARY}


def test_outbound_single_a2a_sequence() -> None:
    req = _request(
        "Delegate `a2a-ci-outbound-single-task-012345abcdef` to "
        "https://example.com/a2a/worker/card."
    )
    response = mock._a2a_response(req, "outbound-single")
    assert response["name"] == "inkbox_a2a_call"
    _record_tool(req, response, _task_result("task-1", "TASK_STATE_WORKING"))

    response = mock._a2a_response(req, "outbound-single")
    assert response == {
        "name": "inkbox_a2a_check",
        "arguments": {
            "cardUrl": "https://example.com/a2a/worker/card",
            "taskId": "task-1",
            "wait": True,
        },
    }
    result = _task_result("task-1", "TASK_STATE_COMPLETED")
    result["task"]["raw"]["history"] = [{
        "parts": [{"text": "a2a-ci-outbound-single-result-012345abcdef"}]
    }]
    _record_tool(req, response, result)

    response = mock._a2a_response(req, "outbound-single")
    assert response["name"] == "inkbox_a2a_complete"
    assert response["arguments"]["text"] == "a2a-ci-outbound-single-result-012345abcdef"


def test_outbound_multi_a2a_sequence() -> None:
    req = _request(
        "Delegate `a2a-ci-outbound-multi-task-012345abcdef` to "
        "https://example.com/a2a/worker/card and reply with "
        "`a2a-ci-outbound-multi-answer-012345abcdef`."
    )
    response = mock._a2a_response(req, "outbound-multi")
    _record_tool(req, response, _task_result("task-2", "TASK_STATE_WORKING"))

    response = mock._a2a_response(req, "outbound-multi")
    assert response["name"] == "inkbox_a2a_check"
    _record_tool(req, response, _task_result("task-2", "TASK_STATE_INPUT_REQUIRED"))

    response = mock._a2a_response(req, "outbound-multi")
    assert response["name"] == "inkbox_a2a_reply"
    assert response["arguments"]["text"] == "a2a-ci-outbound-multi-answer-012345abcdef"
    _record_tool(req, response, _task_result("task-2", "TASK_STATE_WORKING"))

    response = mock._a2a_response(req, "outbound-multi")
    assert response["name"] == "inkbox_a2a_check"
    result = _task_result("task-2", "TASK_STATE_COMPLETED")
    result["task"]["raw"]["history"] = [{
        "parts": [{"text": "a2a-ci-outbound-multi-result-012345abcdef"}]
    }]
    _record_tool(req, response, result)

    response = mock._a2a_response(req, "outbound-multi")
    assert response["name"] == "inkbox_a2a_complete"
    assert response["arguments"]["text"] == "a2a-ci-outbound-multi-result-012345abcdef"
