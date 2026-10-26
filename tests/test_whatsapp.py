"""Real-SDK WhatsApp wires and native receipt/media/permission ownership."""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import httpx
from inkbox import Inkbox
from inkbox_plugin.whatsapp import receive, send_media, record_status
from inkbox_plugin.whatsapp_tools import invoke
from inkbox_plugin.imessage_state import bind_context, clear_context
from tests import test_native_turns as native_harness
from tests.test_native_turns import harness, receipt, settle

factory = native_harness.factory
from tests.test_companion import uid

TIME = "2026-10-01T12:00:00Z"
WIRE = {
    "id": uid(800),
    "agent_identity_id": uid(100),
    "conversation_id": uid(30),
    "direction": "outbound",
    "local_number": "+15555550100",
    "remote_number": "+15555550101",
    "sender_number": "+15555550100",
    "participants": ["+15555550101"],
    "is_group": False,
    "content": "Answer",
    "media": [],
    "status": "sent",
    "delivery_state": "unknown",
    "recipients": [],
    "is_read": False,
    "is_blocked": False,
    "error_code": None,
    "error_detail": None,
    "source_at": None,
    "created_at": TIME,
    "updated_at": TIME,
    "sent_at": TIME,
    "delivered_at": None,
    "failed_at": None,
    "status_observed_at": TIME,
    "status_refresh_pending": False,
}
MEDIA = {
    "id": uid(801),
    "filename": "image.png",
    "content_type": "image/png",
    "size": 3,
    "status": "ready",
    "url": "https://media.example/fresh",
}


def real_identity(adapter):
    client = Inkbox("test-only", base_url="http://127.0.0.1:9")
    client._ids_resource._http = Mock()
    client._ids_resource._http.get.return_value = {
        "id": uid(100),
        "organization_id": "org_example",
        "agent_handle": "sample-agent",
        "created_at": TIME,
        "updated_at": TIME,
    }
    identity = client.get_identity("sample-agent")
    client.whatsapp._http = Mock()
    client.whatsapp._http.post.return_value = {"message": WIRE}
    client.whatsapp._http.post_multipart.return_value = MEDIA
    client.whatsapp._http.get.return_value = MEDIA
    adapter._reply_identity = identity
    adapter._base_url = "http://127.0.0.1:9"
    adapter._last_inbound_modality = {}
    adapter._contact_memories = Mock(return_value=[])
    owner = adapter._message_handler.__self__
    owner._is_user_authorized = lambda source: source.user_id_alt == "+15555550101" and source.user_id not in owner.denied
    return client


def inbound(**changes):
    return {
        "id": "evt_whatsapp_example",
        "event_type": "whatsapp.received",
        "timestamp": TIME,
        "data": {
            "contacts": [],
            "agent_identities": [],
            "message": {
                **WIRE,
                "id": uid(1),
                "direction": "inbound",
                "sender_number": "+15555550101",
                "status": "received",
                "delivery_state": None,
                **changes,
            },
        },
    }


