import asyncio
from types import SimpleNamespace

import pytest
from inkbox.remote_mcp import RemoteMcp, RemoteToolError

from inkbox_plugin.terminal_reply import blocked, send, source


def test_terminal_reply_reconciles_uncertainty_and_separates_new_source(tmp_path):
    async def run():
        path = tmp_path / "session.sqlite"
        remote = RemoteMcp(api_key="synthetic", base_url="https://inkbox.ai", identity="example", journal_path=path)
        bound = source("sms", {"message_id": "message-one", "sender": "+15550000001"})
        calls = []

        async def call(name, args, **kwargs):
            kwargs["before_execute"]()
            kwargs["before_submit"]()
            calls.append((name, args, kwargs["call_identity"]))
            if len(calls) == 1:
                raise RemoteToolError("Synthetic lost response")
            return {
                "content": [],
                "structuredContent": {"message_id": "accepted", "delivery_status": "sent", "recipients": ["+15550000001"]},
            }

        remote.call = call
        with pytest.raises(RemoteToolError, match="lost response"):
            await send(remote, None, "example", bound, {"text": "Finished"}, path, lambda: None)
        reopened = RemoteMcp(api_key="synthetic", base_url="https://inkbox.ai", identity="example", journal_path=path)
        assert blocked(reopened, bound, path)
        with pytest.raises(RemoteToolError, match="pending"):
            await send(remote, None, "example", bound, {"text": "Different"}, path, lambda: None)
        await send(remote, None, "example", bound, {"text": "Finished"}, path, lambda: None)
        assert calls[0] == calls[1]
        await send(remote, None, "example", bound, {"text": "Finished"}, path, lambda: None)
        assert len(calls) == 2
        new = {**bound, "source_id": "message-two"}
        assert not blocked(reopened, new, path)
        await send(remote, None, "example", new, {"text": "Finished"}, path, lambda: None)
        assert calls[-1][2] != calls[0][2]

    asyncio.run(run())


def test_preparation_rejection_does_not_consume_final_reply(tmp_path):
    async def run():
        path = tmp_path / "session.sqlite"
        remote = RemoteMcp(api_key="synthetic", base_url="https://inkbox.ai", identity="example", journal_path=path)
        bound = source("sms", {"message_id": "message", "sender": "+15550000001"})

        async def call(*args, **kwargs):
            return {"content": [], "isError": True}

        remote.call = call
        assert (await send(remote, None, "example", bound, {"text": "Finished"}, path, lambda: None))["isError"]
        assert not blocked(remote, bound, path)

    asyncio.run(run())


def test_reply_all_uses_current_server_destination_and_rechecks_source(tmp_path):
    async def run():
        path = tmp_path / "session.sqlite"
        remote = RemoteMcp(api_key="synthetic", base_url="https://inkbox.ai", identity="example", journal_path=path)
        bound = source("email", {"message_id": "message", "sender": "sender@example.com"})
        original = SimpleNamespace(
            id="message",
            direction="inbound",
            reply_all_recipients=SimpleNamespace(to=["sender@example.com"], cc=["other@example.com"]),
        )
        client = SimpleNamespace(get_identity=lambda handle: SimpleNamespace(get_message=lambda mid: original))
        alive = True
        calls = []

        def validate():
            if not alive:
                raise RemoteToolError("Source ended")

        async def call(name, args, **kwargs):
            nonlocal alive
            calls.append((name, args))
            alive = False
            kwargs["before_submit"]()
            raise AssertionError("Cannot submit a retired source")

        remote.call = call
        with pytest.raises(RemoteToolError, match="Source ended"):
            await send(remote, client, "example", bound, {"text": "Finished"}, path, validate)
        assert calls[0][0] == "inkbox_email_reply"
        assert calls[0][1]["approved_recipients"] == {"to": ["sender@example.com"], "cc": ["other@example.com"], "bcc": []}
        assert not blocked(remote, bound, path)

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"delivery_status": "blocked_spam_filter"},
        {"delivery_status": "sending_failed"},
        {"message_id": ""},
        {"recipients": ["+15550000002"]},
        {"error": "synthetic_failure"},
    ],
)
def test_sms_receipt_requires_canonical_acceptance(tmp_path, changes):
    async def run():
        path = tmp_path / "receipt.sqlite"
        remote = RemoteMcp(api_key="synthetic", base_url="https://example.test", identity="example", journal_path=path)
        bound = source("sms", {"message_id": "source", "sender": "+15550000001"})

        async def call(name, args, **kwargs):
            kwargs["before_execute"]()
            kwargs["before_submit"]()
            return {
                "content": [],
                "structuredContent": {
                    "message_id": "accepted",
                    "recipients": ["+15550000001"],
                    "delivery_status": "queued",
                    **changes,
                },
            }

        remote.call = call
        with pytest.raises(RemoteToolError):
            await send(remote, None, "example", bound, {"text": "Finished"}, path, lambda: None)
        assert blocked(remote, bound, path)

    asyncio.run(run())
