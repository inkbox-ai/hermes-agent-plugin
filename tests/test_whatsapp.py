"""Real-SDK WhatsApp wires and native receipt/media/permission ownership."""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import httpx
import pytest
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


def conversation_wire(conversation_id, **changes):
    return {"id": conversation_id, "agent_identity_id": uid(100),
        "local_number": "+15555550100", "remote_number": "+15555550102",
        "participants": ["+15555550102"], "is_group": False,
        "created_at": TIME, "updated_at": TIME, **changes}


@pytest.mark.parametrize("outcome", ["not_found", "timeout", "accepted"])
@pytest.mark.parametrize("explicit_key", [False, True])
def test_whatsapp_send_cannot_own_matching_imessage_turn(factory, tmp_path, monkeypatch, outcome, explicit_key):
    import hashlib
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    from inkbox_plugin.imessage_state import active_context
    from inkbox_plugin.native_turns import NativeTurns

    monkeypatch.setattr("inkbox._message_requests.time.sleep", lambda _: None)

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        whatsapp_identity = value.adapter._reply_identity
        # Keep the native iMessage final on its own transport.
        value.adapter._reply_identity = value.base.identity
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (
            NS(base_url=value.adapter._base_url), client, whatsapp_identity))
        if outcome == "not_found":
            client.whatsapp._http.post.side_effect = InkboxAPIError(404, "WhatsApp conversation not found")
        elif outcome == "timeout":
            client.whatsapp._http.post.side_effect = httpx.ReadTimeout("response lost")
        completed = []
        contexts = []
        payload = {"conversation_id": uid(30), "text": "Requested WhatsApp message"}

        async def handle(event):
            event._gateway_accepted = True
            await value.adapter.on_processing_start(event)
            session = str(event.source.chat_id)
            context = active_context(session)
            assert context["mode"] == "imessage"
            assert context["conversation_id"] == payload["conversation_id"]
            contexts.append(context)
            args = {**payload, **({"idempotency_key": "requested-message-1"} if explicit_key else {})}
            result = json.loads(invoke("send_whatsapp", args, session))
            key = "requested-message-1" if explicit_key else "hermes-wa-tool:" + hashlib.sha256(
                json.dumps([uid(100), event.message_id, payload], sort_keys=True).encode()).hexdigest()
            if outcome == "accepted":
                assert result["ok"]
                assert result["result"]["idempotency_key"] == key
            else:
                assert "error" in result and result["idempotency_key"] == key
                if outcome == "not_found":
                    assert result["status_code"] == 404
            calls = client.whatsapp._http.post.call_args_list
            assert len(calls) == (3 if outcome == "timeout" else 1)
            assert all(call.kwargs["json"] == payload for call in calls)
            assert all(call.kwargs["headers"]["Idempotency-Key"] == key for call in calls)
            value.queue.capture_result(event, "iMessage final")
            final = await value.adapter.send(event.source.chat_id, "iMessage final", reply_to=event.message_id)
            await value.adapter.on_processing_complete(event, "success" if final.success else "failure")
            completed.append(final)

        value.adapter.handle_message = handle
        try:
            await value.queue.accept(receipt(1, mode="imessage"))
            await settle(value.queue)
            assert len(completed) == 1 and completed[0].success
            value.base.identity.send_imessage.assert_called_once()
            sent = value.base.identity.send_imessage.call_args.kwargs
            assert sent["conversation_id"] == uid(30) and sent["text"] == "iMessage final"
            assert sent["reply_to_message_id"] == uid(1)
            context = contexts[0]
            assert context["whatsapp_send_attempt"] is None
            assert context["whatsapp_send_attempts"] == {}
            assert context["explicit_sends"] == [] and context["leases"] == 0
            for restarted in (False, True):
                if restarted:
                    await value.queue.close()
                    value.adapter._native_turns = value.queue = NativeTurns(value.adapter, tmp_path / "native")
                    await value.queue.start()
                    await settle(value.queue)
                row = next(iter(value.queue.rows.values()))
                turn = row["turns"][0]
                assert turn["state"] == "done" and not row.get("blocked")
                assert not {"whatsapp_send_attempt", "whatsapp_send_attempts", "explicit_delivery_state", "reply_suppressed"} & turn.keys()
                assert not turn.get("explicit_sends")
                value.base.identity.send_imessage.assert_called_once()
                assert len(completed) == 1
                assert client.whatsapp._http.post.call_count == (3 if outcome == "timeout" else 1)
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


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


def test_attachment_following_text_keeps_its_own_durable_turn(factory, tmp_path, monkeypatch):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.adapter._imessage_threaded_replies = False
        admitted = asyncio.Event()
        original_batch = value.queue._batch

        async def batch_after_admission(*args):
            await admitted.wait()
            return await original_batch(*args)

        monkeypatch.setattr(value.queue, "_batch", batch_after_admission)
        try:
            import inkbox_plugin.native_turns as native_module
            received_at = native_module.time.time()
            with monkeypatch.context() as patch:
                patch.setattr(native_module, "time", NS(time=lambda: received_at))
                assert (await receive(value.adapter, inbound(content="First question"))).status == 202
                assert (await receive(value.adapter, inbound(
                    id=uid(2), content="Attached question", media=[{**MEDIA, "url": "https://media.example/expired"}],
                ))).status == 202
            admitted.set()
            await settle(value.queue)
            assert len(value.inputs) == 2
            assert value.inputs[0].text.endswith("\nFirst question")
            assert value.inputs[1].text.endswith("\nAttached question")
            assert value.inputs[0].media_urls == []
            assert value.inputs[1].media_urls == [MEDIA["url"]]
            turns = next(iter(value.queue.rows.values()))["turns"]
            assert [turn["source_ids"] for turn in turns] == [[uid(1)], [uid(2)]]
            assert turns[1]["route"]["whatsapp_media_ids"] == [uid(801)]
            assert all(turn["state"] == "done" for turn in turns)
            client.whatsapp._http.get.assert_called_once()
        finally:
            admitted.set()
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_unresolved_group_wrong_identity_and_self_echo_do_not_admit(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        try:
            for changes in ({"is_group": True, "participants": []}, {"agent_identity_id": uid(999)}, {"sender_number": "+15555550100"}):
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
            value.queue._record_outbound({"chat_id": "whatsapp:example"},
                {"id": uid(1), "route": {"mode": "whatsapp", "conversation_id": uid(30)}}, NS(**WIRE))
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


def test_uncertain_tool_send_allows_only_read_only_recovery_for_its_active_owner(factory, tmp_path, monkeypatch):
    from inkbox_plugin import tools

    monkeypatch.setattr("inkbox._message_requests.time.sleep", lambda _: None)

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        identity = value.adapter._reply_identity
        monkeypatch.setattr(value.queue, "_kick", lambda _: None)
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, identity))
        client.whatsapp._http.post.side_effect = httpx.ReadTimeout("response lost")
        client.message_sends._http = Mock()
        client.message_sends._http.get.return_value = {"message_id": uid(800)}
        event = receipt(1, mode="whatsapp")
        await value.queue.accept(event)
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        turn["state"] = "running"
        value.queue.active[row["chat_id"]] = turn
        session = str(tmp_path / "lookup-session")
        bind_context(session, {
            **turn["route"], "turn_id": turn["id"], "identity_id": uid(100), "base_url": value.adapter._base_url,
            "authorize_whatsapp": lambda **kw: value.queue.check_whatsapp_tool_authority(row, turn, **kw),
            "explicit_delivery_state": lambda state: turn.update(explicit_delivery_state=state),
        })
        try:
            send = {"conversation_id": uid(30), "text": "Answer", "idempotency_key": "original-key"}
            unknown = json.loads(invoke("send_whatsapp", send, session))
            assert unknown["idempotency_key"] == "original-key"
            assert turn["explicit_delivery_state"] == "uncertain"
            attempts = client.whatsapp._http.post.call_count
            recovered = json.loads(invoke("lookup", {"idempotency_key": "original-key"}, session))
            assert recovered == {"ok": True, "result": {"message_id": uid(800)}}
            assert "error" in json.loads(invoke("send_whatsapp", send, session))
            assert client.whatsapp._http.post.call_count == attempts
            client.message_sends._http.get.assert_called_once()
            assert client.message_sends._http.get.call_args.kwargs["headers"] == {"Idempotency-Key": "original-key"}
            turn["state"] = "cancelled"
            assert "error" in json.loads(invoke("lookup", {"idempotency_key": "original-key"}, session))
            client.message_sends._http.get.assert_called_once()
        finally:
            clear_context(session, turn["id"])
            value.queue.active.clear()
            await value.queue.close()
            client.close()

    asyncio.run(run())


@pytest.mark.parametrize("recipient", ["+15555550101", "+1 (555) 555-0101", ["+15555550101"], ["+1 (555) 555-0101"], "+15555550102"])
@pytest.mark.parametrize("uncertain", [False, True, "unknown_403", "unknown_422", "unknown_429"])
def test_explicit_same_recipient_send_cannot_be_repeated_by_automatic_final(factory, tmp_path, monkeypatch, recipient, uncertain):
    from inkbox_plugin import tools

    monkeypatch.setattr("inkbox._message_requests.time.sleep", lambda _: None)

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        identity = value.adapter._reply_identity
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, identity))
        same_recipient = recipient != "+15555550102"
        attempts = 3 if uncertain is True or uncertain == "unknown_429" else 1
        if uncertain:
            from inkbox import InkboxAPIError
            error = (InkboxAPIError(int(uncertain.rsplit("_", 1)[1]), {"error": "whatsapp_send_pending"})
                     if isinstance(uncertain, str) else httpx.ReadTimeout("response lost"))
            client.whatsapp._http.post.side_effect = [error] * attempts + [{"message": WIRE}]

        async def handle(event):
            event._gateway_accepted = True
            await value.adapter.on_processing_start(event)
            invoke("send_whatsapp", {"to": recipient, "text": "Answer", "idempotency_key": "original-key"}, str(event.source.chat_id))
            value.queue.capture_result(event, "Answer")
            final = await value.adapter.send(event.source.chat_id, "Answer", reply_to=event.message_id)
            await value.adapter.on_processing_complete(event, "success" if final.success else "failure")

        value.adapter.handle_message = handle
        try:
            event = receipt(1, mode="whatsapp")
            event.metadata["inkbox_reply_route"].update(identity_id=uid(100), base_url=value.adapter._base_url)
            await value.queue.accept(event)
            await settle(value.queue)
            calls = client.whatsapp._http.post.call_args_list
            assert len(calls) == attempts + (0 if same_recipient or uncertain else 1)
            keys = {call.kwargs["headers"]["Idempotency-Key"] for call in calls}
            assert len(keys) == (1 if same_recipient or uncertain else 2)
            turn = next(iter(value.queue.rows.values()))["turns"][0]
            if same_recipient:
                assert turn["explicit_delivery_state"] == ("uncertain" if uncertain else "sent")
            else:
                assert turn["explicit_delivery_state"] == ("uncertain" if uncertain else "sent")
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_preconfigured_subscription_startup_initializes_whatsapp_without_other_channels(factory):
    async def run():
        value = factory("imessage")
        adapter = value.adapter
        client = real_identity(adapter)
        adapter._inkbox = client
        identity = adapter._reply_identity
        del adapter._reply_identity
        adapter._identity_id = None
        adapter._skip_webhook_reconcile = True
        adapter._slack_enabled = False
        adapter._imessage_threaded_replies = False
        adapter._public_url = "https://agent.example"
        adapter._webhook_path = "/webhooks/inkbox"
        adapter._companion_max_bytes = 128_000
        client.get_identity = Mock(return_value=identity)
        try:
            adapter._patch_identity_objects()
            await adapter._start_companion()
            assert adapter._reply_identity is identity
            assert adapter._identity_id == uid(100)
            assert adapter._native_turns is not None
            client.get_identity.assert_called_once_with("sample-agent")
            assert not client.whatsapp._http.post.called
        finally:
            if getattr(adapter, "_native_turns", None) is not None:
                await adapter._native_turns.close()
            if adapter._companion is not None:
                await adapter._companion.close()
            client.close()

    asyncio.run(run())


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
    error = InkboxAPIError(429, {"error": "whatsapp_daily_limit_exceeded", "quota": "whatsapp.texts_per_agent_identity_per_24h", "limit": 20, "used": 20, "reset_at": "2026-10-27T00:00:00Z", "retry_condition": "window_reset", "message": "untrusted provider body", "credentials": "private"}, retry_after="60")
    assert public_quota_error(error) == {"error": "whatsapp_daily_limit_exceeded", "quota": "whatsapp.texts_per_agent_identity_per_24h", "limit": 20, "used": 20, "reset_at": "2026-10-27T00:00:00Z", "retry_condition": "window_reset", "retry_after_seconds": 60}
    assert public_quota_error(InkboxAPIError(502, {"error": "upstream", "message": "private"})) == {}
    assert public_quota_error(InkboxAPIError(429, {"error": "whatsapp_send_pending", "message": "private"})) == {}