def test_native_plain_reply_works_without_imessage_flag_and_uses_stable_real_sdk_key(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.adapter._imessage_threaded_replies = False
        try:
            event = receipt(1, mode="whatsapp")
            event.metadata["inkbox_reply_route"].update(identity_id=uid(100), base_url=value.adapter._base_url)
            await value.queue.accept(event)
            await value.queue.accept(event)
            await settle(value.queue)
            assert len(value.inputs) == 1
            call = client.whatsapp._http.post.call_args
            assert call.kwargs["params"] == {"agent_identity_id": uid(100)}
            assert set(call.kwargs["json"]) == {"conversation_id", "text"}
            turn = next(iter(value.queue.rows.values()))["turns"][0]
            assert turn["reply_idempotency_key"] == call.kwargs["headers"]["Idempotency-Key"]
            assert turn["state"] == "done"
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_received_media_is_owned_and_refreshed_at_host_dispatch(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.adapter._imessage_threaded_replies = False
        try:
            response = await receive(
                value.adapter, inbound(content=None, media=[{**MEDIA, "url": "https://media.example/expired"}])
            )
            assert response.status == 202
            await settle(value.queue)
            assert len(value.inputs) == 1
            assert value.inputs[0].media_urls == [MEDIA["url"]]
            client.whatsapp._http.get.assert_called_once_with(f"/media/{uid(801)}", params={"agent_identity_id": uid(100)})
            route = next(iter(value.queue.rows.values()))["turns"][0]["route"]
            assert route["mode"] == "whatsapp" and route["whatsapp_media_ids"] == [uid(801)]
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_unsupported_group_wrong_identity_and_self_echo_do_not_admit(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        try:
            for changes in ({"is_group": True}, {"agent_identity_id": uid(999)}, {"sender_number": "+15555550100"}):
                assert (await receive(value.adapter, inbound(**changes))).status == 200
            assert value.inputs == [] and value.queue.rows == {}
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_media_id_is_retained_before_send_and_not_uploaded_again_after_uncertainty(factory, tmp_path, monkeypatch):
    monkeypatch.setattr("inkbox._message_requests.time.sleep", lambda _: None)

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.adapter._imessage_threaded_replies = False
        try:
            await value.queue.accept(receipt(1, mode="whatsapp"))
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            turn = row["turns"][0]
            path = tmp_path / "image.png"
            path.write_bytes(b"png")
            value.adapter.validate_media_delivery_path = lambda path: path
            client.whatsapp._http.post.reset_mock()
            client.whatsapp._http.post.side_effect = httpx.ReadTimeout("response lost")
            meta = {"inkbox_reply_route": turn["route"]}
            result = await send_media(value.adapter, row["chat_id"], path, "caption", meta)
            assert not result.success
            delivery = next(iter(turn["media_deliveries"].values()))
            assert delivery["media_id"] == uid(801) and delivery["state"] == "uncertain"
            assert delivery["idempotency_key"]
            assert client.whatsapp._http.post.call_count == 3
            assert all(
                call == client.whatsapp._http.post.call_args_list[0] for call in client.whatsapp._http.post.call_args_list
            )
            assert not (await send_media(value.adapter, row["chat_id"], path, "caption", meta)).success
            assert client.whatsapp._http.post_multipart.call_count == 1
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_delivery_only_facts_ignore_old_updates_without_model_work(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        try:
            record_status(
                value.adapter,
                {
                    "event_type": "whatsapp.delivered",
                    "data": {
                        "message": {
                            **WIRE,
                            "status": "delivered",
                            "delivery_state": "delivered",
                            "updated_at": "2026-10-01T12:02:00Z",
                        }
                    },
                },
            )
            record_status(value.adapter, {"event_type": "whatsapp.sent", "data": {"message": WIRE}})
            assert value.queue.delivery_status[uid(800)]["delivery_state"] == "delivered"
            assert value.inputs == []
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_tools_preserve_explicit_key_and_cannot_run_after_originating_turn_ends(factory, tmp_path, monkeypatch):
    from inkbox_plugin import tools

    value = factory("imessage")
    client = real_identity(value.adapter)
    identity = value.adapter._reply_identity
    cfg = NS(base_url=value.adapter._base_url)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (cfg, client, identity))
    session = str(tmp_path)
    guard = Mock()
    bind_context(
        session,
        {
            "mode": "whatsapp",
            "turn_id": "turn-example",
            "conversation_id": uid(30),
            "identity_id": uid(100),
            "base_url": value.adapter._base_url,
            "authorize_whatsapp": guard,
        },
    )
    try:
        result = json.loads(
            invoke(
                "send_whatsapp", {"conversation_id": uid(30), "text": "Hello", "idempotency_key": "original-key"}, session
            )
        )
        assert result["ok"] and result["result"]["idempotency_key"] == "original-key"
        assert client.whatsapp._http.post.call_args.kwargs["headers"]["Idempotency-Key"] == "original-key"
        clear_context(session, "turn-example")
        assert "error" in json.loads(invoke("send_whatsapp", {"conversation_id": uid(30), "text": "Hello"}, session))
        assert client.whatsapp._http.post.call_count == 1
    finally:
        clear_context(session, "turn-example")
        client.close()


def test_signature_is_mandatory_even_when_generic_verification_is_disabled():
    from tests.test_webhook_providers import _adapter, _FakeRequest

    adapter = _adapter(require_signature=False, external_events_enabled=True)
    for event_type in ("whatsapp.received", "whatsapp.delivered", "whatsapp.future"):
        response = asyncio.run(adapter._handle_webhook(_FakeRequest(
            json.dumps({"event_type": event_type}).encode(),
            headers={"X-Inkbox-Signature": "sha256=unchecked"},
        )))
        assert response.status == 401
    assert not adapter._enqueued


def test_control_scope_is_exact_channel_conversation_and_author(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        row = {"chat_id": "contact:shared"}
        active = {"route": {"mode": "whatsapp", "conversation_id": uid(30), "author": "+15555550101"}}
        value.queue.active[row["chat_id"]] = active
        try:
            for changes in ({"mode": "imessage"}, {"conversation_id": uid(31)}, {"author": "+15555550102"}):
                event = receipt(2, mode="whatsapp")
                event.metadata["inkbox_reply_route"].update(raw_text="yes", **changes)
                assert not await value.queue.control(event, row)
            assert "controls" not in row
        finally:
            value.queue.active.clear()
            await value.queue.close()
    asyncio.run(run())


def test_public_quota_errors_retain_only_policy_fields_and_retry_advice():
    from inkbox import InkboxAPIError
    from inkbox_plugin.whatsapp import public_quota_error
    error = InkboxAPIError(429, {"error": "whatsapp_rate_limit", "quota": "whatsapp.texts_per_agent_identity_per_24h", "limit": 20, "used": 20, "reset_at": "2026-10-27T00:00:00Z", "retry_condition": "Wait until reset", "message": "untrusted provider body", "credentials": "private"}, retry_after="60")
    assert public_quota_error(error) == {"error": "whatsapp_rate_limit", "quota": "whatsapp.texts_per_agent_identity_per_24h", "limit": 20, "used": 20, "reset_at": "2026-10-27T00:00:00Z", "retry_condition": "Wait until reset", "retry_after_seconds": 60}
    assert public_quota_error(InkboxAPIError(502, {"error": "upstream", "message": "private"})) == {}
