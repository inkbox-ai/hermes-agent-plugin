"""Companion delivery at the Hermes enqueue and processing boundaries."""

import asyncio
import json
import sys
import threading
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.adapter import InkboxAdapter
from inkbox_plugin.companion import CompanionReceiver
from inkbox_plugin import tools as plugin_tools


def uid(number):
    return str(UUID(int=number))


def event(channel="mail", phase="initialization", number=3, *, scope=10, activation=20, conversation=30):
    author = "owner@example.com" if number == 3 else "fred@example.com"
    if channel != "mail":
        author = "+15555550101" if number == 3 else "+15555550102"
    item = {"id": uid(number), "direction": "inbound", "sender_access": "direct", "created_at": "2026-01-01T00:00:00Z"}
    if channel == "mail":
        item.update(from_address=author, thread_id=uid(conversation), body_text="/clear")
    elif channel == "phone":
        item.update(sender_phone_number=author, remote_phone_number=None, conversation_id=uid(conversation), text="YES")
    else:
        item.update(sender_number=author, remote_number=None, conversation_id=uid(conversation), content="/approve")
    meta = {"channel": channel, "phase": phase, "scope_id": uid(scope), "conversation_id": uid(conversation), "sequence": number}
    if phase != "ordinary":
        meta.update(activation_id=uid(activation))
    return {
        "event_type": {"mail": "message.received", "phone": "text.received", "imessage": "imessage.received"}[channel],
        "data": {"text_message" if channel == "phone" else "message": item},
        "companion": meta,
    }


def snapshot(envelope):
    meta = envelope["companion"]
    channel = meta["channel"]
    authors = ["fred@example.com", "nancy@example.com", "owner@example.com"] if channel == "mail" else [
        "+15555550102", "+15555550103", "+15555550101",
    ]
    entries = [{
        "id": uid(index), "author": author, "occurred_at": f"2026-01-01T00:00:0{index}Z",
        "text": ["/clear", "YES", "Hello group"][index - 1], "historical": index != 3,
        "is_trigger": index == 3, "attachments": [{"filename": "notes.txt"}] if index == 2 else [],
    } for index, author in enumerate(authors, 1)]
    context = {"channel": channel, "conversation_id": meta["conversation_id"]}
    if channel == "mail":
        context.update(reply_to_message_id=uid(3), to=[authors[2]], cc=authors[:2])
    return {key: meta[key] for key in ("scope_id", "activation_id", "conversation_id", "channel")} | {
        "entries": entries, "text": json.dumps(entries), "reply_context": context,
        "notices": [{"code": "history_unavailable", "message": "Some earlier messages are unavailable."}],
    }


class Host:
    def __init__(self):
        self.denied = set()
        self.allow_sponsor = True
        self._pending_approvals = {}
        self.session_store = types.SimpleNamespace(get_or_create_session=lambda source: types.SimpleNamespace(
            session_id=source.chat_id, session_key=source.chat_id + ":" + source.thread_id,
        ))

    def _is_user_authorized(self, source):
        if source.user_id in self.denied:
            return False
        return bool(getattr(source, "role_authorized", False)) or (
            self.allow_sponsor and source.user_id in {"owner@example.com", "+15555550101"}
        )

    async def handler(self, _event):
        pass