@pytest.mark.parametrize("status,code,known", [
    (422, "whatsapp_reply_required_for_email_addresses", True),
    (429, "whatsapp_reply_required_for_email_addresses", False),
    (422, "whatsapp_reply_required_for_unknown_content", False),
    (402, "whatsapp_cycle_limit_exceeded", False),
])
def test_email_policy_rejection_reuses_original_key_without_freeing_unknown_sends(factory, tmp_path, monkeypatch, status, code, known):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        monkeypatch.setattr(value.queue, "_kick", lambda _: None)
        monkeypatch.setattr("inkbox._message_requests.time.sleep", lambda _: None)
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
        await value.queue.accept(receipt(1, mode="whatsapp"))
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        turn["state"] = "running"
        value.queue.active[row["chat_id"]] = turn
        session = str(tmp_path / "email-policy")
        bind_context(session, {**turn["route"], "identity_id": uid(100), "base_url": value.adapter._base_url,
            "turn_id": turn["id"], "authorize_whatsapp": lambda **kw: value.queue.check_whatsapp_tool_authority(row, turn, **kw),
            "explicit_delivery_state": lambda state: turn.update(explicit_delivery_state=state)})
        policy = {"error": code, "quota": "whatsapp.pre_reply_email_addresses_allowed", "limit": 0,
                  "retry_condition": "recipient_reply_or_configuration"}
        client.whatsapp._http.post.side_effect = InkboxAPIError(status, {**policy, "message": "private-diagnostic"})
        args = {"conversation_id": uid(30), "text": "Email sender@example.com", "idempotency_key": "original-email-key"}
        try:
            first = json.loads(invoke("send_whatsapp", args, session))
            assert "private-diagnostic" not in json.dumps(first)
            assert turn["explicit_delivery_state"] == ("rejected" if known else "uncertain")
            if known:
                assert {key: first[key] for key in policy} == policy
            else:
                assert "not accepted" not in first.get("recovery", "").lower()
            attempts = client.whatsapp._http.post.call_count
            client.whatsapp._http.post.side_effect = None
            retried = json.loads(invoke("send_whatsapp", args, session))
            assert bool(retried.get("ok")) is known
            assert client.whatsapp._http.post.call_count == attempts + int(known)
            assert {call.kwargs["headers"]["Idempotency-Key"] for call in client.whatsapp._http.post.call_args_list} == {"original-email-key"}
        finally:
            clear_context(session, turn["id"])
            value.queue.active.clear()
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_group_receipts_isolate_conversations_and_authority_and_retain_the_actual_source(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        try:
            for number, conversation in ((1, uid(31)), (2, uid(32))):
                response = await receive(value.adapter, inbound(id=uid(number), conversation_id=conversation,
                    is_group=True, remote_number=None, participants=["+15555550101", "+15555550102"],
                    content="@sample-agent please review this"))
                assert response.status == 202
            await settle(value.queue)
            assert len(value.inputs) == 2
            assert len(value.queue.rows) == 2
            assert {event.source.chat_id for event in value.inputs} == {f"whatsapp:{uid(31)}", f"whatsapp:{uid(32)}"}
            assert all(event.source.chat_type == "group" and event.source.user_id_alt == "+15555550101" for event in value.inputs)
            assert all(event.source.thread_id == event.source.chat_id for event in value.inputs)
            assert all(row["turns"][0]["route"]["conversation_kind"] == "group" for row in value.queue.rows.values())
            calls = client.whatsapp._http.post.call_args_list
            assert {call.kwargs["json"]["reply_to_message_id"] for call in calls} == {uid(1), uid(2)}
            assert {row["turns"][0]["route"]["whatsapp_reply_target"] for row in value.queue.rows.values()} == {uid(1), uid(2)}
            assert {call.kwargs["json"]["conversation_id"] for call in calls} == {uid(31), uid(32)}
            assert all("plain_reply_fallback" not in call.kwargs["json"] for call in calls)
            assert all(call.kwargs["is_group"] is True for call in value.adapter._contact_memories.call_args_list)
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"participants": []}, {"participants": ["+15555550102", "+15555550103"]},
    {"participants": ["+15555550101", "+15555550101"]},
    {"participants": ["+15555550100", "+15555550101"]}, {"sender_number": "opaque-author"},
])
def test_group_without_resolved_remote_author_never_enters_native_permissions(factory, tmp_path, changes):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        try:
            assert (await receive(value.adapter, inbound(is_group=True, remote_number=None,
                **changes))).status == 200
            assert not value.inputs and not value.queue.rows
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_native_replies_quote_first_burst_source_without_moving_the_host_session(factory, tmp_path, monkeypatch):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        admitted = asyncio.Event()
        original_batch = value.queue._batch
        async def wait_batch(*args):
            await admitted.wait()
            return await original_batch(*args)
        monkeypatch.setattr(value.queue, "_batch", wait_batch)
        try:
            import inkbox_plugin.native_turns as native_module
            received_at = native_module.time.time()
            with monkeypatch.context() as patch:
                patch.setattr(native_module, "time", NS(time=lambda: received_at))
                assert (await receive(value.adapter, inbound(id=uid(1), content="First question"))).status == 202
                assert (await receive(value.adapter, inbound(id=uid(2), content="Follow-up question"))).status == 202
            admitted.set()
            await settle(value.queue)
            assert len(value.inputs) == 1
            route = next(iter(value.queue.rows.values()))["turns"][0]["route"]
            assert route["whatsapp_reply_target"] == uid(2)
            assert client.whatsapp._http.post.call_args.kwargs["json"]["reply_to_message_id"] == uid(1)
            assert value.inputs[0].source.thread_id == f"whatsapp:{uid(30)}"
        finally:
            admitted.set()
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_versioned_reaction_events_never_wake_model_and_cannot_regress_removal(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        item = {"id": uid(901), "target_message_id": uid(800), "conversation_id": uid(30),
            "agent_identity_id": uid(100), "direction": "inbound", "remote_number": None,
            "status": "removed", "revision": 2, "updated_at": "2026-10-01T12:02:00Z"}
        try:
            record_status(value.adapter, {"event_type": "whatsapp.reaction_received", "data": {"reaction": item}})
            record_status(value.adapter, {"event_type": "whatsapp.reaction_received", "data": {"reaction": {**item,
                "status": "applied", "revision": 1, "updated_at": TIME}}})
            saved = value.queue.delivery_status[f"whatsapp-reaction:{uid(901)}"]
            assert saved["status"] == "removed" and saved["remote_number"] is None
            assert not value.inputs and not client.whatsapp._http.post.called
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_group_explicit_direct_send_to_author_never_retargets_to_the_group(factory, tmp_path, monkeypatch):
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    identity = value.adapter._reply_identity
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, identity))
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": "group-turn", "conversation_kind": "group", "conversation_id": uid(30),
        "author": "+15555550101", "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock()})
    try:
        result = json.loads(invoke("send_whatsapp", {"to": "+15555550101", "text": "Private answer", "idempotency_key": "private-key"}, session))
        assert result["ok"]
        assert client.whatsapp._http.post.call_args.kwargs["json"] == {"to": "+15555550101", "text": "Private answer"}
    finally:
        clear_context(session, "group-turn")
        client.close()


def test_reaction_tools_keep_stable_keys_and_exact_receipts_and_allow_read_only_recovery(factory, tmp_path, monkeypatch):
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    identity = value.adapter._reply_identity
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, identity))
    item = {"id": uid(901), "target_message_id": uid(800), "conversation_id": uid(30), "agent_identity_id": uid(100),
        "direction": "outbound", "remote_number": "+15555550100", "reaction": "like", "custom_emoji": None,
        "emoji": "👍", "status": "uncertain", "revision": 1, "created_at": TIME, "updated_at": TIME, "removed_at": None}
    client.whatsapp._http.post.return_value = {"reaction": item}
    client.whatsapp._http.get.return_value = {"reaction": item}
    client.whatsapp._http.delete_with_response.return_value = {"reaction": {**item, "status": "superseded"}}
    guard = Mock()
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": "reaction-turn", "conversation_id": uid(30), "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": guard})
    try:
        result = json.loads(invoke("send_whatsapp_reaction", {"message_id": uid(800), "reaction": "like", "idempotency_key": "reaction-key"}, session))
        assert result["ok"] and result["result"]["reaction"]["status"] == "uncertain"
        assert client.whatsapp._http.post.call_args.kwargs["headers"] == {"Idempotency-Key": "reaction-key"}
        assert json.loads(invoke("get_whatsapp_reaction", {"reaction_id": uid(901)}, session))["ok"]
        guard.assert_called_with(read_only=True)
        removed = json.loads(invoke("remove_whatsapp_reaction", {"reaction_id": uid(901), "idempotency_key": "remove-key"}, session))
        assert removed["result"]["reaction"]["status"] == "superseded"
        assert client.whatsapp._http.delete_with_response.call_args.kwargs["headers"] == {"Idempotency-Key": "remove-key"}
    finally:
        clear_context(session, "reaction-turn")
        client.close()


