"""Exercise the local gateway with synthetic model execution."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
from uuid import uuid4


async def main():
    root = Path(tempfile.mkdtemp(prefix="chat-gateway-"))
    os.environ.update(
        HOME=str(root),
        HERMES_HOME=str(root / "hermes"),
        OPENAI_API_KEY="synthetic-test-key",
        OPENAI_BASE_URL="http://127.0.0.1:1/v1",
        HERMES_EXEC_ASK="1",
    )
    (root / "hermes").mkdir()
    (root / "hermes/config.yaml").write_text("model:\n  provider: openai-api\n  default: gpt-5.4\n")
    package = types.ModuleType("inkbox_plugin")
    package.__path__ = [str(Path(__file__).resolve().parents[2])]
    sys.modules["inkbox_plugin"] = package
    connect = socket.socket.connect

    def local_connect(self, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("External network disabled")
        return connect(self, address)

    socket.socket.connect = local_connect
    from run_agent import AIAgent

    calls = []

    def conversation(self, user_message, **kwargs):
        calls.append(user_message)
        if user_message.startswith("approval"):
            from tools.approval import request_tool_approval

            result = request_tool_approval("terminal", "Synthetic permission request")
        else:
            from tools.clarify_tool import clarify_tool

            result = clarify_tool(
                [
                    {"question": "Which format?", "choices": ["CSV", "JSON"]},
                    {"question": "Which columns?", "choices": ["Date", "Amount"], "multi_select": True},
                    {"question": "Report title?"},
                ],
                callback=self.clarify_callback,
            )
        return {"final_response": json.dumps(result), "completed": True}

    AIAgent.run_conversation = conversation
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.runtime_ownership import process_ownership
    from gateway.run_runtime import initialize_gateway_runtime, start_gateway_runtime_api, publish_gateway_runtime_ready
    from gateway.run_bootstrap import _start_gateway_start_control_socket
    from inkbox_plugin.chat_adapter import ChatAdapter

    token = "synthetic-local-gateway"
    process_ownership.reserve([root / "hermes"])
    runner = GatewayRunner(
        GatewayConfig(
            platforms={
                Platform.API_SERVER: PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "key": token})
            },
            multiplex_profiles=False,
        )
    )
    control = None
    try:
        await initialize_gateway_runtime(runner)
        control = await _start_gateway_start_control_socket(runner)
        assert control is not None
        await start_gateway_runtime_api(runner)
        assert await runner.start()
        publish_gateway_runtime_ready(runner)
        native = runner.adapters[Platform.API_SERVER]
        port = native._site._server.sockets[0].getsockname()[1]
        adapter = ChatAdapter(f"http://127.0.0.1:{port}", token)
        cards = []

        async def ask(card):
            cards.append(card)
            if card["kind"] == "approval":
                assert card["effective_scope"] == "operation"
                return {"kind": "approval", "decision": "approve_once" if prompt == "approval-allow" else "deny"}
            field = card["fields"][0]
            answer = {"single_select": "1", "multi_select": ["0", "1"], "text": "Quarterly report"}[field["type"]]
            return {"kind": "question", "answers": {field["id"]: answer}}

        for prompt in ["approval-deny", "approval-allow", "questions"]:
            spec = {"run_id": str(uuid4()), "session_id": str(uuid4()), "prompt": prompt}
            result = await asyncio.wait_for(adapter.run(spec, lambda *_: None, ask), 30)
            assert not result["failed"] and not result["cancelled"], result
            assert result["text"], result
            if prompt == "questions":
                assert "JSON" in result["text"] and "Quarterly report" in result["text"], result
            else:
                assert json.loads(result["text"])["approved"] == (prompt == "approval-allow"), result
        from inkbox_plugin.managed_chat import file_instruction

        body = {
            "input": spec["prompt"] + file_instruction(spec["run_id"]),
            "session_id": "session_" + spec["session_id"].replace("-", ""),
        }
        replay = await adapter.request("POST", "/v1/runs", body, {"Idempotency-Key": spec["run_id"]})
        repeated = await adapter.request("POST", "/v1/runs", body, {"Idempotency-Key": spec["run_id"]})
        assert replay["run_id"] == repeated["run_id"]
        assert len(calls) == 3, calls
        assert [c["kind"] for c in cards] == ["approval", "approval", "question", "question", "question"], cards
        assert [c["fields"][0]["type"] for c in cards[2:]] == ["single_select", "multi_select", "text"], cards
        print("Native chat approval and question contracts passed")
    finally:
        await runner.stop()
        if control is not None:
            await control.stop()
        process_ownership.close()
        from hermes_logging import _reset_queued_handlers

        _reset_queued_handlers()
        import shutil

        shutil.rmtree(root)


if __name__ == "__main__":
    asyncio.run(main())
