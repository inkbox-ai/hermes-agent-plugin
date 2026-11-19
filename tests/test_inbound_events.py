import asyncio
import base64
import hashlib
import hmac
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin import inbound_events
from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.inbound_events import InboundEvents


def _data(event_id="inbev_1", handler=None, delivery_id="inbdel_1", **request):
    return {
        "event_id": event_id,
        "delivery_id": delivery_id,
        "received_at": "2026-01-01T00:00:00Z",
        "source": {"id": "inbsrc_1", "name": "payments", "instructions": "Log each charge."},
        "handler": handler or {"type": "agent"},
        "signature_verified": True,
        "request": {
            "method": "POST", "query": {}, "headers": {"content-type": "application/json"},
            "content_type": "application/json", "body": '{"amount": 5}', "body_encoding": "utf8",
            **request,
        },
    }


class _Request:
    url = "http://127.0.0.1:8080/webhook"

    def __init__(self, body, headers):
        self._body = body
        self.headers = headers

    async def read(self):
        return self._body


def _signed(envelope, key="secret"):
    body = json.dumps(envelope).encode()
    ts, rid = "1700000000", "req-1"
    return _Request(body, {
        "X-Inkbox-Request-Id": rid, "X-Inkbox-Timestamp": ts,
        "X-Inkbox-Signature": "sha256=" + hmac.new(key.encode(), f"{rid}.{ts}.".encode() + body, hashlib.sha256).hexdigest(),
    })


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(adapter_mod, "web", types.SimpleNamespace(
        Response=lambda **kwargs: types.SimpleNamespace(**kwargs)))
    instance = object.__new__(InkboxAdapter)
    instance._require_signature = True
    instance._signing_key = "secret"
    instance._local_delivery = True
    instance._identity_handle = "agent"
    instance.platform = "inkbox"
    instance._enqueued = []

    async def capture(event):
        instance._enqueued.append(event)

    instance._enqueue = capture
    return instance


def test_local_delivery_flag(monkeypatch):
    monkeypatch.setenv("INKBOX_DELIVERY_MODE", "local")
    assert inbound_events.local_delivery() is True
    monkeypatch.setenv("INKBOX_DELIVERY_MODE", "")
    assert inbound_events.local_delivery() is False


def test_local_connect_opens_no_tunnel_and_registers_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    instance = object.__new__(InkboxAdapter)
    instance._local_delivery = True
    instance._voice_stack_invalid_value = ""
    instance._api_key = "key"
    instance._identity_handle = "agent"
    instance._identity_id = "id-1"
    instance._require_signature = True
    instance._signing_key = "secret"
    instance._host = "127.0.0.1"
    instance._port = 9_877
    instance._webhook_path = "/webhook"
    instance._ws_path = "/ws"
    instance._public_url_override = ""
    instance._public_url = None
    instance._base_url = "https://api.example"
    instance._a2a_closing = False
    instance._acquire_platform_lock = lambda **_: True
    instance._release_platform_lock = lambda: None
    instance._cleanup = AsyncMock()
    instance._start_companion = AsyncMock()
    instance._catch_up_hosted_call_completions = AsyncMock()
    instance._catch_up_a2a_tasks = AsyncMock()
    instance._ordinary_voice_note_receiver = lambda: types.SimpleNamespace(start=lambda: None)
    instance._mark_connected = lambda: None
    instance._patch_identity_objects = lambda: pytest.fail("registered webhooks")
    instance._provision_inkbox_tunnel = AsyncMock(side_effect=AssertionError("opened tunnel"))
    sizes, hosts = [], []

    class Runner:
        async def setup(self):
            pass

    class Site:
        async def start(self):
            pass

    def application(**kwargs):
        sizes.append(kwargs.get("client_max_size"))
        return types.SimpleNamespace(router=types.SimpleNamespace(add_get=lambda *_: None, add_post=lambda *_: None))

    monkeypatch.setattr(adapter_mod, "check_inkbox_requirements", lambda: True)
    monkeypatch.setattr(adapter_mod, "Inkbox", lambda **_: object())
    monkeypatch.setattr(adapter_mod.web, "Application", application)
    monkeypatch.setattr(adapter_mod.web, "AppRunner", lambda _app: Runner())
    monkeypatch.setattr(adapter_mod.web, "TCPSite", lambda _r, host, _p: hosts.append(host) or Site())

    async def run():
        assert await instance.connect() is True
        assert instance._inbound_events.task is not None
        await instance._inbound_events.close()

    asyncio.run(run())
    assert sizes == [2 * 1024 * 1024]
    assert hosts == ["127.0.0.1"]
    assert instance._public_url is None


