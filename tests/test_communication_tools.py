import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
from inkbox import Inkbox
from inkbox.exceptions import InkboxAPIError

from inkbox_plugin import communication_tools as comms, tools, whatsapp_tools
from inkbox_plugin.config import InkboxPluginConfig, VoiceStack


def uid(value):
    return str(UUID(int=value))


TIME = "2026-11-01T12:00:00Z"
EMAIL = "agent@example.com"
MESSAGE = {"id": uid(10), "mailbox_id": uid(2), "thread_id": uid(11), "message_id": "<example@example.com>",
           "from_address": "person@example.com", "to_addresses": [EMAIL], "subject": "Meeting", "snippet": "Tomorrow",
           "direction": "inbound", "status": "delivered", "is_read": False, "is_starred": False,
           "has_attachments": False, "created_at": TIME}
CALL = {"id": uid(12), "remote_phone_number": "+15555550101", "direction": "outbound", "status": "in_progress",
        "created_at": TIME, "updated_at": TIME, "connections": []}


@pytest.fixture
def sdk(monkeypatch):
    client = Inkbox("synthetic-test-key", base_url="http://127.0.0.1:9")
    client._ids_resource._http = Mock()
    client._ids_resource._http.get.return_value = {
        "id": uid(1), "organization_id": "org_example", "agent_handle": "example-agent", "created_at": TIME, "updated_at": TIME,
        "imessage_enabled": True,
        "mailbox": {"id": uid(2), "email_address": EMAIL, "created_at": TIME, "updated_at": TIME},
        "phone_number": {"id": uid(3), "number": "+15555550100", "type": "local", "status": "active",
                         "incoming_call_action": "hosted_agent", "created_at": TIME, "updated_at": TIME},
    }
    identity = client.get_identity("example-agent")
    for resource in (client.messages, client.threads, client.calls, client.message_sends, client.mailboxes, client.texts, client.imessages):
        resource._http = Mock()
    cfg = InkboxPluginConfig(api_key="synthetic-test-key", identity="example-agent", voice_stack=VoiceStack.INKBOX_VOICE_AI)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (cfg, client, identity))
    monkeypatch.setattr(tools, "read_runtime_config", lambda: cfg)
    yield client, identity
    client.close()


def invoke(name, args):
    return json.loads(comms.dispatch("inkbox_" + name, args))


def test_email_filters_are_bounded_and_can_continue_after_empty_matches(sdk):
    client, _ = sdk
    client.messages._http.get.return_value = {"items": [MESSAGE, {**MESSAGE, "id": uid(13), "subject": "Other"}], "has_more": False}
    result = invoke("list_emails", {"limit": 1, "q": "other", "unread_only": True, "start_datetime": "2026-11-01"})
    assert result["result"] == {"messages": [], "scanned_count": 1, "next_offset": 1}
    result = invoke("list_emails", {"limit": 1, "offset": 1, "q": "OTHER"})
    assert result["result"]["messages"][0]["id"] == uid(13)
    assert result["result"]["next_offset"] is None
    assert client.messages._http.get.call_count == 2
    assert client.messages._http.get.call_args_list[0].kwargs["params"]["start_datetime"] == "2026-11-01"
    assert all(call.args[0] == f"/mailboxes/{EMAIL}/messages" for call in client.messages._http.get.call_args_list)


def test_email_read_preserves_body_and_attachment_metadata(sdk):
    client, _ = sdk
    client.messages._http.get.return_value = {**MESSAGE, "body_text": "Full body", "attachment_metadata": [{"filename": "notes.txt"}]}
    result = invoke("get_email", {"message_id": uid(10)})
    assert result["result"]["body_text"] == "Full body"
    assert result["result"]["attachment_metadata"] == [{"filename": "notes.txt"}]
    client.messages._http.get.assert_called_once_with(f"/mailboxes/{EMAIL}/messages/{uid(10)}")


