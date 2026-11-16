import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from inkbox.remote_mcp import RemoteMcp, RemoteToolError
from inkbox_plugin.remote_compat import compatibility_arguments
from inkbox_plugin.terminal_reply import blocked, send, source


@pytest.mark.parametrize("action", ["create", "update", "delete"])
def test_contact_alias_uses_public_arguments_and_durable_continuation(tmp_path, action):
    async def run():
        name = "inkbox_contact_" + action
        fields = {
            "preferred_name": "Example",
            "emails": ["contact@example.test"],
            "phones": [{"value": "+15550000001", "label": "work", "isPrimary": False}],
        }
        args = fields if action == "create" else {
            "contact_id": "00000000-0000-4000-8000-000000000001", **(fields if action == "update" else {})
        }
        converted = compatibility_arguments("inkbox_" + action + "_contact", args)
        # Advertised tools/list schemas, with descriptive annotations omitted.
        catalog = json.loads((Path(__file__).parent / "fixtures" / "contact-input-schemas.json").read_text())
        remote = RemoteMcp(api_key="synthetic", base_url="https://example.test", identity="example", journal_path=tmp_path / "call.sqlite")
        remote._catalog = tuple(catalog)
        requests = []

        async def request(method, params):
            requests.append(params)
            if "requestState" not in params:
                return {"resultType": "input_required", "requestState": "prepared", "inputRequests": {}}
            return {"content": [], "structuredContent": {"id": "contact"}}

        remote.request = request
        await remote.call(name, converted, call_identity="native-call")
        await remote.call(name, converted, call_identity="native-call")
        assert len(requests) == 3
        assert requests[1] == requests[2]
        assert "idempotency_key" not in requests[0]["arguments"]

        before = len(requests)
        with pytest.raises(RemoteToolError, match="remote schema"):
            await remote.call(name, {**converted, "idempotency_key": "unexpected"}, call_identity="invalid-call")
        assert len(requests) == before
        if action != "delete":
            field = "contact" if action == "create" else "patch"
            with pytest.raises(RemoteToolError, match="remote schema"):
                await remote.call(name, {**converted, field: {"emails": [{"value": 42}]}}, call_identity="invalid-nested-call")
            assert len(requests) == before

    asyncio.run(run())


@pytest.mark.parametrize("channel", ["email", "sms"])
def test_terminal_reply_validates_public_schema_and_recovers_lost_response(tmp_path, channel):
    async def run():
        path = tmp_path / "reply.sqlite"
        bound = source(channel, {"message_id": "incoming", "sender": "+15550000001" if channel == "sms" else "sender@example.com"})
        remote = RemoteMcp(api_key="synthetic", base_url="https://example.test", identity="example", journal_path=path)
        name = "inkbox_email_reply" if channel == "email" else "inkbox_text_send"
        properties = ({"message_id": {"type": "string"}, "reply_all": {"type": "boolean"},
                       "approved_recipients": {"type": "object"}, "body_text": {"type": "string"}}
                      if channel == "email" else {"to": {"type": "array"}, "text": {"type": "string"}})
        remote._catalog = ({"name": name, "inputSchema": {
            "type": "object", "properties": properties, "required": list(properties), "additionalProperties": False,
        }},)
        original = SimpleNamespace(id="incoming", direction="inbound", reply_all_recipients=SimpleNamespace(to=["sender@example.com"], cc=[]))
        client = SimpleNamespace(get_identity=lambda _: SimpleNamespace(get_message=lambda _: original))
        requests = []

        async def request(method, params):
            requests.append(params)
            if "requestState" not in params:
                return {"resultType": "input_required", "requestState": "prepared", "inputRequests": {}}
            if len(requests) == 2:
                raise RemoteToolError("Synthetic lost response")
            receipt = ({"id": "accepted", "direction": "outbound", "to_addresses": ["sender@example.com"], "cc_addresses": []}
                       if channel == "email" else {"message_id": "accepted", "delivery_status": "queued", "recipients": ["+15550000001"]})
            return {"content": [], "structuredContent": receipt}

        remote.request = request
        with pytest.raises(RemoteToolError, match="lost response"):
            await send(remote, client, "example", bound, {"text": "Done"}, path, lambda: None)
        assert blocked(remote, bound, path)
        await send(remote, client, "example", bound, {"text": "Done"}, path, lambda: None)
        assert len(requests) == 3
        assert requests[1] == requests[2]
        assert "idempotency_key" not in requests[0]["arguments"]
        assert blocked(remote, bound, path)

    asyncio.run(run())