def test_shared_group_quote_matrix_uses_real_sdk_and_rejects_before_io(factory, monkeypatch):
    from pathlib import Path
    from inkbox_plugin import tools
    fixture = json.loads((Path(__file__).parent / "fixtures" / "whatsapp-v2.json").read_text(encoding="utf-8"))
    value = factory("imessage")
    client = real_identity(value.adapter)
    identity = value.adapter._reply_identity
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, identity))
    for item in fixture["send_cases"]:
        before = client.whatsapp._http.post.call_count
        result = json.loads(invoke("send_whatsapp", {**item["body"], "idempotency_key": item["name"]}))
        if item["valid"]:
            assert result["ok"], item["name"]
            assert client.whatsapp._http.post.call_args.kwargs["json"] == item["body"]
            assert client.whatsapp._http.post.call_args.kwargs["headers"]["Idempotency-Key"] == item["name"]
        else:
            assert "error" in result, item["name"]
            assert client.whatsapp._http.post.call_count == before


def test_whatsapp_registration_exposes_group_quote_thread_and_reaction_contracts():
    from inkbox_plugin.whatsapp_tools import register
    ctx = Mock()
    register(ctx, lambda: True)
    tools = {call.args[0]: call.args[2] for call in ctx.register_tool.call_args_list}
    send = tools["inkbox_send_whatsapp"]["parameters"]
    assert send["additionalProperties"] is False
    assert send["properties"]["to"]["oneOf"][1]["maxItems"] == 8
    assert {"reply_to_message_id", "group_name"} <= set(send["properties"])
    assert "plain_reply_fallback" not in send["properties"]
    assert {"inkbox_get_whatsapp_thread", "inkbox_get_whatsapp_conversation_thread", "inkbox_send_whatsapp_reaction",
        "inkbox_get_whatsapp_reaction", "inkbox_remove_whatsapp_reaction"} <= set(tools)
    reaction = tools["inkbox_send_whatsapp_reaction"]["parameters"]
    assert "idempotency_key" in reaction["required"] and "part_index" not in reaction["properties"]
    assert reaction["properties"]["reaction"]["enum"] == ["love", "like", "dislike", "laugh", "emphasize", "question", "eyes"]


def test_quoted_inbound_dm_automatically_quotes_source_not_its_parent(factory, tmp_path):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        def source_reply(_path, **kwargs):
            assert kwargs["json"]["reply_to_message_id"] == uid(1)
            assert "plain_reply_fallback" not in kwargs["json"]
            return {"message": WIRE}
        client.whatsapp._http.post.side_effect = source_reply
        try:
            assert (await receive(value.adapter, inbound(reply_to_message_id=uid(3), thread_id=uid(3), thread_root_message_id=uid(3)))).status == 202
            await settle(value.queue)
            assert len(value.inputs) == 1 and client.whatsapp._http.post.call_count == 1
            assert next(iter(value.queue.rows.values()))["turns"][0]["state"] == "done"
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_explicit_native_quote_keeps_target_and_never_falls_back_after_rejection(factory, tmp_path, monkeypatch):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    identity = value.adapter._reply_identity
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, identity))
    client.whatsapp._http.post.side_effect = InkboxAPIError(422, {"error": "whatsapp_quoted_replies_not_supported"})
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": "quote-turn", "conversation_id": uid(30), "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock()})
    try:
        result = json.loads(invoke("send_whatsapp", {"conversation_id": uid(30), "reply_to_message_id": uid(1), "text": "Quoted", "idempotency_key": "quote-original"}, session))
        assert result["status_code"] == 422
        assert client.whatsapp._http.post.call_count == 1
        assert client.whatsapp._http.post.call_args.kwargs["json"]["reply_to_message_id"] == uid(1)
        assert client.whatsapp._http.post.call_args.kwargs["headers"]["Idempotency-Key"] == "quote-original"
    finally:
        clear_context(session, "quote-turn")
        client.close()


def peer_reaction(**changes):
    return {"id": "evt_peer_reaction", "event_type": "whatsapp.reaction_received", "timestamp": TIME,
        "data": {"reaction": {"id": uid(901), "target_message_id": uid(800), "conversation_id": uid(30), "agent_identity_id": uid(100),
            "direction": "inbound", "remote_number": "+15555550101", "reaction": "question", "custom_emoji": None, "emoji": "❓",
            "status": "applied", "revision": 1, "created_at": TIME, "updated_at": TIME, "removed_at": None, **changes}}}


def reaction_reads(client, *, current=None, **changes):
    conversation = {"id": uid(30), "agent_identity_id": uid(100), "local_number": "+15555550100", "remote_number": "+15555550101",
        "participants": ["+15555550101"], "is_group": False, "created_at": TIME, "updated_at": TIME, **changes}
    receipt_state = current or peer_reaction()["data"]["reaction"]
    client.whatsapp._http.get.side_effect = lambda path, **_kw: ({"reaction": receipt_state} if "/reactions/" in path else WIRE if "/messages/" in path else conversation)


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("stale_read", [False, True])
def test_suspended_reaction_cannot_follow_newer_removal(factory, tmp_path, monkeypatch, quiet, stale_read):
    from inkbox_plugin.whatsapp import receive_reaction

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        from inkbox_plugin.conversation import ConversationState
        value.adapter._conversation_journal = ConversationState(tmp_path / "conversations")
        options = {"is_group": True} if quiet else {}
        if quiet:
            value.adapter.config.extra["group_reply_mode"] = "mention"
        first = peer_reaction()
        removed = peer_reaction(status="removed", revision=2, updated_at="2026-10-01T12:01:00Z")
        reaction_reads(client, **options)
        paused, resume = asyncio.Event(), asyncio.Event()
        resolve = value.adapter._resolve_contact_full

        async def paused_contact(**kwargs):
            if asyncio.current_task() is older:
                paused.set()
                await resume.wait()
            return await resolve(**kwargs)

        monkeypatch.setattr(value.adapter, "_resolve_contact_full", paused_contact)
        older = asyncio.create_task(receive_reaction(value.adapter, first))
        try:
            # The applied receipt passed its read; contact lookup is the async gap.
            await asyncio.wait_for(paused.wait(), 2)
            reaction_reads(client, current=removed["data"]["reaction"], **options)
            assert (await receive_reaction(value.adapter, removed)).status == 202
            await settle(value.queue)
            if stale_read:
                reaction_reads(client, current=first["data"]["reaction"], **options)
            resume.set()
            assert (await asyncio.wait_for(older, 2)).status == 200
            await settle(value.queue)
            for replay in (first, removed):
                assert (await receive_reaction(value.adapter, replay)).status == 200
            if quiet:
                texts = [item["text"] for item in value.adapter._conversation_state().row(f"whatsapp:{uid(30)}")["quiet"]]
                assert not value.inputs and not value.queue.rows
            else:
                texts = [event.text for event in value.inputs]
                assert all(not row.get("blocked") for row in value.queue.rows.values())
            assert len(texts) == 1 and "was removed" in texts[0]
            assert value.queue.delivery_status[f"whatsapp-reaction:{uid(901)}"]["revision"] == 2

            # A later genuine add still works after stale and duplicate receipts.
            latest = peer_reaction(revision=3, updated_at="2026-10-01T12:02:00Z")
            reaction_reads(client, current=latest["data"]["reaction"], **options)
            assert (await receive_reaction(value.adapter, latest)).status == 202
            await settle(value.queue)
            texts = ([item["text"] for item in value.adapter._conversation_state().row(f"whatsapp:{uid(30)}")["quiet"]]
                if quiet else [event.text for event in value.inputs])
            assert len(texts) == 2 and "was added" in texts[-1]

            # Restart without process-local dedup: retained revisions still fence replay.
            from inkbox_plugin.native_turns import NativeTurns
            await value.queue.close()
            value.adapter._seen_request_ids.clear()
            value.queue = value.adapter._native_turns = NativeTurns(value.adapter, tmp_path / "native")
            await value.queue.start()
            reads = client.whatsapp._http.get.call_count
            for replay in (first, removed):
                assert (await receive_reaction(value.adapter, replay)).status == 200
            await settle(value.queue)
            assert client.whatsapp._http.get.call_count == reads
            assert len(value.inputs) == (0 if quiet else 2)
        finally:
            resume.set()
            await asyncio.gather(older, return_exceptions=True)
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_reaction_final_read_observes_removal_without_callback(factory, tmp_path, monkeypatch):
    from inkbox_plugin.whatsapp import receive_reaction

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        reaction_reads(client)
        removed = peer_reaction(status="removed", revision=2, updated_at="2026-10-01T12:01:00Z")
        resolve = value.adapter._resolve_contact_full

        async def removal_during_contact_read(**kwargs):
            result = await resolve(**kwargs)
            reaction_reads(client, current=removed["data"]["reaction"])
            return result

        monkeypatch.setattr(value.adapter, "_resolve_contact_full", removal_during_contact_read)
        try:
            assert (await receive_reaction(value.adapter, peer_reaction())).status == 200
            assert not value.inputs and not value.queue.rows and not value.queue.delivery_status
            assert (await receive_reaction(value.adapter, removed)).status == 202
            await settle(value.queue)
            assert len(value.inputs) == 1 and "was removed" in value.inputs[0].text
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


@pytest.mark.parametrize("independent", ["actor", "target"])
def test_reaction_admission_serializes_only_matching_actor_target(factory, tmp_path, monkeypatch, independent):
    from inkbox_plugin.whatsapp import receive_reaction

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        first = peer_reaction()
        removed = peer_reaction(status="removed", revision=2, updated_at="2026-10-01T12:01:00Z")
        other = peer_reaction(id=uid(902), **({"remote_number": "+15555550102"} if independent == "actor"
            else {"target_message_id": uid(801)}))
        reaction_reads(client, is_group=True, participants=["+15555550101", "+15555550102"])
        read = client.whatsapp._http.get.side_effect
        current = removed["data"]["reaction"]

        def reads(path, **kwargs):
            if "/reactions/" in path:
                return {"reaction": other["data"]["reaction"] if path.endswith(uid(902)) else current}
            if "/messages/" in path:
                return {**WIRE, "id": path.rsplit("/", 1)[-1]}
            return read(path, **kwargs)

        client.whatsapp._http.get.side_effect = reads
        value.adapter._message_handler.__self__._is_user_authorized = lambda source: True
        entered, resume, resolved = asyncio.Event(), asyncio.Event(), asyncio.Event()
        accept = value.adapter._accept_native_imessage
        resolve = value.adapter._resolve_contact_full

        async def paused_admission(event):
            if event.raw_message is removed:
                entered.set()
                await resume.wait()
            return await accept(event)

        async def contact(**kwargs):
            result = await resolve(**kwargs)
            resolved.set()
            return result

        monkeypatch.setattr(value.adapter, "_accept_native_imessage", paused_admission)
        newer = asyncio.create_task(receive_reaction(value.adapter, removed))
        older = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            # Even a lagging receipt read must not bypass an in-flight admission.
            current = first["data"]["reaction"]
            monkeypatch.setattr(value.adapter, "_resolve_contact_full", contact)
            older = asyncio.create_task(receive_reaction(value.adapter, first))
            await asyncio.wait_for(resolved.wait(), 2)
            assert (await asyncio.wait_for(receive_reaction(value.adapter, other), 2)).status == 202
            assert not older.done()
            resume.set()
            assert (await asyncio.wait_for(newer, 2)).status == 202
            assert (await asyncio.wait_for(older, 2)).status == 200
            await settle(value.queue)
            assert len(value.inputs) == 2
            related = [event for event in value.inputs if event.metadata["inkbox_reply_route"]["reaction_id"] == uid(901)]
            assert len(related) == 1 and "was removed" in related[0].text
            assert not value.adapter._whatsapp_reaction_admission_locks
        finally:
            resume.set()
            await asyncio.gather(*[task for task in (newer, older) if task is not None], return_exceptions=True)
            await value.queue.close()
            client.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["read", "cancel"])