def test_email_thread_returns_summaries_without_marking_messages_read(sdk):
    client, _ = sdk
    client.threads._http.get.return_value = {"id": uid(11), "mailbox_id": uid(2), "message_count": 1,
                                           "last_message_at": TIME, "created_at": TIME, "messages": [MESSAGE]}
    result = invoke("get_email_thread", {"thread_id": uid(11)})
    assert result["result"]["messages"][0]["snippet"] == "Tomorrow"
    client.messages._http.get.assert_not_called()
    client.messages._http.patch.assert_not_called()


@pytest.mark.parametrize("args", [{"message_id": "../other"}, {"message_id": uid(10), "email_address": "other@example.com"}])
def test_email_tools_cannot_override_mailbox_or_escape_id_path(sdk, args):
    client, _ = sdk
    assert "error" in invoke("get_email", args)
    client.messages._http.get.assert_not_called()


def test_email_access_error_is_not_hidden_or_retried(sdk):
    client, _ = sdk
    client.messages._http.get.side_effect = InkboxAPIError(403, "not authorized")
    assert invoke("get_email", {"message_id": uid(10)})["status_code"] == 403
    assert client.messages._http.get.call_count == 1


def test_send_and_reply_attach_local_files_and_preserve_keys(sdk, tmp_path):
    client, _ = sdk
    client.messages._http.post.return_value = {**MESSAGE, "direction": "outbound", "status": "queued"}
    image = tmp_path / "chart.png"
    image.write_bytes(b"image")
    for target in ({"to": ["person@example.com"]}, {"reply_to_message_id": uid(10)}):
        result = json.loads(tools.inkbox_send_email({**target, "body_html": '<img src="cid:chart">',
            "attachments": [{"path": str(image), "content_id": "chart"}], "idempotency_key": "same-request"}))
        assert result["ok"] and result["idempotency_key"] == "same-request"
        assert result["status"] == "queued"
        call = client.messages._http.post.call_args
        assert call.kwargs["headers"]["Idempotency-Key"] == "same-request"
        assert call.kwargs["json"]["attachments"] == [{"filename": "chart.png", "content_type": "image/png",
            "content_base64": base64.b64encode(b"image").decode(), "content_id": "chart"}]


def test_attachment_host_rejection_prevents_email_send(sdk, monkeypatch):
    client, _ = sdk
    import gateway.platforms.base as base
    monkeypatch.setattr(base, "validate_media_delivery_path", lambda _path: None)
    result = json.loads(tools.inkbox_send_email({"to": ["person@example.com"], "attachments": [{"path": "/private/key"}]}))
    assert "error" in result
    client.messages._http.post.assert_not_called()


def test_forward_keeps_original_attachments_and_own_recovery_namespace(sdk):
    client, _ = sdk
    client.messages._http.post.return_value = MESSAGE
    result = invoke("forward_email", {"message_id": uid(10), "to": ["person@example.com"], "idempotency_key": "forward-key"})
    assert result["operation"] == "mail.forward"
    assert result["idempotency_key"] == "forward-key"
    call = client.messages._http.post.call_args
    assert call.args[0].endswith(f"/messages/{uid(10)}/forward")
    assert call.kwargs["json"].get("include_original_attachments", True)


@pytest.mark.parametrize("name,args,resource,operation", [
    ("inkbox_send_email", {"to": ["person@example.com"]}, "messages", "mail.send"),
    ("inkbox_send_sms", {"to": "+15555550101", "text": "Hello"}, "texts", "text.send"),
    ("inkbox_send_imessage", {"to": "+15555550101", "text": "Hello"}, "imessages", "imessage.send"),
])
def test_send_timeout_retains_one_key_across_sdk_retries(sdk, monkeypatch, name, args, resource, operation):
    client, _ = sdk
    import inkbox._message_requests as requests
    monkeypatch.setattr(requests.time, "sleep", lambda _seconds: None)
    http = getattr(client, resource)._http
    http.post.side_effect = httpx.ReadTimeout("response lost")
    result = json.loads(getattr(tools, name)({**args, "idempotency_key": "recover-original"}))
    assert result["idempotency_key"] == "recover-original"
    assert result["operation"] == operation
    assert "recovery" in result and not result.get("ok")
    assert http.post.call_count == 3
    assert {call.kwargs["headers"]["Idempotency-Key"] for call in http.post.call_args_list} == {"recover-original"}