@pytest.fixture
def factory(tmp_path, monkeypatch):
    session = types.ModuleType("gateway.session")
    session.build_session_key = lambda source, **kw: source.chat_id + ":" + source.thread_id
    monkeypatch.setitem(sys.modules, "gateway.session", session)
    created = []

    def make(channel="mail", *, root=None, max_bytes=128_000):
        adapter = object.__new__(InkboxAdapter)
        adapter.config = types.SimpleNamespace(extra={})
        adapter._identity_handle = "sample-agent"
        adapter._identity_id = uid(100)
        adapter._background_tasks = set()
        adapter._active_sessions = {}
        adapter.build_source = lambda **kw: types.SimpleNamespace(**kw)
        adapter._resolve_channel_overrides = lambda *args: ("Reply to this conversation.", None)
        adapter._resolve_contact_full = AsyncMock(return_value={"id": uid(900), "name": "Saved contact"})
        adapter._last_inbound_email = {}
        adapter._require_signature = False
        adapter._signing_key = "synthetic-signing-key"
        adapter._external_events_enabled = False
        host = Host()
        adapter._message_handler = host.handler
        resource = types.SimpleNamespace(
            load_initialization=Mock(side_effect=lambda *args, **kw: snapshot(event(channel))),
            activation_messages=Mock(side_effect=lambda *args, **kw: snapshot(event(channel))),
        )
        identity = types.SimpleNamespace(**{method: Mock(return_value=types.SimpleNamespace(id=uid(800))) for method in (
            "reply_all_email", "send_email", "send_text", "send_imessage",
        )})
        adapter._inkbox = types.SimpleNamespace(companion=resource, get_identity=Mock(return_value=identity))
        adapter._companion = CompanionReceiver(adapter, root or tmp_path / str(len(created)), max_bytes)
        receiver = adapter._companion
        receiver.completion_timeout = 1
        inputs = []
        gate = asyncio.Event()
        gate.set()

        async def handle(item):
            inputs.append(item)
            await adapter.on_processing_start(item)
            await gate.wait()
            await adapter.on_processing_complete(item, "success")

        adapter.handle_message = handle
        instance = types.SimpleNamespace(
            adapter=adapter, receiver=receiver, host=host, resource=resource, identity=identity,
            inputs=inputs, gate=gate,
        )
        created.append(instance)
        return instance

    return make


async def idle(instance):
    for _ in range(300):
        if not instance.receiver.tasks:
            await asyncio.sleep(0)
            if not instance.receiver.tasks:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("Companion worker did not settle")


async def wait_inputs(instance, count):
    for _ in range(100):
        if len(instance.inputs) == count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Expected {count} host inputs; received {len(instance.inputs)}")


@pytest.mark.parametrize("channel", ["mail", "phone", "imessage"])
def test_one_initialization_live_barrier_and_immutable_reply(factory, channel):
    async def run():
        instance = factory(channel)
        instance.gate.clear()
        await instance.receiver.accept(event(channel))
        await wait_inputs(instance, 1)
        first = instance.inputs[0]
        assert first.text.count('"is_trigger": true') == 1
        assert "/clear" in first.text and "YES" in first.text and "notes.txt" in first.text
        assert "history_unavailable" in first.text
        assert not first.text.startswith("/") and not first.internal
        assert first.source.chat_type == "group"
        assert first.source.chat_id.startswith("companion:")
        assert uid(900) not in first.source.chat_id
        await instance.receiver.accept(event(channel))
        await instance.receiver.accept(event(channel, "live", 4))
        assert len(instance.inputs) == 1
        saved = json.loads(next(instance.receiver.root.glob("*.json")).read_text())
        assert saved["turns"][1]["state"] == "pending"
        instance.gate.set()
        await idle(instance)
        assert len(instance.inputs) == 2
        assert first.source.chat_id == instance.inputs[1].source.chat_id
        instance.adapter._last_inbound_email[first.source.chat_id] = {"from_address": "stranger@example.com"}
        result = await instance.adapter.send(first.source.chat_id, "Group reply", reply_to=first.message_id)
        assert result.success
        if channel == "mail":
            instance.identity.reply_all_email.assert_called_once_with(uid(3), body_text="Group reply")
            instance.identity.send_email.assert_not_called()
        else:
            send = instance.identity.send_text if channel == "phone" else instance.identity.send_imessage
            send.assert_called_once_with(conversation_id=uid(30), text="Group reply")
        assert instance.inputs[1].source.user_id != first.source.user_id
        await instance.receiver.accept(event(channel, "live", 4))
        await idle(instance)
        assert len(instance.inputs) == 2
    asyncio.run(run())


def test_real_sdk_pagination_produces_one_host_input(factory):
    from inkbox.companion import CompanionResource

    async def run():
        instance = factory()
        full = snapshot(event())
        calls = []

        def get(_path, params):
            calls.append(params)
            cursor = params.get("cursor")
            page = {key: value for key, value in full.items() if key not in {"entries", "text"}}
            page.update(
                items=full["entries"][:2] if cursor is None else full["entries"][1:],
                history_complete=cursor is not None, next_cursor="page-2" if cursor is None else None,
            )
            return page

        instance.adapter._inkbox.companion = CompanionResource(types.SimpleNamespace(get=get))
        await instance.receiver.accept(event())
        await idle(instance)
        assert len(instance.inputs) == 1
        text = instance.inputs[0].text
        assert text.count(uid(1)) == 1 and text.count(uid(2)) == 1
        assert "/clear" in text and "YES" in text and "Hello group" in text
        assert any(call.get("cursor") == "page-2" for call in calls)
        assert len(calls) == 3 and all(call["limit"] == 100 for call in calls)  # SDK snapshot consistency check only.
    asyncio.run(run())