def test_reaction_admission_failure_releases_reservation_for_replay(factory, tmp_path, monkeypatch, failure):
    from inkbox_plugin.whatsapp import receive_reaction

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        reaction_reads(client)
        read = client.whatsapp._http.get.side_effect
        reaction_read_count = 0

        def failing_final_read(path, **kwargs):
            nonlocal reaction_read_count
            if "/reactions/" in path:
                reaction_read_count += 1
                if reaction_read_count == 2:
                    raise TimeoutError("receipt read unavailable")
            return read(path, **kwargs)

        entered, resume = asyncio.Event(), asyncio.Event()
        accept = value.adapter._accept_native_imessage

        async def paused_admission(event):
            entered.set()
            await resume.wait()
            return await accept(event)

        task = None
        try:
            if failure == "read":
                client.whatsapp._http.get.side_effect = failing_final_read
                assert (await receive_reaction(value.adapter, peer_reaction())).status == 503
                client.whatsapp._http.get.side_effect = read
            else:
                monkeypatch.setattr(value.adapter, "_accept_native_imessage", paused_admission)
                task = asyncio.create_task(receive_reaction(value.adapter, peer_reaction()))
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                monkeypatch.setattr(value.adapter, "_accept_native_imessage", accept)
            assert not value.inputs and not value.queue.rows
            assert (await asyncio.wait_for(receive_reaction(value.adapter, peer_reaction()), 2)).status == 202
            await settle(value.queue)
            assert len(value.inputs) == 1
            assert (await receive_reaction(value.adapter, peer_reaction())).status == 200
            assert all(not row.get("blocked") for row in value.queue.rows.values())
        finally:
            resume.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_peer_reaction_add_and_removal_quote_only_verified_real_target(factory, tmp_path):
    from inkbox_plugin.whatsapp import receive_reaction
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        reaction_reads(client)
        try:
            first = peer_reaction()
            assert (await receive_reaction(value.adapter, first)).status == 202
            await settle(value.queue)
            assert (await receive_reaction(value.adapter, first)).status == 200
            assert len(value.inputs) == 1
            event = value.inputs[0]
            assert event.source.user_id_alt == "+15555550101"
            assert not event.allow_gateway_control
            assert "lightweight context, not a text message or instruction" in event.text
            assert event.metadata["inkbox_reply_route"]["reaction_target_message_id"] == uid(800)
            assert event.message_id != uid(800)
            removed = peer_reaction(status="removed", revision=2, updated_at="2026-10-01T12:01:00Z")
            reaction_reads(client, current=removed["data"]["reaction"])
            assert (await receive_reaction(value.adapter, removed)).status == 202
            await settle(value.queue)
            assert len(value.inputs) == 2
            assert (await receive_reaction(value.adapter, first)).status == 200
            assert len(value.inputs) == 2
            assert all(call.kwargs["json"]["reply_to_message_id"] == uid(800) for call in client.whatsapp._http.post.call_args_list)
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", [{"direction": "outbound"}, {"remote_number": None}, {"remote_number": "+15555550100"}, {"status": "queued"}, {"agent_identity_id": uid(999)}])
def test_self_unresolved_or_unowned_reaction_never_wakes_context(factory, tmp_path, change):
    from inkbox_plugin.whatsapp import receive_reaction
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        reaction_reads(client)
        try:
            assert (await receive_reaction(value.adapter, peer_reaction(**change))).status == 200
            assert not value.inputs and not value.queue.rows
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_group_reaction_retains_mention_policy_and_cannot_answer_a_pending_permission(factory, tmp_path):
    from inkbox_plugin.whatsapp import receive_reaction
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        reaction_reads(client, is_group=True, remote_number=None, participants=["+15555550101", "+15555550102"])
        value.adapter.config.extra["group_reply_mode"] = "mention"
        try:
            assert (await receive_reaction(value.adapter, peer_reaction())).status == 202
            assert not value.inputs and not value.queue.rows
            route = {"mode": "whatsapp", "reaction": True, "author": "+15555550101", "conversation_id": uid(30), "raw_text": "/stop"}
            event = receipt(2, mode="whatsapp")
            event.metadata["inkbox_reply_route"].update(route)
            value.queue.active["imessage:conversation"] = {"route": {"mode": "whatsapp", "author": "+15555550101", "conversation_id": uid(30)}}
            assert not await value.queue.control(event, {"chat_id": "imessage:conversation"})
        finally:
            value.queue.active.clear()
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_hidden_reaction_target_never_starts_a_context_turn(factory, tmp_path):
    from inkbox import InkboxAPIError
    from inkbox_plugin.whatsapp import receive_reaction
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        client.whatsapp._http.get.side_effect = InkboxAPIError(404, {"error": "not_found"})
        try:
            assert (await receive_reaction(value.adapter, peer_reaction())).status == 200
            assert not value.inputs and not value.queue.rows
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_old_api_reaction_event_rejection_preserves_legacy_direct_notifications():
    from inkbox import InkboxAPIError
    from inkbox_plugin.whatsapp import EVENTS, reconcile_notifications
    unsupported = InkboxAPIError(422, {"error": "whatsapp event unsupported"})
    reconcile = Mock(side_effect=[unsupported, None])
    assert reconcile_notifications(reconcile)
    assert reconcile.call_args_list[0].args[0] == EVENTS
    assert reconcile.call_args_list[1].args[0] == tuple(event for event in EVENTS if event != "whatsapp.reaction_received")
    denied = Mock(side_effect=InkboxAPIError(403, {"error": "permission denied"}))
    with pytest.raises(InkboxAPIError):
        reconcile_notifications(denied)
    assert denied.call_count == 1


@pytest.mark.parametrize("change", [{"revision": 2}, {"status": "removed"}, {"target_message_id": uid(999)},
    {"remote_number": "+15555550102"}, {"agent_identity_id": uid(999)}, {"conversation_id": uid(999)}])
