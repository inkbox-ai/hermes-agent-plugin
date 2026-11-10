"""Interactive turns attached to the running native gateway."""

from __future__ import annotations

import json
import uuid

import aiohttp


class ChatAdapter:
    def __init__(self, base_url, token):
        if not base_url.startswith("http://127.0.0.1:"):
            raise ValueError("Local gateway required")
        self.base_url, self.token = base_url, token
        self.runs = {}
        self.cancelled = set()
        self.last_runs = {}

    async def request(self, method, path, data=None, headers=None):
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token}"}, timeout=aiohttp.ClientTimeout(total=15)
        ) as client:
            async with client.request(method, self.base_url + path, json=data, headers=headers) as response:
                if response.status >= 400:
                    raise RuntimeError("Native request unavailable")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    if len(body) + len(chunk) > 65536:
                        raise RuntimeError("Native request unavailable")
                    body.extend(chunk)
                return json.loads(body)

    async def cancel(self, session_id):
        self.cancelled.add(session_id)
        if run := self.runs.get(session_id):
            await self.request("POST", f"/v1/runs/{run}/stop", {})

    async def settled(self, session_id):
        run = self.last_runs.get(session_id)
        if not run:
            return False
        value = await self.request("GET", f"/v1/runs/{run}")
        terminal = value.get("status") in {"completed", "cancelled", "failed"}
        if terminal and self.runs.get(session_id) == run:
            self.runs.pop(session_id, None)
            self.cancelled.discard(session_id)
        return terminal

    async def run(self, spec, emit, ask):
        session = spec["session_id"]
        native_session = "session_" + session.replace("-", "")
        if session in self.cancelled:
            self.cancelled.discard(session)
            return {"text": "", "cancelled": True, "session_id": native_session}
        from .managed_chat import file_instruction, safe_text, failure_code

        self.last_runs.pop(session, None)
        # This adapter is browser-only. Hosted incoming/scheduled approval
        # policy belongs to the native owner; browser requests must still
        # suspend and use ask() below, never inherit a global autoapproval.
        accepted = await self.request(
            "POST",
            "/v1/runs",
            {"input": spec["prompt"] + file_instruction(spec["run_id"]), "session_id": native_session,
             **({"model": spec["model"]} if spec.get("model") else {})},
            {"Idempotency-Key": spec["run_id"]},
        )
        run = accepted["run_id"]
        self.runs[session] = run
        self.last_runs[session] = run
        if session in self.cancelled:
            await self.cancel(session)
        terminal = False
        try:
            async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=30),
            ) as client:
                async with client.get(self.base_url + f"/v1/runs/{run}/events") as response:
                    if response.status != 200:
                        raise RuntimeError("Native events unavailable")
                    async for raw in response.content:
                        if len(raw) > 65536:
                            raise ValueError("Native event too large")
                        if not raw.startswith(b"data: "):
                            continue
                        event = json.loads(raw[6:])
                        kind = event.get("event")
                        if kind == "replay.truncated":
                            raise RuntimeError("Native history unavailable")
                        if kind == "message.delta":
                            emit("message.delta", {"text": str(event.get("delta") or "")})
                        elif kind in {"tool.started", "tool.completed"}:
                            call_id = str(event.get("tool_call_id") or "")
                            if call_id:
                                emit(
                                    kind,
                                    {
                                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, spec["run_id"] + call_id)),
                                        "title": "Call tool",
                                        "detail": safe_text(event.get("preview") or "")[:4096],
                                        "status": "running"
                                        if kind.endswith("started")
                                        else "failed"
                                        if event.get("error")
                                        else "completed",
                                    },
                                )
                        elif kind in {"approval.request", "clarify.request"}:
                            native_id = event.get("prompt_id") or event.get("request_id")
                            if not isinstance(native_id, str):
                                raise ValueError("Interaction identity missing")
                            if kind == "approval.request":
                                answer = await ask(
                                    {
                                        "kind": "approval",
                                        "title": "Permission requested",
                                        "effective_scope": "operation",
                                        "detail": safe_text(
                                            event.get("command")
                                            or event.get("description")
                                            or "Review the requested permission"
                                        )[:2048],
                                        "decisions": (
                                            ["approve_once"] if "once" in event.get("choices", ["once", "deny"]) else []
                                        )
                                        + ["deny"],
                                    }
                                )
                                if answer.get("cancelled") or session in self.cancelled:
                                    continue
                                endpoint = "approval"
                                body = {"choice": "once" if answer["decision"] == "approve_once" else "deny"}
                            else:
                                choices = event.get("choices") or []
                                if (
                                    not isinstance(choices, list)
                                    or len(choices) > 32
                                    or any(not isinstance(choice, str) for choice in choices)
                                ):
                                    raise ValueError("Unsupported question choices")
                                multiple = bool(event.get("multi_select")) and bool(choices)
                                answer = await ask(
                                    {
                                        "kind": "question",
                                        "title": "Input needed",
                                        "fields": [
                                            {
                                                "id": "answer",
                                                "label": safe_text(event.get("question") or "Answer the agent")[:2048],
                                                "required": True,
                                                "type": "multi_select"
                                                if multiple
                                                else "single_select"
                                                if choices
                                                else "text",
                                                "choices": [
                                                    {"id": str(i), "label": safe_text(label)[:256]}
                                                    for i, label in enumerate(choices)
                                                ],
                                                "allow_free_text": not bool(choices),
                                            }
                                        ],
                                    }
                                )
                                if answer.get("cancelled") or session in self.cancelled:
                                    continue
                                endpoint = "clarify"
                                selected = answer["answers"]["answer"]
                                if multiple:
                                    selected = json.dumps([choices[int(index)] for index in selected])
                                elif choices:
                                    selected = choices[int(selected)]
                                body = {"answer": selected}
                            body["request_id"] = native_id
                            if event.get("execution_generation") is not None:
                                body["execution_generation"] = event["execution_generation"]
                            await self.request("POST", f"/v1/runs/{run}/{endpoint}", body)
                        elif kind in {"run.completed", "run.cancelled", "run.failed"}:
                            terminal = True
                            return {
                                "text": str(event.get("output") or ""),
                                "cancelled": kind == "run.cancelled",
                                "failed": kind == "run.failed",
                                "error_code": failure_code(event),
                                "session_id": native_session,
                            }
                        elif kind == "run.interrupted":
                            raise RuntimeError("Native outcome uncertain")
            raise RuntimeError("Native event stream ended before completion")
        finally:
            # Keep a failed stream's run identity until status proves it stopped;
            # ChatRuntime still has to cancel and settle this exact turn.
            if terminal:
                self.runs.pop(session, None)
                self.cancelled.discard(session)