def test_restart_during_hydration_resumes_once(factory):
    async def run():
        first = factory()
        started, release = threading.Event(), threading.Event()

        def load(*args, **kwargs):
            started.set()
            release.wait(5)
            return snapshot(event())

        first.resource.load_initialization.side_effect = load
        await first.receiver.accept(event())
        await asyncio.to_thread(started.wait, 2)
        await first.receiver.accept(event(phase="live", number=4))
        await first.receiver.close()
        release.set()
        second = factory(root=first.receiver.root)
        await second.receiver.start()
        await idle(second)
        assert not first.inputs
        assert len(second.inputs) == 2
        await second.receiver.accept(event())
        await idle(second)
        assert len(second.inputs) == 2
    asyncio.run(run())


@pytest.mark.parametrize("state", ["submitting", "submitted"])
def test_uncertain_restart_never_resubmits(factory, state):
    async def run():
        first = factory()
        first.gate.clear()
        await first.receiver.accept(event())
        await wait_inputs(first, 1)
        row = next(iter(first.receiver.rows.values()))
        row["turns"][0]["state"] = state
        first.receiver._save(row)
        await first.receiver.close()
        second = factory(root=first.receiver.root)
        await second.receiver.start()
        await second.receiver.accept(event(phase="live", number=4))
        await second.receiver.accept(event())
        await idle(second)
        assert not second.inputs
        assert next(iter(second.receiver.rows.values()))["state"] == "paused"
        for task in first.adapter._background_tasks:
            task.cancel()
    asyncio.run(run())


def test_timeout_pauses_and_live_stays_pending(factory):
    async def run():
        instance = factory()
        instance.gate.clear()
        instance.receiver.completion_timeout = 0.03
        await instance.receiver.accept(event())
        await wait_inputs(instance, 1)
        await instance.receiver.accept(event(phase="live", number=4))
        await idle(instance)
        row = next(iter(instance.receiver.rows.values()))
        assert row["state"] == "paused" and row["turns"][1]["state"] == "pending"
        assert len(instance.inputs) == 1
        instance.gate.set()
        await asyncio.gather(*instance.adapter._background_tasks)
        assert row["state"] == "paused"
        assert len(instance.inputs) == 1
    asyncio.run(run())


def test_live_first_hydrates_and_ordinary_is_separate(factory):
    async def run():
        instance = factory()
        await instance.receiver.accept(event(phase="ordinary"))
        await idle(instance)
        instance.resource.load_initialization.assert_not_called()
        await instance.receiver.accept(event(phase="live", number=4))
        await idle(instance)
        assert len(instance.inputs) == 2
        assert instance.inputs[0].source.chat_id != instance.inputs[1].source.chat_id
        assert "Hello group" in instance.inputs[1].text
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["scope", "trigger", "size", "revoked", "sponsor", "denied", "thread_policy", "control"])
def test_failures_do_not_partially_initialize(factory, failure):
    async def run():
        instance = factory(max_bytes=100 if failure == "size" else 128_000)
        if failure in {"scope", "trigger"}:
            value = snapshot(event())
            if failure == "scope":
                value["scope_id"] = uid(999)
            else:
                value["entries"].pop()
            instance.resource.load_initialization.side_effect = lambda *args, **kw: value
        elif failure == "revoked":
            instance.resource.load_initialization.side_effect = PermissionError("Grant revoked")
        elif failure == "sponsor":
            instance.host.allow_sponsor = False
        elif failure == "denied":
            instance.host.denied.add("owner@example.com")
        elif failure == "thread_policy":
            instance.adapter.config.extra["thread_sessions_per_user"] = True
        elif failure == "control":
            instance.receiver._check_controls = Mock(side_effect=RuntimeError("Pending host approval"))
        await instance.receiver.accept(event())
        await idle(instance)
        assert not instance.inputs
        assert next(iter(instance.receiver.rows.values()))["state"] == "failed"
    asyncio.run(run())