def test_init_forces_loopback_in_local_mode(monkeypatch):
    monkeypatch.setenv("INKBOX_DELIVERY_MODE", "local")
    monkeypatch.setenv("INKBOX_HOST", "0.0.0.0")
    instance = InkboxAdapter(types.SimpleNamespace(extra={}, enabled=True))
    assert instance._local_delivery is True
    assert instance._host == "127.0.0.1"


def test_ignored_outside_local_mode(adapter):
    adapter._local_delivery = False
    response = asyncio.run(adapter._handle_webhook(_signed({"event_type": "inbound.received", "data": _data()})))
    assert (response.status, response.text) == (200, "ignored")


def test_rejects_bad_signature(adapter, tmp_path):
    adapter._inbound_events = InboundEvents(tmp_path / "q.sqlite3", AsyncMock(), lambda *_: None)
    request = _signed({"event_type": "inbound.received", "data": _data()}, key="wrong")
    assert asyncio.run(adapter._handle_webhook(request)).status == 401
    unsigned = _Request(json.dumps({"event_type": "inbound.received", "data": _data()}).encode(), {})
    assert asyncio.run(adapter._handle_webhook(unsigned)).status == 401


def test_accepts_once_and_dedupes(adapter, tmp_path):
    async def run():
        adapter._inbound_events = InboundEvents(tmp_path / "q.sqlite3", AsyncMock(), lambda *_: None)
        envelope = {"event_type": "inbound.received", "data": _data()}
        first = await adapter._handle_webhook(_signed(envelope))
        second = await adapter._handle_webhook(_signed(envelope))
        await adapter._inbound_events.close()
        return first, second

    first, second = asyncio.run(run())
    assert first.status == 202
    assert (second.status, second.text) == (200, "duplicate")


def test_agent_turn_message_and_session(adapter):
    asyncio.run(adapter._run_inbound_agent(_data()))
    event = adapter._enqueued[0]
    assert event.source.chat_id == "inbound:payments"
    assert getattr(event.source, "thread_id", None) is None
    assert event.internal is True
    assert event.text.startswith("[inkbox:inbound source=payments] UNTRUSTED external content")
    assert event.text.index("Log each charge.") < event.text.index('{"amount": 5}')


def test_agent_turn_notes_binary_body(adapter):
    data = _data(body=base64.b64encode(b"\x00\x01").decode(), body_encoding="base64", content_type="image/png")
    asyncio.run(adapter._run_inbound_agent(data))
    assert "(binary body of type image/png, not shown)" in adapter._enqueued[0].text
    assert "AAE=" not in adapter._enqueued[0].text


def test_inbound_replies_are_not_delivered(adapter):
    result = asyncio.run(adapter.send("inbound:payments", "done"))
    assert result.success is True
    assert result.message_id == "inbound-event-noreply"


def _queue(tmp_path, run_agent=None, report=None):
    return InboundEvents(tmp_path / "q.sqlite3", run_agent or AsyncMock(), report or (lambda *_: None))


def _row(queue, key="inbdel_1"):
    return queue.db.execute("SELECT state,attempts,due FROM events WHERE delivery_key=?", (key,)).fetchone()


