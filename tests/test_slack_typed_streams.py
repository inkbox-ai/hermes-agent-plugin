"""Typed SDK dispatch preserves the same uncertainty and source boundaries."""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from inkbox_plugin.slack_progress import SlackProgress
from inkbox_plugin.slack_streams import SlackTaskStreams
from .test_slack_task_streams import STREAM_ID, native_resource, operation, source


class TypedResource:
    def __init__(self, result=None):
        self.calls = []
        self.result = result
        self.send_message = Mock()
        self.update_message = Mock()

    @property
    def _http(self):
        raise AssertionError("The typed integration must not access private transport")

    def capabilities(self, connection_id):
        return NS(capabilities={"task_streaming": NS(scopes_satisfied=True)})

    def _write(self, kind, args, kwargs):
        self.calls.append((kind, args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return NS(**(self.result or operation(kind)))

    def start_stream(self, *args, **kwargs):
        return self._write("stream_start", args, kwargs)

    def append_stream(self, *args, **kwargs):
        return self._write("stream_append", args, kwargs)

    def stop_stream(self, *args, **kwargs):
        return self._write("stream_stop", args, kwargs)

    def get_operation_by_key(self, *args, **kwargs):
        self.calls.append(("lookup", args, kwargs))
        return NS(**operation())


def test_typed_lifecycle_uses_public_methods_and_original_source(tmp_path):
    async def run():
        sdk = TypedResource()
        tracker = SlackProgress(sdk, tmp_path / "progress.json", interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Reading information")
        await tracker.flush()
        await tracker.progress("chat", source(), "Checking results")
        await tracker.flush()
        await tracker.notify("chat", source(), "completed")
        await tracker.flush()
        assert [call[0] for call in sdk.calls] == ["stream_start", "stream_append", "stream_stop"]
        initial = sdk.calls[0]
        assert initial[1] == (source()["connection_id"], "C123")
        assert {key: initial[2][key] for key in ("thread_ts", "recipient_user_id", "recipient_team_id", "task_display_mode")} == {
            "thread_ts": source()["thread_ts"], "recipient_user_id": "U123",
            "recipient_team_id": "T123", "task_display_mode": "timeline",
        }
        assert [call[1] for call in sdk.calls[1:]] == [(*initial[1], STREAM_ID)] * 2
        assert len({call[2]["idempotency_key"] for call in sdk.calls}) == 3
        assert sdk.calls[-1][2]["chunks"][0]["status"] == "complete"
        sdk.send_message.assert_not_called()
        sdk.update_message.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "wrong_route", "unknown", "in_progress"])
def test_unconfirmed_typed_call_never_replays_via_legacy_transport(tmp_path, failure):
    async def run():
        result = TimeoutError() if failure == "timeout" else operation(
            status=failure if failure in {"unknown", "in_progress"} else "succeeded",
            **({"conversation_id": "COTHER"} if failure == "wrong_route" else {}),
        )
        sdk = TypedResource(result)
        tracker = SlackProgress(sdk, tmp_path / "progress.json", interval=0)
        await tracker.notify("chat", source(), "accepted")
        await tracker.progress("chat", source(), "Working")
        await tracker.flush()
        await tracker.notify("chat", source(), "cancelled")
        await tracker.flush()
        assert len(sdk.calls) == 1
        key = sdk.calls[0][2]["idempotency_key"]
        sdk.result = None
        recovered = SlackProgress(sdk, tracker.path, interval=0)
        await recovered.recover()
        await recovered.flush()
        assert [call[0] for call in sdk.calls] == ["stream_start", "lookup", "stream_stop"]
        assert sdk.calls[1][1:] == ((source()["connection_id"],), {"idempotency_key": key})
        assert sdk.calls[-1][1][-1] == STREAM_ID
        sdk.send_message.assert_not_called()
    asyncio.run(run())


def test_partial_typed_surface_keeps_older_sdk_compatibility():
    class PartialResource:
        def start_stream(self, *args, **kwargs):
            raise AssertionError("Partial SDK surface must not receive stream writes")
    sdk = PartialResource()
    old = native_resource()
    sdk._http = old._http
    sdk.capabilities = old.capabilities
    streams = SlackTaskStreams(sdk)
    assert streams.capable(source())
    result = streams.write(source(), kind="stream_start", key="test-start", chunks=[])
    assert result.id == STREAM_ID
    old._http.post.assert_called_once()


def test_real_typed_sdk_dispatch_and_recovery_lookup():
    import httpx
    from inkbox import Inkbox

    client = Inkbox(api_key="synthetic-test-key", base_url="https://api.example")
    methods = ("start_stream", "append_stream", "stop_stream", "get_operation_by_key")
    if not all(callable(getattr(type(client.slack), name, None)) for name in methods):
        client.close()
        pytest.skip("Installed SDK predates typed task streams; compatibility path tested separately")
    requests = []
    def handle(request):
        requests.append(request)
        kind = "stream_stop" if request.url.path.endswith("/stop") else "stream_append" if request.url.path.endswith("/append") else "stream_start"
        return httpx.Response(200, json=operation(kind))
    client._api_http._client.close()
    client._api_http._client = httpx.Client(base_url="https://api.example/api/v1",
        headers={"X-API-Key": "synthetic-test-key"}, transport=httpx.MockTransport(handle))
    for name in methods:
        setattr(client.slack, name, Mock(wraps=getattr(client.slack, name)))
    try:
        streams = SlackTaskStreams(client.slack)
        assert streams.typed and streams.http is None
        chunks = [{"type": "task_update", "id": "task", "title": "Reading", "status": "in_progress"}]
        started = streams.write(source(), kind="stream_start", key="start", chunks=chunks)
        streams.write(source(), kind="stream_append", key="append", chunks=chunks, stream_id=started.id)
        streams.write(source(), kind="stream_stop", key="stop", chunks=[], stream_id=started.id)
        assert streams.lookup(source(), kind="stream_start", key="start").id == started.id
        for name in methods:
            getattr(client.slack, name).assert_called_once()
        assert [r.headers["Idempotency-Key"] for r in requests] == ["start", "append", "stop", "start"]
        assert requests[-1].method == "GET" and requests[-1].url.path.endswith("/operations/by-key")
        assert json.loads(requests[-2].content) == {"chunks": []}
        assert all(r.headers["X-API-Key"] == "synthetic-test-key" for r in requests)
    finally:
        client.close()
