import json
import unittest
from uuid import uuid4

from aiohttp import web
from inkbox_plugin.chat_adapter import ChatAdapter


class NativeChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_questions_and_approvals_use_the_suspended_native_request(self):
        received = []
        run_id = str(uuid4())

        async def start(request):
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic-local-key")
            self.assertEqual(request.headers["Idempotency-Key"], run_id)
            body = await request.json()
            self.assertTrue(body["session_id"].startswith("session_"))
            self.assertEqual(body["model"], "gpt-6.1-sol")
            return web.json_response({"run_id": "native-run"})

        async def answer(request):
            received.append((request.match_info["kind"], await request.json()))
            return web.json_response({"resolved": 1})

        async def events(request):
            stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await stream.prepare(request)
            for event in [
                {"event": "message.delta", "delta": "Working"},
                {"event": "reasoning.available", "text": "private reasoning"},
                {"event": "tool.started", "tool_call_id": "call"},
                {
                    "event": "approval.request",
                    "prompt_id": "approval",
                    "execution_generation": 2,
                    "command": "example command",
                    "choices": ["deny"],
                },
                {
                    "event": "clarify.request",
                    "prompt_id": "question",
                    "execution_generation": 2,
                    "question": "Which format?",
                    "choices": ["CSV", "JSON"],
                    "multi_select": True,
                },
                {"event": "tool.completed", "tool_call_id": "call", "error": True},
                {"event": "run.completed", "output": "Finished"},
            ]:
                await stream.write(("data: " + json.dumps(event) + "\n\n").encode())
            return stream

        app = web.Application()
        app.router.add_post("/v1/runs", start)
        app.router.add_get("/v1/runs/native-run/events", events)
        app.router.add_post("/v1/runs/native-run/{kind}", answer)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        adapter = ChatAdapter(f"http://127.0.0.1:{port}", "synthetic-local-key")
        public = []

        async def ask(value):
            if value["kind"] == "approval":
                self.assertEqual(value["decisions"], ["deny"])
                return {"kind": "approval", "decision": "deny"}
            self.assertEqual(value["fields"][0]["type"], "multi_select")
            return {"kind": "question", "answers": {"answer": ["0"]}}

        try:
            result = await adapter.run(
                {"session_id": str(uuid4()), "run_id": run_id, "prompt": "Example", "model": "gpt-6.1-sol"},
                lambda k, v: public.append((k, v)),
                ask,
            )
            self.assertEqual(result["text"], "Finished")
            self.assertEqual(
                received,
                [
                    ("approval", {"choice": "deny", "request_id": "approval", "execution_generation": 2}),
                    ("clarify", {"answer": '["CSV"]', "request_id": "question", "execution_generation": 2}),
                ],
            )
            self.assertNotIn("private reasoning", str(public))
            self.assertEqual(public[-1][1]["status"], "failed")
        finally:
            await runner.cleanup()
