import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from inkbox_plugin.managed_chat import ChatRuntime, Journal, validate_answer


def spec(identifier="turn", session="conversation"):
    return {
        "run_id": identifier,
        "session_id": session,
        "harness": "codex",
        "prompt": "hello",
        "deadline_unix_ms": int(time.time() * 1000) + 60000,
    }


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "chat.sqlite"

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_retry_and_ordered_events(self):
        calls = []

        async def run(value, emit, ask):
            calls.append(value)
            emit("message.delta", {"text": "hello "})
            emit("message.delta", {"text": "world"})
            return {"text": "hello world"}

        async def cancel(_):
            pass

        runtime = ChatRuntime(self.path, "owner", run, cancel)
        command = {"action": "chat_start", "spec": spec()}
        await runtime.control(command)
        await runtime.control(command)
        await asyncio.gather(*list(runtime.tasks.values()))
        await runtime.control(command)
        result = await runtime.control({"action": "chat_events", "run_id": "turn", "after": 0})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["receipt"]["state"], "succeeded")
        events = result["events"]
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual([e["payload"]["text"] for e in events if e["kind"] == "message.delta"], ["hello world"])
        await runtime.close()

    async def test_first_answer_wins_and_restart_does_not_replay(self):
        ready = asyncio.Event()

        async def run(value, emit, ask):
            ready.set()
            result = await ask(
                {
                    "kind": "approval",
                    "title": "Run command",
                    "effective_scope": "operation",
                    "decisions": ["approve_once", "deny"],
                }
            )
            return {"text": result["decision"]}

        async def cancel(_):
            pass

        runtime = ChatRuntime(self.path, "owner", run, cancel)
        value = spec()
        await runtime.control({"action": "chat_start", "spec": value})
        await ready.wait()
        receipt = runtime.journal.get("turn")
        request = receipt["requests"][0]
        response = {
            "action": "chat_respond",
            "run_id": "turn",
            "request_id": request["id"],
            "version": 1,
            "response": {"kind": "approval", "decision": "approve_once"},
        }
        await runtime.control(response)
        await runtime.control(response)
        with self.assertRaises(ValueError):
            await runtime.control({**response, "response": {"kind": "approval", "decision": "deny"}})
        await asyncio.gather(*list(runtime.tasks.values()))
        await runtime.close()
        journal = Journal(self.path, "new-owner")
        self.assertFalse(journal.accept(value))
        self.assertEqual(journal.get("turn")["state"], "succeeded")
        journal.close()

    async def test_interrupted_acceptance_is_unknown_not_replayed(self):
        journal = Journal(self.path, "owner")
        value = spec()
        journal.accept(value)
        journal.close()
        journal = Journal(self.path, "new-owner")
        self.assertFalse(journal.accept(value))
        self.assertEqual(journal.get("turn")["state"], "outcome_unknown")
        self.assertTrue(journal.get("turn")["children_complete"])
        journal.close()

    async def test_scope_busy_and_question_validation(self):
        journal = Journal(self.path, "owner")
        journal.accept(spec())
        with self.assertRaises(ValueError):
            journal.accept(spec("second"))
        journal.accept(spec("other", "different"))
        request = {
            "kind": "question",
            "fields": [
                {"id": "format", "type": "single_select", "required": True, "choices": [{"id": "csv", "label": "CSV"}]}
            ],
        }
        validate_answer(request, {"kind": "question", "answers": {"format": "csv"}})
        with self.assertRaises(ValueError):
            validate_answer(request, {"kind": "question", "answers": {"format": "bad"}})
        journal.close()

    async def test_cancel_before_dispatch_never_runs(self):
        calls = []

        async def run(value, emit, ask):
            calls.append(value)
            return {"text": "unexpected"}

        async def cancel(_):
            pass

        runtime = ChatRuntime(self.path, "owner", run, cancel)
        await runtime.control({"action": "chat_start", "spec": spec()})
        await runtime.control({"action": "chat_cancel", "run_id": "turn"})
        await asyncio.gather(*list(runtime.tasks.values()))
        self.assertEqual(calls, [])
        self.assertEqual(runtime.journal.get("turn")["state"], "cancelled")
        self.assertTrue(runtime.journal.get("turn")["children_complete"])
        await runtime.close()

    async def test_delta_flushes_while_tool_is_waiting_and_failure_is_terminal(self):
        emitted = asyncio.Event()
        finish = asyncio.Event()

        async def run(value, emit, ask):
            emit("message.delta", {"text": "Working"})
            emitted.set()
            await finish.wait()
            return {"text": "Working", "failed": True}

        async def cancel(_):
            finish.set()

        runtime = ChatRuntime(self.path, "owner", run, cancel)
        await runtime.control({"action": "chat_start", "spec": spec()})
        await emitted.wait()
        await asyncio.sleep(0.15)
        self.assertTrue(any(e["kind"] == "message.delta" for e in runtime.journal.events("turn", 0)))
        finish.set()
        await asyncio.gather(*list(runtime.tasks.values()))
        self.assertEqual(runtime.journal.get("turn")["state"], "failed")
        await runtime.close()

    async def test_split_credentials_never_reach_events(self):
        from unittest.mock import patch

        secret = "example-private-credential-value"

        async def run(value, emit, ask):
            for part in ("prefix " + secret[:8], secret[8:17], secret[17:] + " suffix"):
                emit("message.delta", {"text": part})
                await asyncio.sleep(0.12)
            return {"text": "prefix " + secret + " suffix"}

        async def cancel(_):
            pass

        with patch.dict("os.environ", {"EXAMPLE_API_KEY": secret}):
            runtime = ChatRuntime(self.path, "owner", run, cancel)
            await runtime.control({"action": "chat_start", "spec": spec()})
            await asyncio.gather(*list(runtime.tasks.values()))
            events = runtime.journal.events("turn", 0)
            text = "".join(e["payload"]["text"] for e in events if e["kind"] == "message.delta")
            self.assertEqual(text, "prefix [redacted] suffix")
            self.assertNotIn(secret, str(events))
            await runtime.close()

    async def test_expired_question_never_authorizes_and_cancels(self):
        entered = asyncio.Event()
        cancelled = []

        async def run(value, emit, ask):
            entered.set()
            await ask(
                {
                    "kind": "question",
                    "title": "Format",
                    "fields": [{"id": "format", "label": "Format", "required": True, "type": "text"}],
                }
            )
            self.fail("expired answer resumed native work")

        async def cancel(session):
            cancelled.append(session)

        runtime = ChatRuntime(self.path, "owner", run, cancel)
        value = spec()
        value["deadline_unix_ms"] = int(time.time() * 1000) + 100
        await runtime.control({"action": "chat_start", "spec": value})
        await entered.wait()
        await asyncio.gather(*list(runtime.tasks.values()))
        receipt = runtime.journal.get("turn")
        self.assertEqual(receipt["state"], "outcome_unknown")
        self.assertTrue(cancelled)
        self.assertIn(receipt["requests"][0]["payload"]["status"], {"expired", "cancelled"})
        await runtime.close()


def test_restart_emits_terminal_update_and_keeps_unfenced_conversation_busy(tmp_path):
    path = tmp_path / 'chat.sqlite'
    journal = Journal(path, 'owner')
    value = spec()
    journal.accept(value)
    journal.close()
    journal = Journal(path, 'owner')
    assert journal.events(value['run_id'], 0, 32)[-1]['payload']['status'] == 'outcome_unknown'
    try:
        journal.accept(spec('second'))
    except ValueError:
        pass
    else:
        raise AssertionError('An unfenced owner may still be executing')
    journal.accept(spec('other', 'different'))
    journal.close()
