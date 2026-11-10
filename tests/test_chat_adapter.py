import asyncio
import json
import os
from unittest.mock import patch
import unittest
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestServer
from inkbox_plugin.chat_adapter import ChatAdapter


class NativeChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_hosted_browser_ask_still_uses_native_approval(self):
        # A hosted launcher may autoapprove non-chat work. Browser Ask remains
        # an explicit suspended request answered through the chat runtime.
        with patch.dict(os.environ, {"INKBOX_HOSTED_ACTIVATION_ID": "synthetic-activation"}):
            await self.test_questions_and_approvals_use_the_suspended_native_request()

    async def test_questions_and_approvals_use_the_suspended_native_request(self):
        received = []
        run_id = str(uuid4())
        session_id = str(uuid4())

        async def start(request):
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic-local-key")
            self.assertEqual(request.headers["Idempotency-Key"], run_id)
            body = await request.json()
            self.assertTrue(body["session_id"].startswith("session_"))
            self.assertEqual(body["model"], "gpt-6.1-sol")
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b'{"run_id":')
            await asyncio.sleep(0.02)
            await response.write(b'"native-run"}')
            await response.write_eof()
            return response

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
                {"session_id": session_id, "run_id": run_id, "prompt": "Example", "model": "gpt-6.1-sol"},
                lambda k, v: public.append((k, v)),
                ask,
            )
            self.assertEqual(result["text"], "Finished")
            self.assertEqual(adapter.last_runs[session_id], "native-run")
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

    async def test_response_limit_applies_to_the_complete_body(self):
        async def response(request):
            size = int(request.match_info["size"])
            body = b'{"value":"' + b'x' * (size - len(b'{"value":""}')) + b'"}'
            return web.Response(body=body, content_type="application/json")

        app = web.Application()
        app.router.add_get("/{size}", response)
        async with TestServer(app) as server:
            adapter = ChatAdapter(str(server.make_url("")), "synthetic-local-key")
            result = await adapter.request("GET", "/65536")
            self.assertEqual(len(json.dumps(result, separators=(",", ":"))), 65536)
            with self.assertRaisesRegex(RuntimeError, "Native request unavailable"):
                await adapter.request("GET", "/65537")


def test_stream_failure_cleanup_cannot_cancel_the_next_turn(tmp_path):
    import time
    from inkbox_plugin.managed_chat import ChatRuntime

    async def scenario():
        starts, stops = [], []
        turns = [str(uuid4()), str(uuid4())]
        async def start(request):
            starts.append(await request.json())
            return web.json_response({'run_id': f'native-{len(starts)}'})
        async def events(request):
            body = '' if request.match_info['run'] == 'native-1' else 'data: {"event":"run.completed","output":"done"}\n\n'
            return web.Response(text=body, content_type='text/event-stream')
        async def stop(request):
            stops.append(request.match_info['run'])
            return web.json_response({})
        async def status(request):
            return web.json_response({'status': 'completed'})
        app = web.Application()
        app.router.add_post('/v1/runs', start)
        app.router.add_get('/v1/runs/{run}/events', events)
        app.router.add_post('/v1/runs/{run}/stop', stop)
        app.router.add_get('/v1/runs/{run}', status)
        async with TestServer(app) as server:
            adapter = ChatAdapter(str(server.make_url('')), 'synthetic-local-key')
            runtime = ChatRuntime(tmp_path / 'chat.sqlite', 'owner', adapter.run, adapter.cancel, settled=adapter.settled)
            try:
                for number in (1, 2):
                    await runtime.control({'action': 'chat_start', 'spec': {
                        'run_id': turns[number - 1], 'session_id': 'conversation', 'harness': 'hermes',
                        'prompt': 'hello', 'deadline_unix_ms': int(time.time() * 1000) + 60000}})
                    await asyncio.gather(*list(runtime.tasks.values()))
                assert len(starts) == 2
                assert stops == ['native-1']
                assert runtime.journal.get(turns[0])['children_complete'] is True
                assert runtime.journal.get(turns[1])['state'] == 'succeeded'
                assert not adapter.cancelled and not adapter.runs
            finally:
                await runtime.close()
    asyncio.run(scenario())