def test_denied_bystander_cannot_borrow_or_answer_approval(factory):
    async def run():
        instance = factory()
        await instance.receiver.accept(event())
        await idle(instance)
        instance.host.denied.add("fred@example.com")
        await instance.receiver.accept(event(phase="live", number=4))
        await idle(instance)
        assert len(instance.inputs) == 1
        assert next(iter(instance.receiver.rows.values()))["state"] == "failed"
    asyncio.run(run())


def test_new_scope_and_activation_get_separate_sessions(factory):
    async def run():
        instance = factory()
        for scope, activation, conversation in [(10, 20, 30), (11, 21, 30), (10, 22, 30), (12, 23, 31)]:
            incoming = event(scope=scope, activation=activation, conversation=conversation)
            instance.resource.load_initialization.side_effect = lambda *args, value=snapshot(incoming), **kw: value
            instance.resource.activation_messages.side_effect = lambda *args, value=snapshot(incoming), **kw: value
            await instance.receiver.accept(incoming)
            await idle(instance)
        assert len({item.source.chat_id for item in instance.inputs}) == 4
    asyncio.run(run())


def test_gateway_requires_signature_even_when_ordinary_verification_disabled(factory, monkeypatch):
    from inkbox_plugin import adapter as adapter_module

    async def run():
        instance = factory()
        provider = types.SimpleNamespace(name="inkbox", verify=Mock(return_value=False))
        monkeypatch.setattr(adapter_module, "match_provider", lambda headers: provider)
        request = types.SimpleNamespace(read=AsyncMock(return_value=json.dumps(event()).encode()), headers={}, url="https://example.com/webhook")
        response = await instance.adapter._handle_webhook(request)
        assert response.status == 401
        assert not instance.receiver.rows
        provider.verify.return_value = True
        response = await instance.adapter._handle_webhook(request)
        assert response.status == 202
        assert list(instance.receiver.root.glob("*.json"))
        await idle(instance)
        assert len(instance.inputs) == 1
    asyncio.run(run())


def test_external_companion_json_does_not_authorize(factory, monkeypatch):
    from inkbox_plugin import adapter as adapter_module

    async def run():
        instance = factory()
        monkeypatch.setattr(adapter_module, "match_provider", lambda headers: None)
        request = types.SimpleNamespace(read=AsyncMock(return_value=json.dumps(event()).encode()), headers={}, url="https://example.com/webhook")
        response = await instance.adapter._handle_webhook(request)
        assert response.status == 200 and not instance.receiver.rows
    asyncio.run(run())


def test_ordinary_cannot_supply_history(factory):
    async def run():
        instance = factory()
        incoming = event(phase="ordinary")
        incoming["companion"]["history"] = []
        with pytest.raises(ValueError, match="cannot carry"):
            await instance.receiver.accept(incoming)
        assert not instance.receiver.rows
    asyncio.run(run())


def test_email_tool_stored_reply_parent_preserves_audience(monkeypatch):
    identity = types.SimpleNamespace(reply_all_email=Mock(return_value=types.SimpleNamespace(id=uid(99))))
    monkeypatch.setattr(plugin_tools, "_client_and_identity", lambda: (None, None, identity))
    result = json.loads(plugin_tools.inkbox_send_email({"reply_to_message_id": uid(3), "body_text": "Hello group"}))
    assert result["ok"]
    identity.reply_all_email.assert_called_once_with(uid(3), subject=None, body_text="Hello group", body_html=None)
    result = json.loads(plugin_tools.inkbox_send_email({"reply_to_message_id": uid(3), "to": ["stranger@example.com"]}))
    assert "error" in result
    assert identity.reply_all_email.call_count == 1


def test_transient_hydration_retries_without_another_receipt(factory):
    async def run():
        instance = factory()
        instance.receiver.retry_delay = 0.01
        instance.resource.load_initialization.side_effect = [ConnectionError("Unavailable"), snapshot(event())]
        await instance.receiver.accept(event())
        await idle(instance)
        assert len(instance.inputs) == 1
        assert instance.resource.load_initialization.call_count == 2
    asyncio.run(run())


