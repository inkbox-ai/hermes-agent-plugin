"""Deterministic OpenAI-compatible mock for live agent tests.

Hermes' ``openai-api`` provider honours ``OPENAI_BASE_URL``, so pointing it at
this server makes the agent "think" here instead of against real OpenAI: no real
key, no tokens, no flakiness, fully deterministic. We still exercise the entire
real pipeline (gateway, plugin, inbound routing, agent loop, Inkbox send +
delivery) — only the LLM brain is faked.

Channel replies contain ``REPLY_OK`` plus the inbound smoke nonce. When
``MOCK_A2A_SCENARIO`` is set, the mock instead drives the requested A2A tool
sequence from prior tool calls and results.

Run: ``python mock_openai.py [port]`` (default 8088). Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_NONCE = re.compile(r"smoke-[0-9a-f]{6,}")
_A2A_CARD_URL = re.compile(r"https://[^\s`\"\\]+/a2a/[^\s`\"\\]+/card")
_A2A_TOKEN = re.compile(r"a2a-ci-[a-z-]+-[0-9a-f]{12}")


def _reply_text(req: dict) -> str:
    m = _NONCE.search(json.dumps(req))
    tag = m.group(0) if m else "no-nonce"
    return f"REPLY_OK {tag} — automated reachability reply from the agent."


def _messages(req: dict[str, Any]) -> list[dict[str, Any]]:
    messages = req.get("messages")
    return messages if isinstance(messages, list) else []


def _tool_names(req: dict[str, Any]) -> list[str]:
    names = []
    for message in _messages(req):
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and function.get("name"):
                names.append(str(function["name"]))
    return names


def _effective_tool_names(req: dict[str, Any]) -> list[str]:
    names = []
    for message in _messages(req):
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            if name != "tool_call":
                if name:
                    names.append(name)
                continue
            try:
                arguments = json.loads(str(function.get("arguments") or "{}"))
            except ValueError:
                arguments = {}
            underlying = str(arguments.get("name") or "")
            if underlying:
                names.append(underlying)
    return names


def _available_tool_names(req: dict[str, Any]) -> set[str]:
    names = set()
    for tool in req.get("tools") or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return names


def _request_text(req: dict[str, Any]) -> str:
    return json.dumps(req, ensure_ascii=False)


def _task_id(req: dict[str, Any]) -> str:
    def visit(value: Any) -> str:
        if isinstance(value, dict):
            if value.get("id") and ("contextId" in value or "status" in value):
                return str(value["id"])
            for nested in value.values():
                found = visit(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = visit(nested)
                if found:
                    return found
        return ""

    for message in reversed(_messages(req)):
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except ValueError:
            continue
        found = visit(payload)
        if found:
            return found
    raise ValueError("A2A mock could not find the delegated task id")


def _matched(pattern: re.Pattern[str], req: dict[str, Any], label: str) -> str:
    match = pattern.search(_request_text(req))
    if match is None:
        raise ValueError(f"A2A mock could not find {label}")
    return match.group(0)


def _a2a_tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def _a2a_token(tokens: list[str], fragment: str) -> str:
    for token in reversed(tokens):
        if fragment in token:
            return token
    raise ValueError(f"A2A mock could not find token containing {fragment}")


def _a2a_response(req: dict[str, Any], scenario: str) -> dict[str, Any]:
    names = _effective_tool_names(req)
    tokens = _A2A_TOKEN.findall(_request_text(req))

    if scenario == "inbound-single":
        if not names:
            return _a2a_tool(
                "inkbox_a2a_complete",
                text=_a2a_token(tokens, "inbound-single"),
            )
        return {"text": "[SILENT]"}

    if scenario == "inbound-multi":
        answer = next((token for token in reversed(tokens) if token.startswith("a2a-ci-answer-")), "")
        if answer:
            return _a2a_tool(
                "inkbox_a2a_complete",
                text=f"{answer} {_a2a_token(tokens, 'inbound-multi')}",
            )
        if not names:
            return _a2a_tool(
                "inkbox_a2a_ask_caller",
                text="What is the access code?",
            )
        return {"text": "[SILENT]"}

    card_url = _matched(_A2A_CARD_URL, req, "Agent Card URL")
    call_count = names.count("inkbox_a2a_call")
    check_count = names.count("inkbox_a2a_check")
    reply_count = names.count("inkbox_a2a_reply")
    complete_count = names.count("inkbox_a2a_complete")

    if call_count == 0:
        return _a2a_tool(
            "inkbox_a2a_call",
            cardUrl=card_url,
            text=_a2a_token(tokens, "-task-"),
        )
    if complete_count:
        return {"text": "[SILENT]"}

    task_id = _task_id(req)
    if scenario == "outbound-single":
        if check_count == 0:
            return _a2a_tool(
                "inkbox_a2a_check",
                cardUrl=card_url,
                taskId=task_id,
                wait=True,
            )
        return _a2a_tool(
            "inkbox_a2a_complete",
            text=_a2a_token(tokens, "-result-"),
        )

    if scenario == "outbound-multi":
        if check_count == 0:
            return _a2a_tool(
                "inkbox_a2a_check",
                cardUrl=card_url,
                taskId=task_id,
                wait=True,
            )
        if reply_count == 0:
            return _a2a_tool(
                "inkbox_a2a_reply",
                cardUrl=card_url,
                taskId=task_id,
                text=_a2a_token(tokens, "-answer-"),
            )
        if check_count == 1:
            return _a2a_tool(
                "inkbox_a2a_check",
                cardUrl=card_url,
                taskId=task_id,
                wait=True,
            )
        return _a2a_tool(
            "inkbox_a2a_complete",
            text=_a2a_token(tokens, "-result-"),
        )

    raise ValueError(f"Unknown MOCK_A2A_SCENARIO: {scenario}")


def _model_response(req: dict[str, Any]) -> dict[str, Any]:
    scenario = os.environ.get("MOCK_A2A_SCENARIO", "").strip()
    available = _available_tool_names(req)
    is_a2a_turn = (
        "inkbox_a2a_complete" in available
        or "tool_call" in available
    )
    response = (
        _a2a_response(req, scenario)
        if scenario and is_a2a_turn
        else {"text": _reply_text(req)}
    )
    if (
        response.get("name", "").startswith("inkbox_")
        and response["name"] not in available
        and "tool_call" in available
    ):
        response = {
            "name": "tool_call",
            "arguments": {
                "name": response["name"],
                "arguments": response.get("arguments") or {},
            },
        }
    if response.get("name"):
        response["id"] = f"call-{len(_tool_names(req)) + 1}-{response['name']}"
    if scenario:
        tool_result = "none"
        for message in reversed(_messages(req)):
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            try:
                payload = json.loads(content) if isinstance(content, str) else content
            except ValueError:
                payload = None
            tool_result = (
                "error"
                if isinstance(payload, dict) and payload.get("error")
                else "ok"
            )
            break
        print(
            json.dumps({
                "scenario": scenario,
                "prior_tools": _effective_tool_names(req),
                "last_tool_result": tool_result,
                "response": (
                    response.get("arguments", {}).get("name")
                    if response.get("name") == "tool_call"
                    else response.get("name") or "text"
                ),
            }),
            flush=True,
        )
    return response


def _tool_call(response: dict[str, Any]) -> dict[str, Any] | None:
    name = response.get("name")
    if not name:
        return None
    return {
        "id": str(response["id"]),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(response.get("arguments") or {}),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):  # quiet
        pass

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802  (model-probe + health)
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(200, {"object": "list", "data": [
                {"id": "mock-model", "object": "model", "owned_by": "mock"},
            ]})
        else:
            self._send_json(200, {"ok": True})

    def do_POST(self):  # noqa: N802  (chat completions)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            req = {}
        try:
            response = _model_response(req)
        except Exception as exc:
            print(f"Mock response failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self._send_json(500, {"error": {"message": str(exc), "type": "mock_error"}})
            return
        text = str(response.get("text") or "")
        tool_call = _tool_call(response)
        model = req.get("model", "mock-model")
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = {"role": "assistant"}
            finish_reason = "stop"
            if tool_call:
                delta["tool_calls"] = [{"index": 0, **tool_call}]
                finish_reason = "tool_calls"
            else:
                delta["content"] = text
            chunks = [
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            message = {"role": "assistant", "content": text or None}
            finish_reason = "stop"
            if tool_call:
                message["tool_calls"] = [tool_call]
                finish_reason = "tool_calls"
            self._send_json(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