def test_response_decode_failure_keeps_generated_key(sdk):
    client, _ = sdk
    client.messages._http.post.return_value = {}
    result = json.loads(tools.inkbox_send_email({"to": ["person@example.com"]}))
    assert result["idempotency_key"] == client.messages._http.post.call_args.kwargs["headers"]["Idempotency-Key"]
    assert "error" in result and client.messages._http.post.call_count == 1


@pytest.mark.parametrize("operation,kind,sender", [("text.send", "phone_number", 3), ("imessage.send", "imessage_identity", 1),
                                                  ("whatsapp.send", "whatsapp_identity", 1)])
def test_lookup_uses_configured_sender_and_never_writes(sdk, operation, kind, sender):
    client, _ = sdk
    client.message_sends._http.get.return_value = {"message_id": uid(10)}
    result = invoke("lookup_message_send", {"operation": operation, "idempotency_key": "recover-original"})
    assert result["result"]["message_id"] == uid(10)
    client.message_sends._http.get.assert_called_once_with("/message-sends/lookup", params={
        "sender_kind": kind, "sender_id": uid(sender), "operation": operation}, headers={"Idempotency-Key": "recover-original"})
    client.message_sends._http.post.assert_not_called()


def test_email_lookup_resolves_only_the_configured_mailbox(sdk):
    client, _ = sdk
    client.mailboxes._http.get.return_value = {"id": uid(2), "email_address": EMAIL, "created_at": TIME, "updated_at": TIME}
    client.message_sends._http.get.return_value = {"message_id": uid(10)}
    result = invoke("lookup_message_send", {"operation": "mail.reply_all", "idempotency_key": "reply-key"})
    assert result["result"]["operation"] == "mail.reply_all"
    client.mailboxes._http.get.assert_called_once_with(f"/mailboxes/{EMAIL}")
    assert client.message_sends._http.get.call_args.kwargs["params"]["sender_id"] == uid(2)


def test_call_reads_keep_connection_history_and_transcript_attribution(sdk):
    client, _ = sdk
    client.calls._http.get.return_value = CALL
    assert invoke("get_call", {"call_id": uid(12)})["result"]["connections"] == []
    client.calls._http.get.return_value = [{"id": uid(15), "call_id": uid(12), "seq": 0, "ts_ms": 0, "party": "remote",
                                          "phone_number": "+15555550101", "text": "Hello", "created_at": TIME}]
    result = invoke("get_call_transcripts", {"call_id": uid(12)})
    assert result["result"][0]["phone_number"] == "+15555550101"


def test_call_listing_is_identity_scoped_and_hangup_is_only_a_request(sdk):
    client, _ = sdk
    client.calls._http.get.return_value = [CALL]
    assert invoke("list_calls", {"limit": 5})["ok"]
    assert client.calls._http.get.call_args.kwargs["params"]["agent_identity_id"] == uid(1)
    client.calls._http.post.return_value = CALL
    result = invoke("hangup_call", {"call_id": uid(12)})
    assert result["result"]["hangup_requested"]
    assert result["result"]["call"]["status"] == "in_progress"


def test_voicemail_message_reaches_hosted_call_without_conflicting_alias(sdk):
    client, _ = sdk
    client.calls._http.post.return_value = {**CALL, "mode": "hosted_agent", "on_voicemail": "leave_message"}
    result = json.loads(tools.inkbox_place_call({"to_number": "+15555550101", "purpose": "Confirm appointment",
                                              "on_voicemail": "leave_message", "voicemail_message": "Please call back."}))
    assert result["ok"] and result["on_voicemail"] == "leave_message"
    body = client.calls._http.post.call_args.kwargs["json"]
    assert body["voicemail_message"] == "Please call back."
    assert body["on_voicemail"] == "leave_message" and "voicemail_detection" not in body