def test_revocation_discards_unsubmitted_snapshot(factory):
    from inkbox.exceptions import InkboxAPIError

    async def run():
        instance = factory()
        instance.resource.load_initialization.side_effect = InkboxAPIError(403, "Unavailable")
        await instance.receiver.accept(event())
        await idle(instance)
        assert not instance.inputs
        row = json.loads(next(instance.receiver.root.glob("*.json")).read_text())
        assert row["state"] == "revoked"
        assert not row["turns"]
        with pytest.raises(ValueError, match="revoked"):
            await instance.receiver.accept(event(phase="live", number=4))
    asyncio.run(run())


def test_one_process_owns_the_identity_checkpoint(factory):
    async def run():
        first = factory()
        first.receiver.root.mkdir()
        await first.receiver.start()
        second = factory(root=first.receiver.root)
        with pytest.raises(RuntimeError, match="Another Companion receiver"):
            await second.receiver.start()
        await first.receiver.close()
        await second.receiver.start()
        await second.receiver.close()
    asyncio.run(run())


def test_restart_refuses_live_if_initialized_host_session_was_lost(factory):
    async def run():
        first = factory()
        await first.receiver.accept(event())
        await idle(first)
        await first.receiver.close()
        second = factory(root=first.receiver.root)
        second.host.session_store.get_or_create_session = lambda source: types.SimpleNamespace(
            session_id="different-session", session_key=source.chat_id,
        )
        await second.receiver.start()
        await second.receiver.accept(event(phase="live", number=4))
        await idle(second)
        assert not second.inputs
        assert "host session is unavailable" in next(iter(second.receiver.rows.values()))["error"]
    asyncio.run(run())


def test_explicit_history_participant_denial_stops_initialization(factory):
    async def run():
        instance = factory()
        instance.host.denied.add("nancy@example.com")
        await instance.receiver.accept(event())
        await idle(instance)
        assert not instance.inputs
    asyncio.run(run())


def test_pending_approval_cannot_consume_bystander_live_message(factory):
    async def run():
        instance = factory()
        await instance.receiver.accept(event())
        await idle(instance)
        first = instance.inputs[0]
        instance.host._pending_approvals[instance.receiver._session_key(first)] = {"requested_by": "owner@example.com"}
        await instance.receiver.accept(event(phase="live", number=4))
        await idle(instance)
        assert len(instance.inputs) == 1
        assert instance.host._pending_approvals
    asyncio.run(run())


def test_sponsor_followup_obeys_initialization_barrier(factory):
    async def run():
        instance = factory()
        instance.gate.clear()
        await instance.receiver.accept(event())
        await wait_inputs(instance, 1)
        followup = event(phase="live", number=4)
        followup["data"]["message"].update(from_address="owner@example.com", body_text="A normal follow-up")
        await instance.receiver.accept(followup)
        assert len(instance.inputs) == 1
        instance.gate.set()
        await idle(instance)
        assert len(instance.inputs) == 2
        assert instance.inputs[1].source.user_id == "owner@example.com"
    asyncio.run(run())


@pytest.mark.parametrize("truncated", [False, True])
def test_live_email_uses_complete_body_and_original_parent(factory, truncated):
    async def run():
        instance = factory()
        await instance.receiver.accept(event())
        await idle(instance)
        incoming = event(phase="live", number=4)
        item = incoming["data"]["message"]
        item.pop("body_text")
        item.update(body="partial" if truncated else "complete message body", snippet="short preview")
        if truncated:
            item["body_state"] = "truncated"
            instance.identity.get_message = Mock(return_value={
                "id": uid(4), "thread_id": uid(30), "body_text": "complete message body",
                "attachment_metadata": [{"filename": "complete.txt"}],
            })
        await instance.receiver.accept(incoming)
        await idle(instance)
        assert len(instance.inputs) == 2
        assert "complete message body" in instance.inputs[1].text
        assert "short preview" not in instance.inputs[1].text
        if truncated:
            instance.identity.get_message.assert_called_once_with(uid(4))
            assert "complete.txt" in instance.inputs[1].text
        result = await instance.adapter.send(instance.inputs[1].source.chat_id, "Reply", reply_to=instance.inputs[1].message_id)
        assert result.success
        instance.identity.reply_all_email.assert_called_once_with(uid(3), body_text="Reply")
    asyncio.run(run())