def test_script_receives_event_on_stdin_and_empty_output_ends(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    run_agent = AsyncMock()
    queue = _queue(tmp_path, run_agent)
    data = _data(handler={"type": "script", "command": "cat > stdin.json; pwd > cwd.txt"})
    queue.accept(data)
    asyncio.run(queue.process("inbdel_1", data, 0))
    sent = json.loads((tmp_path / "stdin.json").read_text())
    assert sent == {"source": "payments", "event_id": "inbev_1", "headers": {"content-type": "application/json"},
                    "body": '{"amount": 5}', "body_encoding": "utf8"}
    assert (tmp_path / "cwd.txt").read_text().strip() == str(tmp_path)
    run_agent.assert_not_awaited()
    assert _row(queue)[0] == "done"


def test_script_output_becomes_agent_body(tmp_path):
    run_agent = AsyncMock()
    queue = _queue(tmp_path, run_agent)
    data = _data(handler={"type": "script", "command": "echo summary"})
    queue.accept(data)
    asyncio.run(queue.process("inbdel_1", data, 0))
    run_agent.assert_awaited_once_with(data, "summary\n")
    assert _row(queue)[0] == "done"


def test_script_failure_retries_then_reports(tmp_path):
    reports = []
    queue = _queue(tmp_path, report=lambda data, error: reports.append((data["event_id"], error)))
    data = _data(handler={"type": "script", "command": "echo boom >&2; exit 3"})
    queue.accept(data)
    for attempt in range(len(inbound_events.RETRY_DELAYS)):
        asyncio.run(queue.process("inbdel_1", data, attempt))
        state, attempts, _ = _row(queue)
        assert (state, attempts) == ("pending", attempt + 1)
        assert reports == []
    asyncio.run(queue.process("inbdel_1", data, len(inbound_events.RETRY_DELAYS)))
    assert _row(queue)[0] == "failed"
    assert reports == [("inbev_1", "boom")]


def test_retry_delays_follow_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(inbound_events.time, "time", lambda: 1000.0)
    queue = _queue(tmp_path)
    data = _data(handler={"type": "script", "command": "exit 1"})
    queue.accept(data)
    dues = []
    for attempt in range(4):
        asyncio.run(queue.process("inbdel_1", data, attempt))
        dues.append(_row(queue)[2] - 1000.0)
    assert dues == [10, 30, 120, 600]
    asyncio.run(queue.process("inbdel_1", data, 4))
    assert _row(queue)[0] == "failed"


def test_script_runs_five_times_then_reports(tmp_path, monkeypatch):
    runs, reports = [], []

    async def fake_run(command, data):
        runs.append(command)
        return False, "", "boom"

    monkeypatch.setattr(inbound_events, "run_script", fake_run)
    queue = _queue(tmp_path, report=lambda data, error: reports.append(error))
    data = _data(handler={"type": "script", "command": "x"})
    queue.accept(data)
    while _row(queue)[0] == "pending":
        asyncio.run(queue.process("inbdel_1", data, _row(queue)[1]))
    assert len(runs) == 5 and reports == ["boom"]


def test_replay_with_new_delivery_id_runs_again(tmp_path):
    queue = _queue(tmp_path)
    assert queue.accept(_data()) is True
    assert queue.accept(_data()) is False  # retry of the same delivery
    assert queue.accept(_data(delivery_id="inbdel_2")) is True  # replay


def test_dedupe_falls_back_to_event_id(tmp_path):
    queue = _queue(tmp_path)
    assert queue.accept(_data(delivery_id=None)) is True
    assert queue.accept(_data(delivery_id=None)) is False
    assert _row(queue, "inbev_1")[0] == "pending"


def test_agent_body_capped_at_64_kib_on_char_boundary(adapter):
    body = "a" + "\u00e9" * 40000  # the 65536-byte cut lands inside a 2-byte char
    asyncio.run(adapter._run_inbound_agent(_data(), body))
    shown = adapter._enqueued[0].text.split("Body:\n", 1)[1]
    assert shown.endswith("\n(truncated)")
    kept = shown[: -len("\n(truncated)")]
    assert len(kept.encode()) == 65535 and kept == body[: len(kept)]
    asyncio.run(adapter._run_inbound_agent(_data(), "x" * 65536))
    assert adapter._enqueued[1].text.endswith("Body:\n" + "x" * 65536)


def test_script_timeout_counts_as_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(inbound_events, "SCRIPT_TIMEOUT", 0.2)
    ok, _, err = asyncio.run(inbound_events.run_script("sleep 5", _data()))
    assert ok is False
    assert "timed out" in err


def test_worker_processes_queued_events(tmp_path):
    seen = []

    async def run_agent(data, body):
        seen.append((data["event_id"], body))

    async def run():
        queue = _queue(tmp_path, run_agent)
        queue.start()
        queue.accept(_data("inbev_a"))
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)
        await queue.close()

    asyncio.run(run())
    assert seen == [("inbev_a", None)]


def test_failure_report_uses_sdk_when_available(adapter):
    calls = []
    adapter._inkbox = types.SimpleNamespace(inbound=types.SimpleNamespace(
        report_inbound_event_result=lambda *args, **kwargs: calls.append((args, kwargs))))
    adapter._report_inbound_failure(_data(), "boom")
    assert calls == [(("agent", "inbsrc_1", "inbev_1"), {"status": "failed", "error": "boom"})]
    adapter._inkbox = object()
    adapter._report_inbound_failure(_data(), "boom")
