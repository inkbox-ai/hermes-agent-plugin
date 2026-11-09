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
        completed = result["receipt"]["finished_unix_ms"]
        self.assertGreater(completed, 0)
        self.assertEqual(runtime.journal.last_completed_unix_ms(), completed)
        runtime.journal.activity()
        self.assertEqual(runtime.journal.last_completed_unix_ms(), completed)

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
    path = tmp_path / "chat.sqlite"
    journal = Journal(path, "owner")
    value = spec()
    journal.accept(value)
    journal.close()
    journal = Journal(path, "owner")
    assert journal.events(value["run_id"], 0, 32)[-1]["payload"]["status"] == "outcome_unknown"
    try:
        journal.accept(spec("second"))
    except ValueError:
        pass
    else:
        raise AssertionError("An unfenced owner may still be executing")
    journal.accept(spec("other", "different"))
    journal.close()


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_answer_cannot_win_after_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            entered = asyncio.Event()
            answers = []

            async def run(value, emit, ask):
                entered.set()
                answers.append(
                    await ask(
                        {
                            "kind": "approval",
                            "title": "Run",
                            "effective_scope": "operation",
                            "decisions": ["approve_once", "deny"],
                        }
                    )
                )
                return {"text": ""}

            async def cancel(_):
                pass

            runtime = ChatRuntime(Path(directory) / "chat.sqlite", "owner", run, cancel)
            await runtime.control({"action": "chat_start", "spec": spec()})
            await entered.wait()
            card = runtime.journal.get("turn")["requests"][0]
            await runtime.control(
                {
                    "action": "chat_respond",
                    "run_id": "turn",
                    "request_id": card["id"],
                    "version": 1,
                    "response": {"kind": "approval", "decision": "approve_once"},
                }
            )
            await runtime.control({"action": "chat_cancel", "run_id": "turn"})
            await asyncio.gather(*list(runtime.tasks.values()))
            self.assertEqual(answers, [{"cancelled": True}])
            self.assertEqual(runtime.journal.get("turn")["state"], "cancelled")
            self.assertTrue(runtime.journal.get("turn")["children_complete"])
            self.assertTrue(runtime.journal.accept(spec("next")))
            await runtime.close()

    async def test_large_output_is_bounded_and_does_not_pin_conversation(self):
        with tempfile.TemporaryDirectory() as directory:

            async def run(value, emit, ask):
                for _ in range(40):
                    emit("message.delta", {"text": "a" * 2048})
                emit("tool.completed", {"id": "tool", "detail": "b" * 20000})
                return {"text": "a" * 81920}

            async def cancel(_):
                pass

            runtime = ChatRuntime(Path(directory) / "chat.sqlite", "owner", run, cancel)
            await runtime.control({"action": "chat_start", "spec": spec()})
            await asyncio.gather(*list(runtime.tasks.values()))
            receipt = runtime.journal.get("turn")
            self.assertEqual(receipt["state"], "failed")
            self.assertEqual(receipt["error_code"], "output_limit")
            self.assertTrue(receipt["children_complete"])
            self.assertTrue(runtime.journal.accept(spec("next")))
            await runtime.close()

    async def test_late_native_completion_releases_unknown_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            stopped = False

            async def run(value, emit, ask):
                raise RuntimeError("transport lost")

            async def cancel(_):
                pass

            async def settled(_):
                return stopped

            runtime = ChatRuntime(Path(directory) / "chat.sqlite", "owner", run, cancel, settled)
            await runtime.control({"action": "chat_start", "spec": spec()})
            await asyncio.gather(*list(runtime.tasks.values()))
            self.assertFalse(runtime.journal.get("turn")["children_complete"])
            stopped = True
            result = await runtime.control({"action": "chat_events", "run_id": "turn", "after": 0})
            self.assertEqual(result["receipt"]["state"], "outcome_unknown")
            self.assertTrue(result["receipt"]["children_complete"])
            self.assertTrue(runtime.journal.accept(spec("next")))
            await runtime.close()

    async def test_expiry_with_native_stop_acknowledgement_releases_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            stopped = asyncio.Event()

            async def run(value, emit, ask):
                answer = await ask(
                    {
                        "kind": "approval",
                        "title": "Run command",
                        "effective_scope": "operation",
                        "decisions": ["approve_once", "deny"],
                    }
                )
                self.assertEqual(answer, {"cancelled": True})
                await stopped.wait()
                return {"text": "", "cancelled": True}

            async def cancel(_):
                stopped.set()

            runtime = ChatRuntime(Path(directory) / "chat.sqlite", "owner", run, cancel)
            value = spec()
            value["deadline_unix_ms"] = int(time.time() * 1000) + 50
            await runtime.control({"action": "chat_start", "spec": value})
            await asyncio.gather(*list(runtime.tasks.values()))
            receipt = runtime.journal.get("turn")
            self.assertEqual(receipt["state"], "cancelled")
            self.assertTrue(receipt["children_complete"])
            self.assertTrue(runtime.journal.accept(spec("next")))
            await runtime.close()

    async def test_known_model_failure_has_key_recovery_action(self):
        with tempfile.TemporaryDirectory() as directory:

            async def run(value, emit, ask):
                return {"failed": True, "error_code": "model_key_invalid", "text": ""}

            async def cancel(_):
                pass

            runtime = ChatRuntime(Path(directory) / "chat.sqlite", "owner", run, cancel)
            await runtime.control({"action": "chat_start", "spec": spec()})
            await asyncio.gather(*list(runtime.tasks.values()))
            events = runtime.journal.events("turn", 0)
            blocker = next(e["payload"]["blocker"] for e in events if e["payload"].get("blocker"))
            self.assertEqual(blocker["code"], "model_key_invalid")
            self.assertIn("manage_model_key", blocker["actions"])
            await runtime.close()


def test_model_failure_classification_requires_native_error_evidence():
    from inkbox_plugin.managed_chat import failure_code

    assert failure_code({"error": "Error code: 401 - {'error': {'code': 'model_key_invalid'}}"}) == "model_key_invalid"
    assert failure_code({"error": "An unrelated message mentioning model_key_invalid"}) is None