def test_companion_media_destination_ignores_sender_and_route_overrides(factory):
    async def run():
        instance = factory("imessage")
        instance.gate.clear()
        await instance.receiver.accept(event("imessage"))
        await wait_inputs(instance, 1)
        chat_id = instance.inputs[0].source.chat_id
        assert instance.adapter._is_imessage_media_route(chat_id, {"mode": "email"})
        target = await instance.adapter._resolve_imessage_destination(chat_id, {"conversation_id": uid(999)})
        assert target == (uid(30), "", "")
        instance.gate.set()
        await idle(instance)
    asyncio.run(run())


def test_live_reply_context_cannot_change_the_cohort(factory):
    async def run():
        instance = factory()
        await instance.receiver.accept(event())
        await idle(instance)
        incoming = event(phase="live", number=4)
        incoming["companion"]["reply_context"] = {
            "channel": "mail", "conversation_id": uid(30), "reply_to_message_id": uid(4),
            "to": ["stranger@example.com"], "cc": [],
        }
        await instance.receiver.accept(incoming)
        await idle(instance)
        assert len(instance.inputs) == 1
        assert "cohort" in next(iter(instance.receiver.rows.values()))["error"]
    asyncio.run(run())


def test_inline_history_is_never_staged_or_used(factory):
    async def run():
        instance = factory()
        incoming = event()
        incoming["companion"]["history"] = [{"text": "unverified inline content"}]
        await instance.receiver.accept(incoming)
        await idle(instance)
        assert len(instance.inputs) == 1
        assert "unverified inline content" not in instance.inputs[0].text
        assert "unverified inline content" not in next(instance.receiver.root.glob("*.json")).read_text()
    asyncio.run(run())


def test_start_without_companion_receipts_leaves_ordinary_startup_unchanged(factory):
    async def run():
        instance = factory()
        await instance.receiver.start()
        assert not instance.receiver.root.exists()
        assert not instance.receiver.tasks
        instance.resource.load_initialization.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("missing", ["companion", "load_initialization", "activation_messages"])
def test_incompatible_sdk_reports_requirement_without_host_input(factory, monkeypatch, missing):
    from inkbox_plugin import adapter as adapter_module

    async def run():
        instance = factory()
        if missing == "companion":
            del instance.adapter._inkbox.companion
        else:
            delattr(instance.resource, missing)
        provider = types.SimpleNamespace(name="inkbox", verify=Mock(return_value=True))
        monkeypatch.setattr(adapter_module, "match_provider", lambda headers: provider)
        request = types.SimpleNamespace(read=AsyncMock(return_value=json.dumps(event()).encode()), headers={}, url="https://example.com/webhook")
        response = await instance.adapter._handle_webhook(request)
        assert response.status == 503
        assert "Incompatible Inkbox SDK" in response.text and ">=0.7.6" in response.text
        assert not instance.inputs and not instance.receiver.rows
        assert not instance.receiver.root.exists()
    asyncio.run(run())


def test_recovered_initialization_reports_missing_sdk_helper(factory):
    async def run():
        first = factory()
        await first.receiver.accept(event())
        await first.receiver.close()
        second = factory(root=first.receiver.root)
        del second.resource.load_initialization
        await second.receiver.start()
        await idle(second)
        row = next(iter(second.receiver.rows.values()))
        assert row["state"] == "failed"
        assert "Incompatible Inkbox SDK" in row["error"] and ">=0.7.6" in row["error"]
        assert not first.inputs and not second.inputs
        await second.receiver.close()
    asyncio.run(run())