def test_stale_or_mismatched_server_reaction_receipt_never_wakes_context(factory, tmp_path, change):
    from inkbox_plugin.whatsapp import receive_reaction
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        reaction_reads(client, current=peer_reaction(**change)["data"]["reaction"])
        try:
            assert (await receive_reaction(value.adapter, peer_reaction())).status == 200
            assert not value.inputs and not value.queue.rows
            assert client.whatsapp._http.get.call_count == 1
            assert client.whatsapp._http.get.call_args.kwargs["params"] == {"agent_identity_id": uid(100)}
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("args", [
    {"to": "+15555550101", "reply_to_message_id": uid(1)},
    {"conversation_id": uid(30), "reply_to_message_id": ""},
    {"to": ["+15555550101"], "group_name": "Invalid single"},
    {"conversation_id": uid(30), "group_name": "Not creation"},
    {"to": ["+15555550101", "+15555550101"]},
    {"to": "+15555550101", "text": "x" * 4001},
])
def test_local_contract_rejection_does_not_fence_corrected_explicit_send(factory, tmp_path, monkeypatch, args):
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    identity = value.adapter._reply_identity
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, identity))
    session = str(tmp_path)
    states = []
    bind_context(session, {"mode": "whatsapp", "turn_id": "validation", "conversation_id": uid(30), "author": "+15555550101",
        "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock(), "explicit_delivery_state": states.append})
    try:
        result = json.loads(invoke("send_whatsapp", {"text": "Answer", **args, "idempotency_key": "same-key"}, session))
        assert "error" in result and not states and not client.whatsapp._http.post.called
        assert json.loads(invoke("send_whatsapp", {"conversation_id": uid(30), "text": "Answer", "idempotency_key": "same-key"}, session))["ok"]
        assert states == ["sending", "sent"]
        client.whatsapp._http.post.assert_called_once()
    finally:
        clear_context(session, "validation")
        client.close()


@pytest.mark.parametrize("code", ["whatsapp_capability_not_ready", "whatsapp_quote_unavailable"])
def test_definite_capability_denial_retains_exact_key_without_uncertain_fence(factory, tmp_path, monkeypatch, code):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    session = str(tmp_path)
    states = []
    bind_context(session, {"mode": "whatsapp", "turn_id": "denial", "conversation_id": uid(30), "author": "+15555550101",
        "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock(), "explicit_delivery_state": states.append})
    args = {"conversation_id": uid(30), "text": "Answer", "reply_to_message_id": uid(1), "idempotency_key": "exact-key"}
    try:
        client.whatsapp._http.post.side_effect = InkboxAPIError(409, {"error": code})
        first = json.loads(invoke("send_whatsapp", args, session))
        assert first["idempotency_key"] == "exact-key" and "not accepted" in first["recovery"]
        assert states == ["sending", "rejected"]
        client.whatsapp._http.post.side_effect = None
        assert json.loads(invoke("send_whatsapp", args, session))["ok"]
        assert all(call.kwargs["headers"]["Idempotency-Key"] == "exact-key" for call in client.whatsapp._http.post.call_args_list)
        assert all(call.kwargs["json"]["reply_to_message_id"] == uid(1) for call in client.whatsapp._http.post.call_args_list)
    finally:
        clear_context(session, "denial")
        client.close()


def test_unowned_status_does_not_grow_checkpoint_and_reaction_facts_are_bounded(factory, tmp_path):
    from inkbox_plugin.whatsapp import record_reaction
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        try:
            record_status(value.adapter, {"event_type": "whatsapp.sent", "data": {"message": WIRE}})
            assert not value.queue.delivery_status
            for number in range(260):
                record_reaction(value.adapter, peer_reaction(id=uid(1000+number)))
            assert len(value.queue.delivery_status) == 256
            assert not value.inputs
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("first_mode", ["imessage", "whatsapp"])
def test_same_contact_native_channels_never_clobber_the_active_turn(factory, tmp_path, first_mode):
    async def run():
        gate = asyncio.Event()
        value = await harness(factory, tmp_path, gate=gate)
        value.base.identity.send_whatsapp = Mock(return_value=NS(id=uid(801)))
        first = receipt(1, mode=first_mode)
        second = receipt(2, mode="whatsapp" if first_mode == "imessage" else "imessage")
        first.source.thread_id = first_mode + ":" + uid(30)
        second.source.thread_id = ("whatsapp" if first_mode == "imessage" else "imessage") + ":" + uid(30)
        try:
            await value.queue.accept(first)
            for _ in range(100):
                if value.inputs:
                    break
                await asyncio.sleep(.01)
            assert len(value.inputs) == 1
            await value.queue.accept(second)
            await asyncio.sleep(.08)
            assert len(value.inputs) == 1
            assert value.queue.active[first.source.chat_id]["source_ids"] == [uid(1)]
            gate.set()
            await settle(value.queue)
            assert len(value.inputs) == 2
            assert all(row["turns"][0]["state"] == "done" and not row.get("blocked") for row in value.queue.rows.values())
            assert not value.queue.active
        finally:
            gate.set()
            await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("active", [False, True])
def test_group_stop_preserves_other_authors_pending_work(factory, tmp_path, monkeypatch, active):
    from unittest.mock import AsyncMock
    async def run():
        value = await harness(factory, tmp_path)
        value.base.host._is_user_authorized = lambda _: True
        monkeypatch.setattr(value.queue, "_kick", lambda _: None)
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", AsyncMock(return_value=True))
        value.queue._forward_control = AsyncMock()
        alice = receipt(1, mode="whatsapp")
        bob = receipt(2, mode="whatsapp", sender="+15555550102")
        another_alice = receipt(3, mode="whatsapp")
        try:
            for event in [alice, bob, another_alice]:
                await value.queue.accept(event)
            row = next(iter(value.queue.rows.values()))
            if active:
                row["turns"][0]["state"] = "running"
                value.queue.active[row["chat_id"]] = row["turns"][0]
            stop = receipt(4, mode="whatsapp")
            stop.metadata["inkbox_reply_route"]["raw_text"] = "/stop"
            await value.queue.accept(stop)
            assert row["turns"][1]["state"] == "pending"
            assert row["turns"][2]["state"] == "cancelled"
            if active:
                assert row["turns"][0]["state"] == "cancelled"
            assert value.queue._forward_control.await_count == 1
        finally:
            value.queue.active.clear()
            await value.queue.close()
    asyncio.run(run())


@pytest.mark.parametrize("state", ["pending", "completed", "submitted"])
def test_startup_preserves_legacy_companion_namespace_turn_ids_and_delivery_proof(factory, tmp_path, monkeypatch, state):
    import hashlib
    from inkbox_plugin.companion import CompanionReceiver
    from tests.test_companion import event
    async def run():
        value = factory("imessage")
        adapter = value.adapter
        adapter._base_url = "https://inkbox.ai"
        adapter._identity_id = None
        adapter._slack_enabled = False
        adapter._imessage_threaded_replies = False
        adapter._companion_max_bytes = 128_000
        state_path = tmp_path / "state.json"
        monkeypatch.setattr("inkbox_plugin.adapter._inkbox_state_path", lambda: state_path)
        monkeypatch.setattr(CompanionReceiver, "_kick", lambda *_: None)
        legacy_key = hashlib.sha256(json.dumps([adapter._base_url, adapter._identity_handle, None]).encode()).hexdigest()
        legacy_root = tmp_path / "inkbox_companion" / legacy_key
        old = CompanionReceiver(adapter, legacy_root)
        envelope = event("imessage", phase="ordinary")
        await old.accept(envelope)
        row = next(iter(old.rows.values()))
        row["turns"][0]["state"] = state
        if state == "completed":
            row["turns"][0]["delivery"] = {"state": "sent", "message_id": uid(800)}
        turn_id = row["turns"][0]["id"]
        old._save(row)
        await old.close()
        value.identity.id = uid(100)
        adapter._reply_identity = value.identity
        try:
            await adapter._start_companion()
            current = adapter._companion
            assert current.root == legacy_root and current.namespace_identity_id is None
            recovered = next(iter(current.rows.values()))
            assert recovered["turns"][0]["id"] == turn_id
            assert recovered["turns"][0]["state"] == state
            if state == "submitted":
                assert recovered["state"] == "paused"
            if state == "completed":
                assert recovered["turns"][0]["delivery"]["message_id"] == uid(800)
            await current.accept(envelope)
            assert len(current.rows) == 1 and len(recovered["turns"]) == 1
            assert not value.inputs
        finally:
            await adapter._companion.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation,args,path", [
    ("list_whatsapp_conversations", {"include_groups": True, "limit": 2}, "/conversations"),
    ("get_whatsapp_conversation", {"conversation_id": uid(30)}, "/conversations/" + uid(30)),
    ("list_whatsapp_messages", {"conversation_id": uid(30), "thread_id": uid(40), "include_groups": True, "limit": 2}, "/messages"),
    ("get_whatsapp_message", {"message_id": uid(800)}, "/messages/" + uid(800)),
    ("get_whatsapp_thread", {"message_id": uid(800), "limit": 2, "cursor": "opaque+/="}, "/messages/" + uid(800) + "/thread"),
    ("get_whatsapp_conversation_thread", {"conversation_id": uid(30), "thread_id": uid(40), "limit": 2, "cursor": "opaque+/="}, "/conversations/" + uid(30) + "/threads/" + uid(40)),
    ("get_whatsapp_limits", {}, "/limits"),
    ("mark_whatsapp_read", {"conversation_id": uid(30), "through_message_id": uid(800)}, "/mark-read"),
    ("get_whatsapp_status", {}, "/limits"),
])
def test_read_and_mark_tools_use_real_sdk_signatures_and_identity_scope(factory, tmp_path, monkeypatch, operation, args, path):
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    conversation = {"id": uid(30), "agent_identity_id": uid(100), "local_number": "+15555550100", "remote_number": "+15555550101",
        "participants": ["+15555550101"], "is_group": False, "created_at": TIME, "updated_at": TIME,
        "latest_text": None, "latest_message_at": None, "latest_direction": None, "latest_has_media": False, "unread_count": 0, "total_count": 1}
    thread = {"conversation_id": uid(30), "thread_id": uid(40), "thread_root_message_id": uid(800), "messages": [WIRE], "next_cursor": None}
    limits = {"agent_identity_id": uid(100), "limits": {}, "usage": {}}
    def response(url, **_kw):
        if url == "/limits":
            return limits
        if "/thread" in url:
            return thread
        if url == "/conversations":
            return [conversation]
        if url.startswith("/conversations/"):
            return conversation
        return [WIRE] if url == "/messages" else WIRE
    client.whatsapp._http.get.side_effect = response
    client.whatsapp._http.post.return_value = {"conversation_id": uid(30), "updated_count": 1}
    client._ids_resource._http.get.return_value = {"whatsapp": {"enabled": True, "number": "+15555550100", "status": "ready"}}
    try:
        result = json.loads(invoke(operation, args))
        assert result["ok"], result
        if operation == "get_whatsapp_status":
            assert result["result"]["status"]["status"] == "ready"
        call = client.whatsapp._http.post.call_args if operation == "mark_whatsapp_read" else client.whatsapp._http.get.call_args
        assert call.args[0] == path
        assert call.kwargs["params"]["agent_identity_id"] == uid(100)
        if "cursor" in args:
            assert call.kwargs["params"]["cursor"] == "opaque+/="
        if "include_groups" in args:
            assert call.kwargs["params"]["include_groups"] is True
        if operation == "mark_whatsapp_read":
            assert call.kwargs["json"] == args
    finally:
        client.close()


@pytest.mark.parametrize("saved_key", [False, True])
def test_legacy_checkpoint_restart_preserves_plain_body_and_unicode_key(factory, tmp_path, monkeypatch, saved_key):
    from inkbox_plugin.companion import digest
    from inkbox_plugin.native_turns import NativeTurns

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        monkeypatch.setattr(value.queue, "_kick", lambda _: None)
        await receive(value.adapter, inbound())
        row = next(iter(value.queue.rows.values()))
        turn = row["turns"][0]
        turn["route"].pop("whatsapp_auto_reply_target")
        turn["event"]["metadata"]["inkbox_reply_route"].pop("whatsapp_auto_reply_target")
        answer = "A café reply 🐳"
        turn.update(state="answer_ready", answer=answer)
        original_key = "hermes:" + digest(json.dumps([turn["id"], uid(30), None, answer]))
        if saved_key:
            turn["reply_idempotency_key"] = original_key
        value.queue._save(row)
        await value.queue.close()
        recovered = NativeTurns(value.adapter, tmp_path / "native")
        value.adapter._native_turns = recovered
        try:
            await recovered.start()
            await settle(recovered)
            assert not value.inputs
            call = client.whatsapp._http.post.call_args
            assert call.kwargs["json"] == {"conversation_id": uid(30), "text": answer}
            assert call.kwargs["headers"]["Idempotency-Key"] == original_key
            assert next(iter(recovered.rows.values()))["turns"][0]["state"] == "done"
        finally:
            await recovered.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("selector", [{}, {"conversation_id": uid(30)}, {"conversation_id": uid(31)}, {"to": "+15555550102"}])
def test_active_tool_defaults_and_outreach_destinations(factory, tmp_path, monkeypatch, group, selector):
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": uid(1), "conversation_id": uid(30), "author": "+15555550101",
        "conversation_kind": "group" if group else "direct", "whatsapp_auto_reply_target": uid(1),
        "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock()})
    try:
        assert json.loads(invoke("send_whatsapp", {**selector, "text": "Answer"}, session))["ok"]
        payload = client.whatsapp._http.post.call_args.kwargs["json"]
        expected = {**(selector or {"conversation_id": uid(30)}), "text": "Answer"}
        if not selector or selector.get("conversation_id") == uid(30):
            expected["reply_to_message_id"] = uid(1)
        assert payload == expected
    finally:
        clear_context(session, uid(1))
        client.close()


@pytest.mark.parametrize("recovery", [False, True])
def test_active_tool_preserves_explicit_quote_or_legacy_recovery_key(factory, tmp_path, monkeypatch, recovery):
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": uid(1), "conversation_id": uid(30), "whatsapp_auto_reply_target": uid(1),
        "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock()})
    args = {"conversation_id": uid(30), "text": "Answer", **({"idempotency_key": "old-plain-key"} if recovery else {"reply_to_message_id": uid(2)})}
    try:
        assert json.loads(invoke("send_whatsapp", args, session))["ok"]
        payload = client.whatsapp._http.post.call_args.kwargs["json"]
        assert payload.get("reply_to_message_id") == (None if recovery else uid(2))
        if recovery:
            assert client.whatsapp._http.post.call_args.kwargs["headers"]["Idempotency-Key"] == "old-plain-key"
    finally:
        clear_context(session, uid(1))
        client.close()


@pytest.mark.parametrize("failure", [None, "rejected", "uncertain"])
def test_source_tool_quote_suppresses_final_and_never_retries_plain(factory, tmp_path, monkeypatch, failure):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
        if failure:
            client.whatsapp._http.post.side_effect = InkboxAPIError(409 if failure == "rejected" else 403,
                {"error": "whatsapp_quote_unavailable" if failure == "rejected" else "unknown"})

        async def handle(event):
            event._gateway_accepted = True
            await value.adapter.on_processing_start(event)
            session = str(event.source.chat_id)
            invoke("send_whatsapp", {"text": "Answer", "reply_to_message_id": uid(2)}, session)
            if failure:
                # A changed plain request must not replace the quoted attempt.
                result = json.loads(invoke("send_whatsapp", {"text": "Replacement", "idempotency_key": "new-key"}, session))
                assert "error" in result
            value.queue.capture_result(event, "Answer")
            result = await value.adapter.send(event.source.chat_id, "Answer", reply_to=event.message_id)
            await value.adapter.on_processing_complete(event, "success" if result.success else "failure")
        value.adapter.handle_message = handle
        try:
            await receive(value.adapter, inbound())
            await settle(value.queue)
            client.whatsapp._http.post.assert_called_once()
            assert client.whatsapp._http.post.call_args.kwargs["json"]["reply_to_message_id"] == uid(2)
            turn = next(iter(value.queue.rows.values()))["turns"][0]
            attempt = turn["whatsapp_send_attempt"]
            assert attempt["payload"]["reply_to_message_id"] == uid(2)
            assert attempt["state"] == (failure or "sent")
            assert attempt["key"] == client.whatsapp._http.post.call_args.kwargs["headers"]["Idempotency-Key"]
            await value.queue.close()
            from inkbox_plugin.native_turns import NativeTurns
            value.queue = NativeTurns(value.adapter, tmp_path / "native")
            value.adapter._native_turns = value.queue
            await value.queue.start()
            await settle(value.queue)
            assert next(iter(value.queue.rows.values()))["turns"][0]["whatsapp_send_attempt"] == attempt
            client.whatsapp._http.post.assert_called_once()
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("legacy_media", [False, True])
def test_media_quote_options_are_frozen_before_upload(factory, tmp_path, legacy_media):
    import hashlib

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        try:
            await receive(value.adapter, inbound())
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            turn = row["turns"][0]
            path = tmp_path / "photo.png"
            path.write_bytes(b"png")
            value.adapter.validate_media_delivery_path = lambda path: path
            fingerprint = hashlib.sha256(json.dumps([str(path), None, "Photo"]).encode()).hexdigest()
            if legacy_media:
                turn["media_deliveries"] = {fingerprint: {"state": "uploaded", "media_id": uid(801)}}
            client.whatsapp._http.post.reset_mock()
            result = await send_media(value.adapter, row["chat_id"], path, "Photo", {"inkbox_reply_route": turn["route"]})
            assert result.success
            payload = client.whatsapp._http.post.call_args.kwargs["json"]
            assert payload.get("reply_to_message_id") == (None if legacy_media else uid(1))
            assert "plain_reply_fallback" not in payload
            if legacy_media:
                client.whatsapp._http.post_multipart.assert_not_called()
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("status", [403, 409, 422])
def test_automatic_quote_rejection_never_downgrades_or_replays(factory, tmp_path, status):
    from inkbox import InkboxAPIError

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        client.whatsapp._http.post.side_effect = InkboxAPIError(status, {"error": "whatsapp_quote_unavailable"})
        try:
            await receive(value.adapter, inbound())
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            turn = row["turns"][0]
            assert turn["state"] == "uncertain"
            assert not (await value.queue._send(row, turn, "Replacement")).success
            client.whatsapp._http.post.assert_called_once()
            assert client.whatsapp._http.post.call_args.kwargs["json"]["reply_to_message_id"] == uid(1)
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("revocation", ["sender", "identity", "endpoint"])
def test_autoquote_rechecks_source_authority_before_sending(factory, tmp_path, monkeypatch, revocation):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        monkeypatch.setattr(value.queue, "_kick", lambda _: None)
        try:
            await receive(value.adapter, inbound())
            row = next(iter(value.queue.rows.values()))
            turn = row["turns"][0]
            if revocation == "sender":
                value.base.host.denied.add(turn["event"]["source"]["user_id"])
            elif revocation == "identity":
                turn["route"]["identity_id"] = uid(999)
            else:
                turn["route"]["base_url"] = "https://other.example"
            assert not (await value.queue._send(row, turn, "Answer")).success
            client.whatsapp._http.post.assert_not_called()
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_autoquoted_tool_exact_recovery_keeps_target_and_original_key(factory, tmp_path, monkeypatch):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    value = factory("imessage")
    client = real_identity(value.adapter)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": uid(1), "conversation_id": uid(30), "whatsapp_auto_reply_target": uid(1),
        "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock()})
    try:
        client.whatsapp._http.post.side_effect = InkboxAPIError(409, {"error": "whatsapp_quote_unavailable"})
        result = json.loads(invoke("send_whatsapp", {"text": "Answer"}, session))
        client.whatsapp._http.post.side_effect = None
        assert json.loads(invoke("send_whatsapp", {"text": "Answer", "idempotency_key": result["idempotency_key"]}, session))["ok"]
        calls = client.whatsapp._http.post.call_args_list
        assert len(calls) == 2 and calls[0] == calls[1]
        assert calls[0].kwargs["json"]["reply_to_message_id"] == uid(1)
    finally:
        clear_context(session, uid(1))
        client.close()


@pytest.mark.parametrize("restart", [False, True])
def test_definite_quoted_rejection_settles_turn_and_releases_successor(factory, tmp_path, monkeypatch, restart):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    from inkbox_plugin.native_turns import NativeTurns

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
        handled = []

        async def handle(event):
            event._gateway_accepted = True
            handled.append(event.message_id)
            await value.adapter.on_processing_start(event)
            if event.message_id == uid(1):
                client.whatsapp._http.post.side_effect = InkboxAPIError(409, {"error": "whatsapp_quote_unavailable"})
                assert "error" in json.loads(invoke("send_whatsapp", {"text": "Quoted", "reply_to_message_id": uid(2)}, str(event.source.chat_id)))
                client.whatsapp._http.post.side_effect = None
            value.queue.capture_result(event, "Final answer")
            result = await value.adapter.send(event.source.chat_id, "Final answer", reply_to=event.message_id)
            await value.adapter.on_processing_complete(event, "success" if result.success else "failure")

        value.adapter.handle_message = handle
        try:
            await receive(value.adapter, inbound())
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            rejected = row["turns"][0]
            original_attempt = json.loads(json.dumps(rejected["whatsapp_send_attempt"]))
            assert rejected["state"] == "done" and not row.get("blocked")
            assert original_attempt["state"] == "rejected"
            assert client.whatsapp._http.post.call_count == 1
            if restart:
                await value.queue.close()
                value.queue = NativeTurns(value.adapter, tmp_path / "native")
                value.adapter._native_turns = value.queue
                await value.queue.start()
                await settle(value.queue)
            await receive(value.adapter, {**inbound(id=uid(3)), "id": "successor-event"})
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            assert handled == [uid(1), uid(3)]
            assert all(turn["state"] == "done" for turn in row["turns"])
            assert row["turns"][0]["whatsapp_send_attempt"] == original_attempt
            assert not row.get("blocked") and client.whatsapp._http.post.call_count == 2
            assert client.whatsapp._http.post.call_args.kwargs["json"]["reply_to_message_id"] == uid(3)
            assert client.whatsapp._http.post.call_args.kwargs["headers"]["Idempotency-Key"] != original_attempt["key"]
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


@pytest.mark.parametrize("command", ["/stop", "/cancel"])
def test_pending_author_can_stop_without_interrupting_other_active_author(factory, tmp_path, monkeypatch, command):
    from unittest.mock import AsyncMock
    from inkbox_plugin.native_turns import NativeTurns

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.base.host._is_user_authorized = lambda _: True
        monkeypatch.setattr(value.queue, "_kick", lambda _: None)
        fence = AsyncMock(return_value=True)
        monkeypatch.setattr("inkbox_plugin.host_fencing.fence_turn", fence)
        value.queue._forward_control = AsyncMock()
        try:
            await value.queue.accept(receipt(1, mode="whatsapp", sender="+15555550102"))
            await value.queue.accept(receipt(2, mode="whatsapp"))
            row = next(iter(value.queue.rows.values()))
            bob, alice = row["turns"]
            bob["state"] = "running"
            value.queue.active[row["chat_id"]] = bob
            stop = receipt(4, mode="whatsapp")
            stop.metadata["inkbox_reply_route"]["raw_text"] = command
            await value.queue.accept(stop)
            assert bob["state"] == "running" and alice["state"] == "cancelled"
            assert alice["fenced"] and row["controls"][uid(4)] == "consumed"
            fence.assert_not_awaited()
            value.queue._forward_control.assert_not_awaited()
            bob["state"] = "done"
            value.queue.active.clear()
            value.queue._save(row)
            await value.queue.close()
            value.queue = NativeTurns(value.adapter, tmp_path / "native")
            value.adapter._native_turns = value.queue
            await value.queue.start()
            await settle(value.queue)
            assert not value.inputs
            recovered = next(iter(value.queue.rows.values()))
            assert recovered["turns"][1]["state"] == "cancelled"
            await value.queue.accept(receipt(5, mode="whatsapp", sender="+15555550102"))
            await settle(value.queue)
            assert [event.message_id for event in value.inputs] == [uid(5)]
        finally:
            value.queue.active.clear()
            await value.queue.close()
            client.close()

    asyncio.run(run())


def test_legacy_windows_scan_survives_utf8_rewrite_and_second_startup(factory, tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path
    from inkbox_plugin import adapter as adapter_module, companion
    from tests.test_companion import event

    async def run():
        value = factory("imessage")
        adapter = value.adapter
        adapter._base_url = "https://example.com"
        adapter._identity_id = None
        adapter._slack_enabled = adapter._imessage_threaded_replies = False
        adapter._companion_max_bytes = 128_000
        monkeypatch.setattr(adapter_module, "_inkbox_state_path", lambda: tmp_path / "state.json")
        monkeypatch.setattr(companion.CompanionReceiver, "_kick", lambda *_: None)
        legacy_key = hashlib.sha256(json.dumps([adapter._base_url, adapter._identity_handle, None]).encode()).hexdigest()
        legacy_root = tmp_path / "inkbox_companion" / legacy_key
        old = companion.CompanionReceiver(adapter, legacy_root)
        await old.accept(event("imessage", phase="ordinary"))
        row = next(iter(old.rows.values()))
        row["legacy_note"] = "A closing quote: ”"
        old._save(row)
        await old.close()
        path = legacy_root / f"{row['key']}.json"
        path.write_bytes(json.dumps(row, ensure_ascii=False).encode("cp1252"))
        original_read = companion.read_checkpoint

        def windows_read(target):
            was_windows = companion._WINDOWS
            companion._WINDOWS = True
            try:
                return original_read(target)
            finally:
                companion._WINDOWS = was_windows

        monkeypatch.setattr(companion.locale, "getpreferredencoding", lambda _: "cp1252")
        monkeypatch.setattr(companion, "read_checkpoint", windows_read)
        monkeypatch.setattr(adapter_module, "read_checkpoint", windows_read)
        original_text = Path.read_text
        monkeypatch.setattr(Path, "read_text", lambda self, encoding=None, **kw: original_text(self, encoding=encoding or "cp1252", **kw))
        value.identity.id = uid(100)
        adapter._reply_identity = value.identity
        for _ in range(2):
            await adapter._start_companion()
            recovered = next(iter(adapter._companion.rows.values()))
            assert recovered["legacy_note"] == row["legacy_note"]
            adapter._companion._save(recovered)
            await adapter._companion.close()
            if getattr(adapter, "_native_turns", None):
                await adapter._native_turns.close()
        assert json.loads(path.read_bytes().decode("utf-8"))["legacy_note"] == row["legacy_note"]

    asyncio.run(run())


@pytest.mark.parametrize("include", [None, True])
def test_registered_group_discovery_default_and_opt_in(factory, monkeypatch, include):
    from inkbox_plugin import tools
    from inkbox_plugin.whatsapp_tools import register
    value = factory("imessage")
    client = real_identity(value.adapter)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    group = {"id": uid(30), "agent_identity_id": uid(100), "local_number": "+15555550100", "remote_number": None,
        "participants": [f"+15555550{n:03d}" for n in range(2, 11)], "is_group": True, "created_at": TIME, "updated_at": TIME,
        "latest_has_media": False, "unread_count": 0, "total_count": 1}
    client.whatsapp._http.get.side_effect = lambda _url, **kw: [group] if kw["params"]["include_groups"] else []
    ctx = Mock()
    register(ctx, lambda: True)
    call = next(c for c in ctx.register_tool.call_args_list if c.args[0] == "inkbox_list_whatsapp_conversations")
    result = json.loads(call.args[3]({} if include is None else {"include_groups": True}))
    assert client.whatsapp._http.get.call_args.kwargs["params"]["include_groups"] is bool(include)
    assert len(result["result"]) == (1 if include else 0)
    if include:
        assert result["result"][0]["is_group"] and len(result["result"][0]["participants"]) == 9


@pytest.mark.parametrize('destination', [{'to': ['+15555550102', '+15555550103'], 'group_name': 'Team'}, {'to': '+15555550102'}, {'conversation_id': uid(99)}])
def test_other_destination_uncertainty_fences_original_turn(factory, tmp_path, monkeypatch, destination):
    from inkbox_plugin import tools
    from inkbox_plugin.native_turns import NativeTurns

    monkeypatch.setattr('inkbox._message_requests.time.sleep', lambda _: None)

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        monkeypatch.setattr(tools, '_client_and_identity', lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
        attempts = []
        async def handle(event):
            event._gateway_accepted = True
            await value.adapter.on_processing_start(event)
            client.whatsapp._http.post.side_effect = httpx.ReadTimeout('lost response')
            session = str(event.source.chat_id)
            first = json.loads(invoke('send_whatsapp', {**destination, 'text': 'Hello'}, session))
            assert first['idempotency_key']
            count = client.whatsapp._http.post.call_count
            for extra in [{'idempotency_key': 'fresh-key'}, {'text': 'Hello!'}, {}]:
                assert 'error' in json.loads(invoke('send_whatsapp', {**destination, 'text': 'Hello', **extra}, session))
                assert client.whatsapp._http.post.call_count == count
            attempts.append(first)
            value.queue.capture_result(event, 'Replacement answer')
            result = await value.adapter.send(event.source.chat_id, 'Replacement answer', reply_to=event.message_id)
            assert not result.success
            await value.adapter.on_processing_complete(event, 'failure')
        value.adapter.handle_message = handle
        try:
            await receive(value.adapter, inbound())
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            saved = json.loads(json.dumps(row['turns'][0]['whatsapp_send_attempt']))
            assert saved['state'] == 'uncertain'
            assert saved['key'] == attempts[0]['idempotency_key']
            assert saved['payload'] == {**destination, 'text': 'Hello'}
            count = client.whatsapp._http.post.call_count
            await value.queue.close()
            value.queue = NativeTurns(value.adapter, tmp_path / 'native')
            value.adapter._native_turns = value.queue
            await value.queue.start()
            await settle(value.queue)
            recovered = next(iter(value.queue.rows.values()))
            assert recovered['turns'][0]['whatsapp_send_attempt'] == saved
            assert recovered.get('blocked')
            assert client.whatsapp._http.post.call_count == count
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


def test_unbound_whatsapp_retry_reuses_payload_key(factory, monkeypatch):
    from inkbox_plugin import tools
    value = factory('imessage')
    client = real_identity(value.adapter)
    monkeypatch.setattr('inkbox._message_requests.time.sleep', lambda _: None)
    monkeypatch.setattr(tools, '_client_and_identity', lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    client.whatsapp._http.post.side_effect = httpx.ReadTimeout('lost response')
    payload = {'to': ['+15555550102', '+15555550103'], 'group_name': 'Team', 'text': 'Hello'}
    try:
        assert 'explicit stable idempotency_key' in json.loads(invoke('send_whatsapp', payload))['error']
        client.whatsapp._http.post.assert_not_called()
        first = json.loads(invoke('send_whatsapp', {**payload, 'idempotency_key': 'logical-create-1'}))
        second = json.loads(invoke('send_whatsapp', {**payload, 'idempotency_key': 'logical-create-1'}))
        assert first['idempotency_key'] == second['idempotency_key']
        assert {call.kwargs['headers']['Idempotency-Key'] for call in client.whatsapp._http.post.call_args_list} == {first['idempotency_key']}
        assert all(call.kwargs['json'] == payload for call in client.whatsapp._http.post.call_args_list)
    finally:
        client.close()


@pytest.mark.parametrize("rejection", ["identity", "authorization"])
def test_whatsapp_preflight_rejection_does_not_block_other_group_author(factory, tmp_path, rejection):
    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        value.base.host._is_user_authorized = lambda _: True
        handled = []
        async def handle(event):
            event._gateway_accepted = True
            handled.append(event.message_id)
            await value.adapter.on_processing_start(event)
            if rejection == 'authorization' and event.message_id == uid(1):
                value.base.host._is_user_authorized = lambda source: source.user_id != '+15555550101'
            value.queue.capture_result(event, 'Final answer')
            result = await value.adapter.send(event.source.chat_id, 'Final answer', reply_to=event.message_id)
            await value.adapter.on_processing_complete(event, 'success' if result.success else 'failure')
        value.adapter.handle_message = handle
        try:
            alice = receipt(1, mode='whatsapp')
            alice.metadata['inkbox_reply_route'].update(identity_id=uid(999) if rejection == 'identity' else uid(100), base_url=value.adapter._base_url, conversation_kind='group')
            bob = receipt(2, mode='whatsapp', sender='+15555550102')
            bob.metadata['inkbox_reply_route'].update(identity_id=uid(100), base_url=value.adapter._base_url, conversation_kind='group', whatsapp_auto_reply_target=uid(2))
            await value.queue.accept(alice)
            await value.queue.accept(bob)
            await settle(value.queue)
            row = next(iter(value.queue.rows.values()))
            assert handled == [uid(1), uid(2)]
            assert not row.get('blocked')
            assert row['turns'][0]['reply_suppressed'] == 'preflight_rejected'
            assert all(turn['state'] == 'done' for turn in row['turns'])
            assert client.whatsapp._http.post.call_count == 1
            assert client.whatsapp._http.post.call_args.kwargs['json']['reply_to_message_id'] == uid(2)
        finally:
            await value.queue.close()
            client.close()
    asyncio.run(run())


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("destination", [
    {"to": "+15555550102"},
    {"to": ["+15555550102", "+15555550104"], "group_name": "Team"},
    {"conversation_id": uid(99), "reply_to_message_id": uid(98)},
])
def test_rejected_outreach_allows_independent_destination_and_retains_intent(factory, tmp_path, monkeypatch, destination, uncertain):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    from inkbox_plugin.native_turns import NativeTurns

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
        original = {**destination, "text": "For Bob", "idempotency_key": "outreach-b"}
        independent = {"to": "+15555550103", "text": "For Carol", "idempotency_key": "outreach-c"}
        client.whatsapp._http.get.return_value = conversation_wire(uid(99))
        saved = {}
        completed = []

        async def handle(event):
            event._gateway_accepted = True
            await value.adapter.on_processing_start(event)
            session = str(event.source.chat_id)
            # An unknown error is deliberately not proof of non-acceptance.
            client.whatsapp._http.post.side_effect = InkboxAPIError(403, {
                "error": "unknown" if uncertain else "whatsapp_contact_not_allowed"})
            assert "error" in json.loads(invoke("send_whatsapp", original, session))
            turn = next(iter(value.queue.rows.values()))["turns"][0]
            saved.update(json.loads(json.dumps(turn["whatsapp_send_attempt"])))
            client.whatsapp._http.post.side_effect = None
            result = json.loads(invoke("send_whatsapp", independent, session))
            assert ("error" in result) if uncertain else result["ok"]
            count = client.whatsapp._http.post.call_count
            # Intervening outreach must not erase the original intent or key.
            replacements = [
                {**original, "text": "Changed"},
                {**original, "idempotency_key": "replacement-key"},
                {**independent, "idempotency_key": "outreach-b"},
            ]
            if "to" in destination:
                phones = destination["to"]
                alias = list(reversed(phones)) if isinstance(phones, list) else ["+1 (555) 555-0102"]
                replacements.append({**original, "to": alias, "idempotency_key": "alias-key"})
            for replacement in replacements:
                assert "error" in json.loads(invoke("send_whatsapp", replacement, session))
                assert client.whatsapp._http.post.call_count == count
            value.queue.capture_result(event, "Source final")
            final = await value.adapter.send(event.source.chat_id, "Source final", reply_to=event.message_id)
            assert final.success is not uncertain
            await value.adapter.on_processing_complete(event, "success" if final.success else "failure")
            completed.append(True)

        value.adapter.handle_message = handle
        try:
            await receive(value.adapter, inbound())
            await settle(value.queue)
            assert completed == [True]
            calls = client.whatsapp._http.post.call_args_list
            assert [c.kwargs["headers"]["Idempotency-Key"] for c in calls[:2]] == (
                ["outreach-b"] if uncertain else ["outreach-b", "outreach-c"])
            assert len(calls) == (1 if uncertain else 3)
            if not uncertain:
                assert calls[-1].kwargs["json"] == {
                    "conversation_id": uid(30), "text": "Source final", "reply_to_message_id": uid(1)}
            row = next(iter(value.queue.rows.values()))
            if not uncertain:
                assert row["turns"][0]["whatsapp_send_attempts"]["outreach-b"] == saved
            await value.queue.close()
            value.queue = NativeTurns(value.adapter, tmp_path / "native")
            value.adapter._native_turns = value.queue
            await value.queue.start()
            await settle(value.queue)
            recovered = next(iter(value.queue.rows.values()))
            if uncertain:
                assert recovered["turns"][0]["whatsapp_send_attempt"] == saved
                assert recovered.get("blocked")
            else:
                assert recovered["turns"][0]["whatsapp_send_attempts"]["outreach-b"] == saved
                assert recovered["turns"][0]["state"] == "done" and not recovered.get("blocked")
            assert client.whatsapp._http.post.call_count == len(calls)
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


@pytest.mark.parametrize("source_destination", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_rejected_request_recovery_after_outreach_and_context_rebind(factory, tmp_path, monkeypatch, source_destination, recover):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    from inkbox_plugin.imessage_state import active_context

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
        destination = uid(30) if source_destination else uid(99)
        original = {"conversation_id": destination, "text": "Original", "reply_to_message_id": uid(2)}
        client.whatsapp._http.get.return_value = conversation_wire(destination,
            remote_number="+15555550101" if source_destination else "+15555550102")
        completed = []

        async def handle(event):
            event._gateway_accepted = True
            await value.adapter.on_processing_start(event)
            session = str(event.source.chat_id)
            client.whatsapp._http.post.side_effect = InkboxAPIError(409, {"error": "whatsapp_quote_unavailable"})
            rejected = json.loads(invoke("send_whatsapp", original, session))
            key = rejected["idempotency_key"]
            turn = next(iter(value.queue.rows.values()))["turns"][0]
            saved = json.loads(json.dumps(turn["whatsapp_send_attempt"]))
            # Older checkpoints contain only the last attempt. Bind that shape
            # before independent outreach, then bind the new journal afterward.
            turn.pop("whatsapp_send_attempts", None)
            assert clear_context(session, event.message_id)
            await value.adapter.on_processing_start(event)
            client.whatsapp._http.post.side_effect = None
            assert json.loads(invoke("send_whatsapp", {
                "to": "+15555550103", "text": "For Carol", "idempotency_key": "outreach-c"}, session))["ok"]
            assert turn["whatsapp_send_attempts"][key] == saved
            assert clear_context(session, event.message_id)
            await value.adapter.on_processing_start(event)
            assert active_context(session)["whatsapp_send_attempts"][key] == saved
            for changes in [{"text": "Changed"}, {"idempotency_key": "replacement-key"}]:
                assert "error" in json.loads(invoke("send_whatsapp", {**original, "idempotency_key": key, **changes}, session))
            assert client.whatsapp._http.post.call_count == 2
            if recover:
                # Omitting the quote during exact-key recovery restores the
                # original target, not the active source or the intervening send.
                assert json.loads(invoke("send_whatsapp", {
                    "conversation_id": destination, "text": "Original", "idempotency_key": key}, session))["ok"]
                assert client.whatsapp._http.post.call_args == client.whatsapp._http.post.call_args_list[0]
                assert turn["whatsapp_send_attempts"][key] == {**saved, "state": "sent"}
            else:
                assert turn["whatsapp_send_attempts"][key] == saved
            value.queue.capture_result(event, "Source final")
            final = await value.adapter.send(event.source.chat_id, "Source final", reply_to=event.message_id)
            assert final.success
            if source_destination and not recover:
                assert final.raw_response["inkbox_suppressed"]
            await value.adapter.on_processing_complete(event, "success")
            completed.append(True)

        value.adapter.handle_message = handle
        try:
            await receive(value.adapter, inbound())
            await settle(value.queue)
            assert completed == [True]
            assert client.whatsapp._http.post.call_count == 2 + int(recover) + int(not source_destination or recover)
            if source_destination:
                client.whatsapp._http.get.assert_not_called()
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


@pytest.mark.parametrize("uncertain", [False, True])
def test_inflight_independent_outreach_remains_turnwide_fence(factory, tmp_path, monkeypatch, uncertain):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools

    value = factory("imessage")
    client = real_identity(value.adapter)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": uid(1), "conversation_id": uid(30),
        "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock()})
    original = {"to": "+15555550102", "text": "For Bob", "idempotency_key": "outreach-b"}
    independent = {"to": "+15555550103", "text": "For Carol", "idempotency_key": "outreach-c"}
    observed = []

    def sending(*args, **kwargs):
        # The nested invocation observes the first request before it settles.
        assert "error" in json.loads(invoke("send_whatsapp", independent, session))
        assert client.whatsapp._http.post.call_count == 1
        observed.append(True)
        raise InkboxAPIError(403, {"error": "unknown" if uncertain else "whatsapp_contact_not_allowed"})

    try:
        client.whatsapp._http.post.side_effect = sending
        assert "error" in json.loads(invoke("send_whatsapp", original, session))
        assert observed == [True]
        client.whatsapp._http.post.side_effect = None
        result = json.loads(invoke("send_whatsapp", independent, session))
        assert ("error" in result) if uncertain else result["ok"]
        if uncertain:
            assert "error" in json.loads(invoke("send_whatsapp", original, session))
        assert client.whatsapp._http.post.call_count == (1 if uncertain else 2)
    finally:
        clear_context(session, uid(1))
        client.close()


@pytest.mark.parametrize("rebind", [False, True])
@pytest.mark.parametrize("alias", ["uuid_case", "phone", "conversation"])
def test_rejected_destination_alias_cannot_replace_saved_request(factory, tmp_path, monkeypatch, alias, rebind):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    from inkbox_plugin.imessage_state import active_context

    async def run():
        value = await harness(factory, tmp_path)
        client = real_identity(value.adapter)
        bob = "abcdefab-1234-4567-89ab-123456789abc"
        client.whatsapp._http.get.return_value = conversation_wire(bob)
        monkeypatch.setattr(tools, "_client_and_identity", lambda: (
            NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
        selector = {"to": "+15555550102"} if alias == "conversation" else {
            "conversation_id": bob, "reply_to_message_id": uid(2)}
        replacement = ({"conversation_id": bob.upper()} if alias == "uuid_case" else
            {"to": ["+1 (555) 555-0102"]} if alias == "phone" else {"conversation_id": bob})
        original = {**selector, "text": "Answer", "idempotency_key": "outreach-b"}
        completed = []

        async def handle(event):
            event._gateway_accepted = True
            await value.adapter.on_processing_start(event)
            session = str(event.source.chat_id)
            client.whatsapp._http.post.side_effect = InkboxAPIError(409, {"error": "whatsapp_quote_unavailable"})
            assert "error" in json.loads(invoke("send_whatsapp", original, session))
            turn = next(iter(value.queue.rows.values()))["turns"][0]
            saved = json.loads(json.dumps(turn["whatsapp_send_attempt"]))
            client.whatsapp._http.post.side_effect = None
            if rebind:
                turn.pop("whatsapp_send_attempts", None)
                assert clear_context(session, event.message_id)
                await value.adapter.on_processing_start(event)
            assert "error" in json.loads(invoke("send_whatsapp", {
                **replacement, "text": "Answer", "idempotency_key": "replacement-b"}, session))
            assert client.whatsapp._http.post.call_count == 1
            assert turn["whatsapp_send_attempt"] == saved
            # Identical content alone must not prohibit independent outreach.
            assert json.loads(invoke("send_whatsapp", {
                "to": "+15555550103", "text": "Answer", "idempotency_key": "outreach-c"}, session))["ok"]
            if rebind:
                assert clear_context(session, event.message_id)
                await value.adapter.on_processing_start(event)
            assert active_context(session)["whatsapp_send_attempts"]["outreach-b"] == saved
            assert "error" in json.loads(invoke("send_whatsapp", {
                **replacement, "text": "Answer", "idempotency_key": "replacement-after-outreach"}, session))
            assert client.whatsapp._http.post.call_count == 2
            assert turn["whatsapp_send_attempts"]["outreach-b"] == saved
            recovery = {k: v for k, v in original.items() if k != "reply_to_message_id"}
            assert json.loads(invoke("send_whatsapp", recovery, session))["ok"]
            calls = client.whatsapp._http.post.call_args_list
            assert calls[-1] == calls[0]
            assert turn["whatsapp_send_attempts"]["outreach-b"] == {**saved, "state": "sent"}
            # Once accepted, a later explicitly keyed send is not payload-deduplicated.
            assert json.loads(invoke("send_whatsapp", {**original, "idempotency_key": "later-b"}, session))["ok"]
            value.queue.capture_result(event, "Source final")
            final = await value.adapter.send(event.source.chat_id, "Source final", reply_to=event.message_id)
            assert final.success
            assert client.whatsapp._http.post.call_args.kwargs["json"] == {
                "conversation_id": uid(30), "text": "Source final", "reply_to_message_id": uid(1)}
            await value.adapter.on_processing_complete(event, "success")
            completed.append(True)

        value.adapter.handle_message = handle
        try:
            await receive(value.adapter, inbound())
            await settle(value.queue)
            assert completed == [True]
            assert client.whatsapp._http.post.call_count == 5
            reads = client.whatsapp._http.get.call_args_list
            assert reads and all(call.args == (f"/conversations/{bob}",) and
                call.kwargs == {"params": {"agent_identity_id": uid(100)}} for call in reads)
        finally:
            await value.queue.close()
            client.close()

    asyncio.run(run())


@pytest.mark.parametrize("evidence", ["unavailable", "wrong_identity", "wrong_conversation", "missing_phone", "group", "direct"])
def test_rejected_alias_requires_owned_direct_conversation_evidence(factory, tmp_path, monkeypatch, evidence):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools
    from inkbox_plugin.imessage_state import active_context

    value = factory("imessage")
    client = real_identity(value.adapter)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (
        NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": uid(1), "conversation_id": uid(30),
        "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock()})
    original = {"conversation_id": uid(99), "text": "Answer", "reply_to_message_id": uid(2), "idempotency_key": "original"}
    details = conversation_wire(uid(99))
    details.update({"wrong_identity": {"agent_identity_id": uid(999)}, "wrong_conversation": {"id": uid(999)},
        "missing_phone": {"remote_number": None}, "group": {"is_group": True}}.get(evidence, {}))
    client.whatsapp._http.get.return_value = details
    if evidence == "unavailable":
        client.whatsapp._http.get.side_effect = InkboxAPIError(404, "Conversation unavailable")
    try:
        client.whatsapp._http.post.side_effect = InkboxAPIError(409, {"error": "whatsapp_quote_unavailable"})
        assert "error" in json.loads(invoke("send_whatsapp", original, session))
        saved = json.loads(json.dumps(active_context(session)["whatsapp_send_attempt"]))
        client.whatsapp._http.post.side_effect = None
        result = json.loads(invoke("send_whatsapp", {
            "to": "+15555550102", "text": "Answer", "idempotency_key": "replacement"}, session))
        assert result.get("ok") if evidence == "group" else "error" in result
        assert client.whatsapp._http.post.call_count == (2 if evidence == "group" else 1)
        assert active_context(session)["whatsapp_send_attempts"]["original"] == saved
        assert json.loads(invoke("send_whatsapp", original, session))["ok"]
        assert client.whatsapp._http.post.call_args == client.whatsapp._http.post.call_args_list[0]
        client.whatsapp._http.get.assert_called_once_with(
            f"/conversations/{uid(99)}", params={"agent_identity_id": uid(100)})
    finally:
        clear_context(session, uid(1))
        client.close()


def test_rejected_group_does_not_alias_distinct_group_or_participants(factory, tmp_path, monkeypatch):
    from inkbox import InkboxAPIError
    from inkbox_plugin import tools

    value = factory("imessage")
    client = real_identity(value.adapter)
    participants = ["+15555550102", "+15555550103"]
    client.whatsapp._http.get.side_effect = lambda path, **kwargs: conversation_wire(
        path.rsplit("/", 1)[-1], is_group=True, remote_number=None, participants=participants)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (
        NS(base_url=value.adapter._base_url), client, value.adapter._reply_identity))
    session = str(tmp_path)
    bind_context(session, {"mode": "whatsapp", "turn_id": uid(1), "conversation_id": uid(30),
        "identity_id": uid(100), "base_url": value.adapter._base_url, "authorize_whatsapp": Mock()})
    original = {"conversation_id": uid(98), "text": "Answer", "reply_to_message_id": uid(2), "idempotency_key": "group-a"}
    try:
        client.whatsapp._http.post.side_effect = InkboxAPIError(409, {"error": "whatsapp_quote_unavailable"})
        assert "error" in json.loads(invoke("send_whatsapp", original, session))
        client.whatsapp._http.post.side_effect = None
        for selector in ({"conversation_id": uid(99)}, {"to": participants, "group_name": "Team"}):
            assert json.loads(invoke("send_whatsapp", {
                **selector, "text": "Answer", "idempotency_key": str(selector)}, session))["ok"]
            assert client.whatsapp._http.post.call_args.kwargs["json"] == {**selector, "text": "Answer"}
        assert client.whatsapp._http.post.call_count == 3
        client.whatsapp._http.get.assert_not_called()
    finally:
        clear_context(session, uid(1))
        client.close()