@pytest.mark.parametrize("options", [{"voicemail_message": "Hello"}, {"on_voicemail": "bad"},
                                    {"on_voicemail": "leave_message", "voicemail_detection": "enabled"}])
def test_invalid_voicemail_options_do_not_place_a_call(sdk, options):
    client, _ = sdk
    assert "error" in json.loads(tools.inkbox_place_call({"to_number": "+15555550101", "purpose": "Check in", **options}))
    client.calls._http.post.assert_not_called()


def test_local_voice_cannot_request_hosted_voicemail_message(sdk, monkeypatch):
    client, _ = sdk
    monkeypatch.setattr(tools, "read_runtime_config", lambda: InkboxPluginConfig(voice_stack=VoiceStack.INKBOX_TTS_STT))
    result = json.loads(tools.inkbox_place_call({"to_number": "+15555550101", "purpose": "Check in", "on_voicemail": "leave_message"}))
    assert "requires Inkbox Voice AI" in result["error"]
    client.calls._http.post.assert_not_called()


def test_attachment_download_uses_only_sdk_url_and_cleans_up_oversize(sdk, monkeypatch, tmp_path):
    client, _ = sdk
    client.messages._http.get.return_value = {"url": "https://files.example.com/attachment?token=synthetic"}
    stream = Mock()
    response = stream.return_value.__enter__ = Mock(return_value=NS(raise_for_status=lambda: None, iter_bytes=lambda: iter([b"abcd"])))
    stream.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(comms.httpx, "stream", stream)
    monkeypatch.setattr(comms.tempfile, "tempdir", str(tmp_path))
    result = invoke("download_email_attachment", {"message_id": uid(10), "filename": "notes.txt"})
    assert Path(result["result"]["file_path"]).read_bytes() == b"abcd"
    assert "headers" not in stream.call_args.kwargs
    Path(result["result"]["file_path"]).unlink()
    monkeypatch.setattr(comms, "MAX_ATTACHMENT_BYTES", 2)
    assert "exceeds" in invoke("download_email_attachment", {"message_id": uid(10), "filename": "notes.txt"})["error"]
    assert not list(tmp_path.iterdir())
    assert response.call_count == 2


def test_whatsapp_visibility_is_cached_by_identity_and_refreshes(monkeypatch):
    from inkbox_plugin import capabilities
    cfg = InkboxPluginConfig(api_key="synthetic", identity="first")
    monkeypatch.setattr(capabilities, "read_runtime_config", lambda: cfg)
    monkeypatch.setattr(capabilities, "_CACHE_KEY", None)
    clock = [0]
    monkeypatch.setattr(capabilities.time, "monotonic", lambda: clock[0])
    client = Mock()
    client.get_identity.return_value = NS(phone_number=None, whatsapp_configured=False, whatsapp_enabled=False)
    factory = Mock()
    factory.return_value = client
    monkeypatch.setattr(capabilities, "_discovery_client", factory)
    registered = {}
    whatsapp_tools.register(NS(register_tool=lambda *args, **kwargs: registered.update({args[0]: kwargs["check_fn"]})), lambda: True)
    assert len(registered) == 15
    assert registered["inkbox_get_whatsapp_status"]()
    assert not registered["inkbox_send_whatsapp"]()
    assert not registered["inkbox_get_whatsapp_message"]()
    assert client.get_identity.call_count == 1
    client.get_identity.return_value = NS(phone_number=None, whatsapp_configured=True, whatsapp_enabled=False)
    clock[0] = 61
    assert registered["inkbox_get_whatsapp_message"]()
    assert not registered["inkbox_send_whatsapp"]()
    assert client.get_identity.call_count == 2
    cfg.identity = "second"
    client.get_identity.side_effect = RuntimeError("unavailable")
    assert registered["inkbox_send_whatsapp"]()
    assert registered["inkbox_get_whatsapp_message"]()
    assert client.get_identity.call_count == 3
