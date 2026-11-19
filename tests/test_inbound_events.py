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


def _data(event_id="inbev_1", handler=None, delivery_id="7b1c2d3e-0000-4000-8000-000000000001", **request):
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


ACTIVATION = "7d3f8f1e-2a4b-4c55-9f0e-1b2c3d4e5f60"


def _envelope(data, activation=ACTIVATION):
    """The signed shape the server relays to a hosted agent."""
    event_id = "evt_" + hashlib.sha256(f"inbound:{data['delivery_id']}".encode()).hexdigest()
    return {
        "event_type": "inbound.received", "timestamp": "2026-01-01T00:00:01+00:00", "data": data,
        "id": event_id,
        "hosted_delivery": {"version": 1, "activation_id": activation, "event_id": event_id, "starts_agent": True},
    }


def _signed(envelope, key="secret"):
    body = json.dumps(envelope, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    ts, rid = "1700000000", envelope.get("id", "req-1")
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
    instance.outcome = "success"

    async def capture(event):
        instance._enqueued.append(event)

        async def turn():
            event._gateway_accepted = True
            await instance.on_processing_complete(event, types.SimpleNamespace(value=instance.outcome))
        return asyncio.create_task(turn())

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
    response = asyncio.run(adapter._handle_webhook(_signed(_envelope(_data()))))
    assert (response.status, response.text) == (200, "ignored")


def test_rejects_bad_signature(adapter, tmp_path):
    adapter._inbound_events = InboundEvents(tmp_path / "q.sqlite3", AsyncMock(), lambda *_: None)
    request = _signed(_envelope(_data()), key="wrong")
    assert asyncio.run(adapter._handle_webhook(request)).status == 401
    unsigned = _Request(json.dumps(_envelope(_data())).encode(), {})
    assert asyncio.run(adapter._handle_webhook(unsigned)).status == 401


def test_accepts_once_and_dedupes(adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("INKBOX_HOSTED_ACTIVATION_ID", ACTIVATION)

    async def run():
        # No managed receiver: a hosted inbound event must not take the managed-inbox path.
        adapter._inbound_events = InboundEvents(tmp_path / "q.sqlite3", AsyncMock(), lambda *_: None)
        envelope = _envelope(_data())
        first = await adapter._handle_webhook(_signed(envelope))
        second = await adapter._handle_webhook(_signed(envelope))
        await adapter._inbound_events.close()
        return first, second

    first, second = asyncio.run(run())
    assert first.status == 202
    assert (second.status, second.text) == (200, "duplicate")


def test_rejects_other_activation_and_missing_delivery_id(adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("INKBOX_HOSTED_ACTIVATION_ID", ACTIVATION)

    async def run():
        adapter._inbound_events = _queue(tmp_path)
        other = await adapter._handle_webhook(_signed(_envelope(_data(), activation="other")))
        data = _data()
        envelope = _envelope(data)
        del data["delivery_id"]
        missing = await adapter._handle_webhook(_signed(envelope))
        count = adapter._inbound_events.pending()
        await adapter._inbound_events.close()
        return other.status, missing.status, count

    assert asyncio.run(run()) == (409, 400, 0)


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


KEY = "7b1c2d3e-0000-4000-8000-000000000001"


def _row(queue, key=KEY):
    return queue.db.execute("SELECT state,attempts,due FROM inbound_events WHERE delivery_id=?", (key,)).fetchone()


def test_script_receives_event_on_stdin_and_empty_output_ends(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    run_agent = AsyncMock()
    queue = _queue(tmp_path, run_agent)
    data = _data(handler={"type": "script", "command": "cat > stdin.json; pwd > cwd.txt"})
    queue.accept(data)
    asyncio.run(queue.process(KEY, data, 0))
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
    asyncio.run(queue.process(KEY, data, 0))
    run_agent.assert_awaited_once_with(data, "summary\n")
    assert _row(queue)[0] == "done"


def _drain_reports(queue, data):
    while _row(queue)[0] == "reporting":
        asyncio.run(queue.report(KEY, data, _row(queue)[1]))


def test_script_failure_retries_then_reports(tmp_path):
    reports = []
    queue = _queue(tmp_path, report=lambda data, error: reports.append((data["event_id"], error)))
    data = _data(handler={"type": "script", "command": "echo boom >&2; exit 3"})
    queue.accept(data)
    for attempt in range(len(inbound_events.RETRY_DELAYS)):
        asyncio.run(queue.process(KEY, data, attempt))
        state, attempts, _ = _row(queue)
        assert (state, attempts) == ("pending", attempt + 1)
        assert reports == []
    asyncio.run(queue.process(KEY, data, len(inbound_events.RETRY_DELAYS)))
    assert _row(queue)[0] == "reporting"
    _drain_reports(queue, data)
    assert _row(queue)[0] == "failed"
    assert reports == [("inbev_1", "boom")]


def test_retry_delays_follow_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(inbound_events.time, "time", lambda: 1000.0)
    queue = _queue(tmp_path)
    data = _data(handler={"type": "script", "command": "exit 1"})
    queue.accept(data)
    dues = []
    for attempt in range(4):
        asyncio.run(queue.process(KEY, data, attempt))
        dues.append(_row(queue)[2] - 1000.0)
    assert dues == [10, 30, 120, 600]
    asyncio.run(queue.process(KEY, data, 4))
    assert _row(queue)[0] == "reporting"


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
        asyncio.run(queue.process(KEY, data, _row(queue)[1]))
    _drain_reports(queue, data)
    assert len(runs) == 5 and reports == ["boom"]


def test_replay_with_new_delivery_id_runs_again(tmp_path):
    queue = _queue(tmp_path)
    assert queue.accept(_data()) is True
    assert queue.accept(_data()) is False  # retry of the same delivery
    assert queue.accept(_data(delivery_id="7b1c2d3e-0000-4000-8000-000000000002")) is True  # replay


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
    assert calls == [(("agent", "inbsrc_1", "inbev_1"), {"status": "failed", "error": "boom", "delivery_id": KEY})]
    adapter._inkbox = object()
    adapter._report_inbound_failure(_data(), "boom")


def test_agent_turn_fails_unless_it_succeeds(adapter):
    adapter.outcome = "failure"
    with pytest.raises(RuntimeError, match="failure"):
        asyncio.run(adapter._run_inbound_agent(_data()))

    async def rejected(event):
        event._gateway_accepted = False
        return asyncio.create_task(asyncio.sleep(0))

    adapter._enqueue = rejected
    with pytest.raises(RuntimeError, match="not started"):
        asyncio.run(adapter._run_inbound_agent(_data()))


def test_agent_turn_waits_for_processing_complete(adapter):
    async def admitted(event):
        event._gateway_accepted = True
        adapter._enqueued.append(event)
        return asyncio.create_task(asyncio.sleep(0))

    adapter._enqueue = admitted

    async def run():
        turn = asyncio.create_task(adapter._run_inbound_agent(_data()))
        await asyncio.sleep(0.05)
        waiting = not turn.done()
        await adapter.on_processing_complete(adapter._enqueued[0], types.SimpleNamespace(value="success"))
        await turn
        return waiting

    assert asyncio.run(run()) is True


def test_agent_failure_is_reported_once(tmp_path):
    reports = []

    async def run_agent(data, body):
        raise RuntimeError("agent turn ended: failure")

    queue = _queue(tmp_path, run_agent, lambda data, error: reports.append(error))
    data = _data()
    queue.accept(data)
    asyncio.run(queue.process(KEY, data, 0))
    _drain_reports(queue, data)
    assert _row(queue)[0] == "failed"
    assert reports == ["agent turn ended: failure"]


def test_unexpected_handler_error_is_reported(tmp_path, monkeypatch):
    reports = []

    async def broken(command, data):
        raise OSError("no shell")

    monkeypatch.setattr(inbound_events, "run_script", broken)

    async def run():
        queue = _queue(tmp_path, report=lambda data, error: reports.append(error))
        queue.start()
        queue.accept(_data(handler={"type": "script", "command": "x"}))
        for _ in range(200):
            if reports:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        state = _row(queue)[0]
        await queue.close()
        return state

    assert asyncio.run(run()) == "failed"
    assert reports == ["handler error (OSError)"]


def test_failure_report_is_persisted_and_retried_without_rerun(tmp_path, monkeypatch):
    runs, calls = [], []

    async def fake_run(command, data):
        runs.append(command)
        return False, "", "boom"

    def flaky(data, error):
        calls.append(error)
        if len(calls) == 1:
            raise OSError("offline")

    monkeypatch.setattr(inbound_events, "run_script", fake_run)
    queue = _queue(tmp_path, report=flaky)
    data = _data(handler={"type": "script", "command": "x"})
    queue.accept(data)
    while _row(queue)[0] == "pending":
        asyncio.run(queue.process(KEY, data, _row(queue)[1]))
    asyncio.run(queue.report(KEY, data, 0))
    assert _row(queue)[:2] == ("reporting", 1)
    queue.db.close()
    queue = _queue(tmp_path, report=flaky)  # restart
    _drain_reports(queue, data)
    assert _row(queue)[0] == "failed"
    assert len(runs) == 5 and calls == ["boom", "boom"]


def test_failure_report_gives_up_after_a_day(tmp_path, monkeypatch):
    def offline(data, error):
        raise OSError("offline")

    queue = _queue(tmp_path, report=offline)
    data = _data()
    queue.accept(data)
    queue.fail(KEY, "boom")
    monkeypatch.setattr(inbound_events.time, "time", lambda: queue.db.execute(
        "SELECT failed_at FROM inbound_events").fetchone()[0] + inbound_events.REPORT_WINDOW)
    asyncio.run(queue.report(KEY, data, 3))
    assert _row(queue)[0] == "failed"


def test_stale_delivery_report_counts_as_done(adapter):
    class Stale(Exception):
        status_code = 409

    def report(*args, **kwargs):
        raise Stale("stale_delivery")

    adapter._inkbox = types.SimpleNamespace(inbound=types.SimpleNamespace(report_inbound_event_result=report))
    adapter._report_inbound_failure(_data(), "boom")

    class Down(Exception):
        status_code = 503

    def down(*args, **kwargs):
        raise Down()

    adapter._inkbox = types.SimpleNamespace(inbound=types.SimpleNamespace(report_inbound_event_result=down))
    with pytest.raises(Down):
        adapter._report_inbound_failure(_data(), "boom")


def test_backoff_counts_as_activity_but_pending_report_does_not(tmp_path):
    from inkbox_plugin import managed_lifecycle

    queue = _queue(tmp_path)
    data = _data(handler={"type": "script", "command": "exit 1"})
    queue.accept(data)
    asyncio.run(queue.process(KEY, data, 0))
    assert _row(queue)[0] == "pending" and _row(queue)[2] > inbound_events.time.time()
    adapter = types.SimpleNamespace(is_connected=True, _inbound_events=queue)
    assert managed_lifecycle.status(adapter)["active"] == 1
    queue.fail(KEY, "boom")
    assert managed_lifecycle.status(adapter)["active"] == 0


def test_running_turn_counts_as_activity(tmp_path):
    seen = []

    async def run_agent(data, body):
        seen.append(queue.pending())

    queue = _queue(tmp_path, run_agent)
    data = _data()
    queue.accept(data)
    asyncio.run(queue.process(KEY, data, 0))
    assert seen == [1] and queue.pending() == 0


def test_interrupted_agent_turn_is_reported_not_repeated(tmp_path):
    reports, turns = [], []

    async def run_agent(data, body):
        turns.append(body)
        await asyncio.Event().wait()

    async def stop_mid_turn():
        queue = _queue(tmp_path, run_agent)
        queue.start()
        queue.accept(_data(handler={"type": "script", "command": "echo summary"}))
        for _ in range(500):
            if turns:
                break
            await asyncio.sleep(0.01)
        await queue.close()

    asyncio.run(stop_mid_turn())
    queue = _queue(tmp_path, report=lambda data, error: reports.append(error))  # restart
    assert _row(queue)[0] == "reporting"
    _drain_reports(queue, _data())
    assert turns == ["summary\n"] and reports == ["agent turn was interrupted by a restart"]


def test_script_output_turn_failure_reports_without_rerun(tmp_path, monkeypatch):
    runs, reports = [], []

    async def fake_run(command, data):
        runs.append(command)
        return True, "summary", ""

    async def run_agent(data, body):
        raise RuntimeError("no session")

    monkeypatch.setattr(inbound_events, "run_script", fake_run)
    queue = _queue(tmp_path, run_agent, lambda data, error: reports.append(error))
    data = _data(handler={"type": "script", "command": "x"})
    queue.accept(data)
    asyncio.run(queue.process(KEY, data, 0))
    _drain_reports(queue, data)
    assert runs == ["x"] and reports == ["no session"]


@pytest.mark.parametrize(("status", "retried"), [(409, False), (400, False), (404, False), (408, True),
                                                 (429, True), (500, True), (None, True)])
def test_failure_report_response_handling(adapter, status, retried):
    error = OSError("offline") if status is None else type("ApiError", (Exception,), {"status_code": status})()

    def report(*args, **kwargs):
        raise error

    adapter._inkbox = types.SimpleNamespace(inbound=types.SimpleNamespace(report_inbound_event_result=report))
    if retried:
        with pytest.raises(type(error)):
            adapter._report_inbound_failure(_data(), "boom")
    else:
        adapter._report_inbound_failure(_data(), "boom")


def test_script_output_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    big = "x" * (2 * 1024 * 1024)
    command = ("cat >/dev/null; head -c 1000000 /dev/zero | tr '\\0' o; "
               "head -c 100000 /dev/zero | tr '\\0' e >&2; printf END >&2")
    ok, out, err = asyncio.run(inbound_events.run_script(command, _data(body=big)))
    assert ok is True
    assert len(out) == inbound_events.STDOUT_LIMIT + 1
    assert len(err) == inbound_events.STDERR_TAIL and err.endswith("END")