@pytest.mark.parametrize("denial", ["sponsor", "participant"])
@pytest.mark.parametrize("channel", ["mail", "phone", "imessage"])
def test_reply_reuses_signed_route_without_authorization_reads(factory, channel, denial):
    async def run():
        instance = factory(channel)
        instance.gate.clear()
        await instance.receiver.accept(event(channel))
        await wait_inputs(instance, 1)
        incoming = instance.inputs[0]
        if denial == "sponsor":
            instance.host.allow_sponsor = False
        else:
            instance.host.denied.add("nancy@example.com" if channel == "mail" else "+15555550103")
        result = await instance.adapter.send(incoming.source.chat_id, "Group reply", reply_to=incoming.message_id)
        assert result.success
        instance.resource.activation_messages.assert_not_called()
        method = {"mail": "reply_all_email", "phone": "send_text", "imessage": "send_imessage"}[channel]
        getattr(instance.identity, method).assert_called_once()
        await instance.receiver.close()
    asyncio.run(run())


def test_closed_receiver_fences_dispatch(factory):
    async def run():
        first = factory()
        await first.receiver.accept(event())
        await idle(first)
        incoming = first.inputs[0]
        await first.receiver.close()
        result = await first.receiver.send(incoming.source.chat_id, "Reply", incoming.message_id)
        assert not result.success
        first.identity.reply_all_email.assert_not_called()
        first.resource.activation_messages.assert_not_called()
    asyncio.run(run())


def test_close_keeps_lock_until_inflight_sdk_send_finishes(factory):
    async def run():
        first = factory()
        await first.receiver.accept(event())
        await idle(first)
        incoming = first.inputs[0]
        started, release = threading.Event(), threading.Event()

        def send(*args, **kwargs):
            started.set()
            release.wait(5)
            return types.SimpleNamespace(id=uid(800))

        first.identity.reply_all_email.side_effect = send
        sending = asyncio.create_task(first.receiver.send(incoming.source.chat_id, "Reply", incoming.message_id))
        assert await asyncio.to_thread(started.wait, 2)
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        closing = asyncio.create_task(first.receiver.close())
        await asyncio.sleep(0)
        second = factory(root=first.receiver.root)
        with pytest.raises(RuntimeError, match="Another Companion receiver"):
            await second.receiver.start()
        assert not closing.done()
        release.set()
        await closing
        await second.receiver.start()
        await second.receiver.close()
        first.identity.reply_all_email.assert_called_once()
    asyncio.run(run())


@pytest.mark.parametrize("channel", ["mail", "phone", "imessage"])
@pytest.mark.parametrize("mismatch", ["conversation", "channel", "missing", "invalid"])
def test_failure_interception_requires_canonical_channel_and_conversation(factory, monkeypatch, channel, mismatch):
    from aiohttp import web
    from inkbox_plugin import adapter as adapter_module

    async def run():
        instance = factory(channel)
        await instance.receiver.accept(event(channel))
        await idle(instance)
        checkpoint = next(instance.receiver.root.glob("*.json"))
        before = checkpoint.read_bytes()
        failure_channel = ("phone" if channel == "mail" else "mail") if mismatch == "channel" else channel
        event_type = {"mail": "message.failed", "phone": "text.delivery_failed", "imessage": "imessage.delivery_failed"}[failure_channel]
        failed = {
            "id": uid(800), "direction": "outbound", "to_addresses": ["owner@example.com"],
            "remote_phone_number": "+15555550101", "remote_number": "+15555550101",
        }
        if mismatch != "missing":
            failed["thread_id" if failure_channel == "mail" else "conversation_id"] = (
                uid(99) if mismatch == "conversation" else "not-a-conversation-id" if mismatch == "invalid" else uid(30)
            )
        envelope = {"event_type": event_type, "data": {"text_message" if failure_channel == "phone" else "message": failed}}
        provider = types.SimpleNamespace(name="inkbox", verify=Mock(return_value=True))
        monkeypatch.setattr(adapter_module, "match_provider", lambda _headers: provider)
        ordinary = AsyncMock(return_value=web.Response(status=200, text="ordinary route"))
        setattr(instance.adapter, {"mail": "_on_mail_delivery_failure", "phone": "_on_text_lifecycle",
                                   "imessage": "_on_imessage_lifecycle"}[failure_channel], ordinary)
        request = types.SimpleNamespace(read=AsyncMock(return_value=json.dumps(envelope).encode()),
                                        headers={}, url="https://example.com/webhook")
        assert (await instance.adapter._handle_webhook(request)).text == "ordinary route"
        ordinary.assert_awaited_once_with(envelope)
        assert checkpoint.read_bytes() == before
        await instance.receiver.close()
    asyncio.run(run())
