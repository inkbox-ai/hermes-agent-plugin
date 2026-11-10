"""Durable Inkbox context around the official Hermes producer admission path.

Hermes owns authorization, FIFO execution and delivery. The plugin keeps its
private route context in its existing journal, never in the host wire envelope.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
from weakref import ref, WeakSet, WeakValueDictionary

from .conversation import reply_route

_BINDING = "inkbox_native_binding"
_EVENT_TYPES = {"message.received", "text.received", "imessage.received", "imessage.reaction_received", "whatsapp.received"}


def identity_scope(adapter):
    origin = getattr(adapter, "_base_url", None) or getattr(getattr(adapter, "_inkbox", None), "_base_url", None)
    values = [origin, getattr(adapter, "_identity_handle", None), getattr(adapter, "_identity_id", None)]
    if not all(isinstance(value, str) and value for value in values):
        return None
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _media_record(event):
    retained = []
    for value in event.media_urls or []:
        path = Path(value)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        retained.append({"sha256": digest.hexdigest(), "size": size})
    return retained


def _prepare_media(event):
    """Keep uncached attachments out of the host's local-file capture path."""
    fields = ("media_urls", "media_types", "media_text_inlined", "media_filenames")
    lists = {name: getattr(event, name) for name in fields if hasattr(event, name)}
    lists = {name: [] if value is None else value for name, value in lists.items()}
    for name, values in lists.items():
        if not isinstance(values, list):
            raise ValueError(f"Invalid {name}")
        valid = (all(value is None or type(value) is bool for value in values) if name == "media_text_inlined"
                 else all(isinstance(value, str) for value in values))
        if not valid:
            raise ValueError(f"Invalid {name}")
    urls = lists["media_urls"]
    if any(len(values) not in (0, len(urls)) for values in lists.values()):
        raise ValueError("Media metadata must align with attachments")
    kept = [index for index, value in enumerate(urls) if os.path.isabs(value)]
    if len(kept) == len(urls):
        return
    types = lists.get("media_types", [])
    notes = [f"[The user sent an attachment ({types[index] if types else 'file'}) that could not be retrieved.]"
             for index in range(len(urls)) if index not in kept]
    event.text = "\n".join(filter(None, [event.text, *notes]))
    for name, values in lists.items():
        setattr(event, name, [values[index] for index in kept] if values else [])


def event_record(event, *, captured_media=None):
    fields = ("user_id", "user_name", "platform_update_id", "reply_to_message_id", "reply_to_text",
        "reply_to_author_id", "reply_to_author_name", "reply_to_is_own_message", "channel_context",
        "reply_expected", "reply_anchor_override", "edit_revision", "inherit_channel_pin")
    return deepcopy({"source": event.source.to_dict(), "is_bot": event.source.is_bot,
        "message_id": event.message_id, "text": hashlib.sha256(event.text.encode()).hexdigest(), "metadata": event.metadata,
        "timestamp": event.timestamp.isoformat(),
        "media": _media_record(event) if captured_media is None else captured_media,
        "media_types": event.media_types or [],
        "channel_prompt": event.channel_prompt, "auto_skill": event.auto_skill,
        "allow_gateway_control": event.allow_gateway_control,
        **{name: getattr(event, name, None) for name in fields}})


def native_refs(event):
    binding = (event.metadata or {}).get(_BINDING)
    if isinstance(binding, dict) and binding.get("kind") == "queued":
        return binding.get("key"), binding.get("turn")
    raw = event.raw_message
    if isinstance(raw, dict):
        return raw.get("_inkbox_native_key"), raw.get("_inkbox_native_turn")
    return None, None


def _digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _slot(adapter, event, binding=None, *, create=False):
    companion = getattr(adapter, "_companion", None)
    chat_id = str(event.source.chat_id)
    if companion is not None and chat_id.startswith("companion:"):
        key = chat_id.removeprefix("companion:")
        row = companion.rows.get(key)
        turn = next((item for item in row["turns"] if item["id"] == event.message_id), None) if row else None
        expected_binding = {"kind": "companion", "key": key, "turn": event.message_id}
        if (turn is not None and (binding is None or all(binding.get(k) == v for k, v in expected_binding.items()))
                and (not create or companion.active.get(chat_id) is turn)):
            return turn, "native_admission", lambda: companion._save(row), expected_binding
        return None
    hosted = adapter._hosted_call_processing_data(event)
    registry = adapter._read_hosted_call_registry()
    for call_id, entry in registry.items():
        records = entry.get("native_admissions", {})
        if (hosted and hosted[0] == call_id and create) or str(event.message_id) in records:
            expected_binding = {"kind": "hosted", "key": call_id, "turn": event.message_id}
            if binding is not None and any(binding.get(k) != v for k, v in expected_binding.items()):
                continue
            if entry.get("state") not in {"queued", "running", "failed"}:
                return None
            records = entry.setdefault("native_admissions", {}) if create else records
            def save_hosted(records=records, call_id=call_id):
                from .adapter import _write_private_json_atomic
                current = adapter._read_hosted_call_registry()
                target = current.get(call_id)
                if target is None or target.get("state") not in {"queued", "running", "failed"}:
                    return
                target.setdefault("native_admissions", {}).update(records)
                _write_private_json_atomic(adapter._hosted_call_registry_path, current)
            return records, str(event.message_id), save_hosted, expected_binding
    queue = getattr(adapter, "_native_turns", None)
    if queue is not None:
        for row in queue.rows.values():
            if row.get("chat_id") != str(event.source.chat_id):
                continue
            for turn in row["turns"]:
                if turn.get("id") != event.message_id:
                    continue
                key, turn_id = native_refs(event)
                if binding is not None and (binding.get("kind") != "queued" or binding.get("key") != row["key"] or binding.get("turn") != turn["id"]):
                    continue
                if create and (key != row["key"] or turn_id != turn["id"] or queue.active.get(row["chat_id"]) is not turn):
                    continue
                return turn, "native_admission", lambda: queue._save(row), {"kind": "queued", "key": row["key"], "turn": turn["id"]}
    if binding is not None and binding.get("kind") != "ordinary":
        return None
    journal = adapter._conversation_state()
    row = journal.row(str(event.source.chat_id))
    if str(event.message_id) not in row["routes"]:
        return None
    records = row.setdefault("native_admissions", {}) if create else row.get("native_admissions", {})
    return records, str(event.message_id), lambda: journal.save(str(event.source.chat_id)), {"kind": "ordinary"}


def _lookup(adapter, event, *, wire=False):
    binding = (event.metadata or {}).get(_BINDING)
    slot = _slot(adapter, event, binding)
    record = slot[0].get(slot[1]) if slot else None
    if not isinstance(record, dict) or record.get("scope") != identity_scope(adapter) or record.get("scope") is None:
        return None
    expected = record.get("event")
    if not isinstance(expected, dict) or record.get("digest") != _digest(expected):
        return None
    if binding is not None and binding != {**slot[3], "scope": record["scope"], "digest": record["digest"]}:
        return None
    proof = getattr(event, "_inkbox_execution_admission", None)
    captured = None
    if (isinstance(proof, tuple) and len(proof) == 4 and proof[0]() is event
            and proof[1] == record["digest"] and tuple(event.media_urls or []) == proof[2]):
        captured = proof[3]
    actual = event_record(event, captured_media=captured)
    if wire:
        if actual["metadata"]:
            return None
        actual["metadata"] = deepcopy(expected["metadata"])
    else:
        actual["metadata"].pop(_BINDING, None)
        proof = getattr(event, "_inkbox_execution_admission", None)
        if isinstance(proof, tuple) and proof[0]() is event and proof[1] == record["digest"]:
            actual["text"] = expected["text"]
            actual["metadata"].pop("inkbox_interrupted", None)
    return (record, slot) if actual == expected else None


def route_for(adapter, event):
    if _BINDING not in (getattr(event, "metadata", None) or {}):
        return (getattr(event, "metadata", None) or {}).get("inkbox_reply_route")
    owned = _lookup(adapter, event)
    if owned is None:
        raise RuntimeError("Native event no longer owns its journal route")
    return deepcopy(owned[0]["event"]["metadata"].get("inkbox_reply_route"))


def eligible(adapter, event):
    metadata = event.metadata or {}
    prepared = metadata.get("inkbox_prepared") and isinstance(metadata.get("inkbox_reply_route"), dict)
    companion = isinstance(metadata.get("companion"), dict)
    hosted = adapter._hosted_call_processing_data(event) is not None
    if event.internal or event.get_command() or metadata.get("inkbox_control") or not (prepared or companion or hosted):
        return False
    try:
        from gateway.platforms.webhook_ingress import admit_producer
        from gateway.session_authorities import active_authority
    except ImportError:
        return False
    owner = getattr(adapter, "gateway_runner", None) or getattr(adapter._message_handler, "__self__", None)
    return callable(admit_producer) and owner is not None and active_authority(owner) is not None


async def remember_plain_voice(adapter, event):
    """Checkpoint a route-free voice input without changing Base admission."""
    raw = event.raw_message
    if (event.internal or event.metadata or getattr(event.source, "chat_topic", None) != "voice_call"
            or not isinstance(raw, dict)
            or adapter._hosted_call_processing_data(event) is not None):
        return
    try:
        from gateway.platforms.webhook_ingress import producer_scope
        from gateway.session_authorities import active_authority
    except ImportError:
        return
    owner = getattr(adapter, "gateway_runner", None) or getattr(adapter._message_handler, "__self__", None)
    if owner is None or active_authority(owner) is None:
        return
    _prepare_media(event)
    async with producer_scope(adapter, event):
        journal = adapter._conversation_state()
        key, message = str(event.source.chat_id), str(event.message_id)
        saved = event_record(event)
        saved["metadata"] = {"inkbox_plain_voice": True}
        record = {"scope": identity_scope(adapter), "event": saved, "digest": _digest(saved)}
        if record["scope"] is None:
            raise RuntimeError("Voice input has no configured identity")
        records = journal.row(key).setdefault("native_admissions", {})
        prior = records.get(message)
        if prior is not None and prior != record:
            raise RuntimeError("Voice input conflicts with its original journal record")
        journal.remember(key, message, {})
        records[message] = record
        journal.save(key)
        event._inkbox_plain_voice = True


async def dispatch(adapter, event):
    from gateway.platforms.webhook_ingress import admit_producer, producer_scope
    _prepare_media(event)
    # Canonicalize and bind the profile before persisting ownership. Admission
    # below still performs the host's complete authorization and durable write.
    async with producer_scope(adapter, event) as authority:
        scope = identity_scope(adapter)
        if scope is None:
            raise RuntimeError("Native admission requires a connected identity")
        metadata = event.metadata or {}
        if set(metadata) - {"inkbox_prepared", "inkbox_reply_route", "inkbox_event_type", "inkbox_buffer_context", "companion"}:
            raise RuntimeError("Native admission contains unsupported adapter context")
        slot = _slot(adapter, event, create=True)
        if slot is None:
            raise RuntimeError("Native admission has no owned journal turn")
        record_event = event_record(event)
        raw = event.raw_message or {}
        event_type = raw.get("event_type") if isinstance(raw, dict) else None
        if event_type in _EVENT_TYPES:
            record_event["metadata"]["inkbox_event_type"] = event_type
        if slot[3]["kind"] == "hosted":
            context = adapter._hosted_call_replay_envelope(raw)
            if context is None:
                raise RuntimeError("Hosted input has no bounded replay context")
            required = raw.get("_inkbox_hosted_sms_required")
            clauses = raw.get("_inkbox_hosted_sms_context", [])
            if not isinstance(required, bool) or not isinstance(clauses, list) or len(clauses) > 20 or any(not isinstance(item, str) or len(item) > 4000 for item in clauses):
                raise RuntimeError("Hosted input has invalid SMS context")
            context["_inkbox_hosted_sms_required"] = required
            context["_inkbox_hosted_sms_context"] = deepcopy(clauses)
            record_event["metadata"]["inkbox_hosted_context"] = context
        record = {"scope": scope, "event": record_event, "digest": _digest(record_event)}
        prior = slot[0].get(slot[1])
        if prior is not None and any(prior.get(key) != record[key] for key in record):
            raise RuntimeError("Native admission conflicts with its original journal turn")
        if prior is not None:
            record = prior
        slot[0][slot[1]] = record
        slot[2]()
        inputs = getattr(adapter, "_inkbox_producer_inputs", None)
        if inputs is None:
            inputs = adapter._inkbox_producer_inputs = WeakValueDictionary()
        inputs[record["digest"]] = event
        original_metadata, original_raw = event.metadata, event.raw_message
        try:
            # The entire private context remains durable above. Only the
            # official closed envelope crosses the host admission boundary.
            event.metadata = {}
            event.raw_message = None
            try:
                receipt = await admit_producer(adapter, event)
            except Exception as error:
                from hermes_state_runtime import RuntimeStoreError
                from .managed_work import AdmissionRejected
                # Only this native pre-write authorization clause proves no turn
                # started. A prior receipt or any other exception stays uncertain.
                if (isinstance(error, RuntimeStoreError) and error.reason == "permission_denied"
                        and error.field == "user_not_authorized" and not record.get("receipt")):
                    record["rejection"] = AdmissionRejected.code
                    slot[2]()
                    raise AdmissionRejected(AdmissionRejected.code) from error
                raise
            if receipt is None:
                raise RuntimeError("Native producer did not commit its input")
            record["receipt"] = {"id": receipt.admission_id, "session_id": receipt.ref.session_id,
                "profile_id": receipt.ref.profile_id}
            slot[2]()
            _watch_receipt(adapter, authority, receipt.admission_id, event)
        finally:
            event.metadata, event.raw_message = original_metadata, original_raw


def _ledger_row(authority, event, record):
    from hermes_state_runtime import get_session_admission, list_session_admissions
    receipt = record.get("receipt")
    if receipt:
        if receipt.get("profile_id") != authority.profile_id:
            raise RuntimeError("Native admission belongs to another profile")
        rows = [get_session_admission(authority.db, admission_id=receipt["id"])]
    else:
        claimed = getattr(event, "_claimed_owner", None)
        if not claimed:
            raise RuntimeError("Native replay has no claimed session")
        rows = list_session_admissions(authority.db, session_id=claimed[0], pending_only=False)
    source = event.source
    principal = "messaging:" + json.dumps([source.profile, source.platform.value, source.chat_id,
        source.thread_id, source.user_id], separators=(",", ":"))
    rows = [row for row in rows if row and row.get("principal_id") == principal and row.get("request_id") == str(event.message_id)]
    if len(rows) != 1:
        raise RuntimeError("Native replay has no exact admitted input")
    row = rows[0]
    claimed = getattr(event, "_claimed_owner", None)
    if (claimed is None or row.get("target_session_id") != claimed[0]
            or row.get("generation") != claimed[1] or row.get("owner_epoch") != authority.epoch):
        raise RuntimeError("Native replay belongs to another execution generation")
    return row


def bind_execution(adapter, event):
    """Restore only journal-owned data onto the actual authorized execution."""
    owned = _lookup(adapter, event, wire=True)
    if owned is None:
        raise RuntimeError("Native execution no longer owns its journal input")
    record, slot = owned
    from gateway.session_authorities import authority_for_home
    from gateway.session_identity import canonical_identity
    identity = canonical_identity(event.source, runner=adapter.gateway_runner)
    authority = authority_for_home(adapter.gateway_runner, identity.runtime_home) if identity is not None else None
    if authority is None:
        raise RuntimeError("Native execution has no serving authority")
    row = _ledger_row(authority, event, record)
    if row["status"] != "started":
        raise RuntimeError("Native execution is not claimed")
    binding = {**slot[3], "scope": record["scope"], "digest": record["digest"]}
    event.metadata = {**deepcopy(record["event"]["metadata"]), _BINDING: binding}
    event._inkbox_execution_admission = (ref(event), record["digest"], tuple(event.media_urls or []),
        deepcopy(record["event"]["media"]))
    event._inkbox_producer_execution = True
    original = getattr(adapter, "_inkbox_producer_inputs", {}).get(record["digest"])
    if original is not None:
        original._inkbox_execution_event = event
    event._inkbox_recovered_delivery = True  # never steal an unrelated Base session guard
    if binding["kind"] == "hosted":
        event.raw_message = deepcopy(record["event"]["metadata"]["inkbox_hosted_context"])
    if binding["kind"] == "companion":
        receiver = adapter._companion
        receiver._require_owner()
        companion_row = receiver.rows[binding["key"]]
        if receiver.closed or companion_row["state"] in {"paused", "failed", "revoked"}:
            raise RuntimeError("Companion execution is not active")
        turn = next(item for item in companion_row["turns"] if item["id"] == binding["turn"])
        active = receiver.active.get(str(event.source.chat_id))
        if active not in (None, turn) or turn.get("fenced") or turn.get("delivery", {}).get("state") in {"sending", "uncertain"} or turn.get("explicit_delivery_state") in {"sending", "uncertain"} or receiver._uncertain_media(turn):
            raise RuntimeError("Companion execution ownership is unresolved")
        if active is None:
            for other in companion_row["turns"]:
                if other is turn or other.get("fenced"):
                    continue
                if (other.get("delivery", {}).get("state") in {"sending", "uncertain"}
                        or other.get("explicit_delivery_state") in {"sending", "uncertain"}
                        or receiver._uncertain_media(other)
                        or (other.get("state") in {"submitting", "submitted", "uncertain", "control_submitting"}
                            and not other.get("result_ready"))):
                    raise RuntimeError("Companion replay conflicts with another unresolved turn")
            receiver.active[str(event.source.chat_id)] = turn
            event._inkbox_recovered_companion = True
        event.raw_message = {"_inkbox_companion_key": binding["key"], "_inkbox_companion_turn": binding["turn"]}
    if binding["kind"] == "queued":
        queue = adapter._native_turns
        queue._require_owner()
        native_row = queue.rows[binding["key"]]
        turn = next(item for item in native_row["turns"] if item["id"] == binding["turn"])
        current = queue.active.get(native_row["chat_id"])
        if queue.closed or current not in (None, turn):
            raise RuntimeError("Native replay conflicts with a physical owner")
        uncertain = (turn.get("reply_delivery_state") in {"sending", "uncertain"}
            or turn.get("explicit_delivery_state") in {"sending", "uncertain"}
            or any(item.get("state") in {"sending", "uncertain"} for item in turn.get("media_deliveries", {}).values()))
        if uncertain or turn["state"] in {"done", "cancelled", "quarantined", "sending"}:
            raise RuntimeError("Native replay has unresolved effects")
        if current is None:
            for other in native_row["turns"]:
                if other is turn:
                    continue
                pending_effect = (other.get("reply_delivery_state") in {"sending", "uncertain"}
                    or other.get("explicit_delivery_state") in {"sending", "uncertain"}
                    or any(item.get("state") in {"sending", "uncertain"}
                           for item in other.get("media_deliveries", {}).values()))
                pending_worker = other.get("state") in {"running", "sending", "uncertain", "cancelled"} and not other.get("fenced")
                if pending_worker or (pending_effect and (other.get("stop_worker_completion") or not queue._settled_proof(other))):
                    raise RuntimeError("Native replay conflicts with another unresolved turn")
            # Positive newly claimed core admission, not absence of a worker,
            # proves the durable queued input can resume after process restart.
            turn["state"] = "running"
            queue.active[native_row["chat_id"]] = turn
            event._inkbox_recovered_queue = True
            native_row.pop("blocked", None)
        queue.events[turn["id"]] = event
        event._inkbox_host_dispatched = True
        event._inkbox_worker_exited = lambda actual: queue._stopped_worker_exited(native_row, turn, actual)
        queue._save(native_row)
    # Observe settlement before inference can finish: a late admission receipt
    # may already be terminal while physical delivery is still in progress.
    _watch_receipt(adapter, authority, row["admission_id"], event)
    return authority, row


def _watch_receipt(adapter, authority, admission_id, original, *, plain_voice=False):
    if getattr(getattr(original, "_inkbox_execution_event", original), "_inkbox_producer_completed", False):
        return
    tasks = getattr(adapter, "_inkbox_receipt_tasks", None)
    if tasks is None:
        tasks = adapter._inkbox_receipt_tasks = {}
    if admission_id in tasks:
        original._inkbox_receipt_task = (ref(original), ref(tasks[admission_id]))
        return
    from hermes_state_runtime import get_session_admission
    row = get_session_admission(authority.db, admission_id=admission_id)
    waiter = None
    if row["status"] in {"queued", "started"}:
        waiter = authority.waiters.setdefault(admission_id, asyncio.get_running_loop().create_future())

    async def observe():
        try:
            if waiter is not None:
                await asyncio.shield(waiter)
            settled = get_session_admission(authority.db, admission_id=admission_id)
            actual = getattr(original, "_inkbox_execution_event", original)
            if plain_voice:
                journal = adapter._conversation_state()
                if settled["status"] == "terminal":
                    journal.complete(str(actual.source.chat_id), str(actual.message_id))
                return
            if not getattr(actual, "_inkbox_producer_completed", False):
                # A preclaim refusal or missing producer bracket is not success
                # merely because inference settled. The adapter's actual send
                # receipts and worker finalizer remain the delivery authority.
                outcome = "failure"
                if getattr(actual, "_inkbox_producer_execution", False) and settled["status"] == "terminal" and settled.get("outcome") == "completed":
                    from .host_fencing import completed_worker_proof
                    delivered = getattr(actual, "_inkbox_response_empty", False) or any(getattr(actual, "_inkbox_delivery_results", ()))
                    if delivered and completed_worker_proof(adapter.gateway_runner, actual) is not None:
                        outcome = "success"
                await complete(adapter, actual, outcome)
        except asyncio.CancelledError:
            if plain_voice:
                raise
            actual = getattr(original, "_inkbox_execution_event", original)
            if not getattr(actual, "_inkbox_producer_completed", False):
                await complete(adapter, actual, "cancelled")
            raise
        except Exception:
            if plain_voice:
                return
            actual = getattr(original, "_inkbox_execution_event", original)
            if not getattr(actual, "_inkbox_producer_completed", False):
                await complete(adapter, actual, "failure")
        finally:
            tasks.pop(admission_id, None)
    task = asyncio.create_task(observe())
    original._inkbox_receipt_task = (ref(original), ref(task))
    tasks[admission_id] = task
    adapter._background_tasks.add(task)
    task.add_done_callback(adapter._background_tasks.discard)


async def complete(adapter, event, outcome):
    if getattr(event, "_inkbox_producer_completed", False):
        return
    event._inkbox_producer_completed = True
    binding = getattr(event, "_inkbox_receipt_task", None)
    observer = binding[1]() if binding is not None and binding[0]() is event else None
    if observer is not None and observer is not asyncio.current_task():
        observer.cancel()
    if (event.metadata or {}).get("inkbox_interrupted"):
        outcome = "cancelled"
    await adapter.on_processing_complete(event, outcome)
    if getattr(event, "_inkbox_recovered_companion", False):
        receiver = adapter._companion
        receiver.active.pop(str(event.source.chat_id), None)
        binding = event.metadata[_BINDING]
        receiver._kick(receiver.rows[binding["key"]])
    if getattr(event, "_inkbox_recovered_queue", False):
        key, turn_id = native_refs(event)
        queue = adapter._native_turns
        row = queue.rows[key]
        if queue.events.get(turn_id) is event:
            queue.events.pop(turn_id, None)
        if queue.active.get(row["chat_id"], {}).get("id") == turn_id:
            queue.active.pop(row["chat_id"], None)
        queue._kick(row)


def install(adapter, owner):
    """One scoped host-instance decorator; never replace host authorization/FIFO."""
    if owner is None or not callable(getattr(owner, "_handle_message", None)):
        return
    try:
        from gateway.session_ingress import executing_admission
    except ImportError:
        return
    existing = owner._handle_message
    if getattr(existing, "_inkbox_producer_bridge", False):
        existing._inkbox_adapters.add(adapter)
        return
    adapters = WeakSet([adapter])
    @wraps(existing)
    async def handle(event, *args, **kwargs):
        current = owner._adapter_for_source(event.source) if executing_admission.get() else None
        if current not in adapters or event.internal:
            return await existing(event, *args, **kwargs)
        plain = _lookup(current, event, wire=True) if not event.metadata else None
        if plain is not None and plain[0]["event"]["metadata"] == {"inkbox_plain_voice": True}:
            from gateway.session_authorities import authority_for_home
            from gateway.session_identity import canonical_identity
            identity = canonical_identity(event.source, runner=owner)
            authority = authority_for_home(owner, identity.runtime_home) if identity is not None else None
            if authority is None:
                raise RuntimeError("Voice execution has no serving authority")
            row = _ledger_row(authority, event, plain[0])
            if row["status"] != "started":
                raise RuntimeError("Voice execution is not claimed")
            _watch_receipt(current, authority, row["admission_id"], event, plain_voice=True)
            token = reply_route.set(None)
            try:
                return await existing(event, *args, **kwargs)
            finally:
                reply_route.reset(token)
        missing_bracket = not getattr(event, "_inkbox_producer_execution", False)
        if missing_bracket:
            authority, row = bind_execution(current, event)
            await current.on_processing_start(event)
            _watch_receipt(current, authority, row["admission_id"], event)
        elif _lookup(current, event) is None:
            raise RuntimeError("Native execution ownership changed")
        token = reply_route.set(route_for(current, event))
        try:
            response = await existing(event, *args, **kwargs)
            event._inkbox_response_empty = not current._unwrap_ephemeral(response)[0]
            companion = getattr(current, "_companion", None)
            if companion:
                companion.capture_result(event, response)
            native = getattr(current, "_native_turns", None)
            if native and response:
                native.capture_result(event, response)
            return None if (event.metadata or {}).get("inkbox_interrupted") else response
        finally:
            reply_route.reset(token)
    handle.__self__ = owner
    handle._inkbox_producer_bridge = True
    handle._inkbox_adapters = adapters
    handle._inkbox_original = existing
    owner._handle_message = handle


def uninstall(adapter, owner):
    handler = getattr(owner, "_handle_message", None)
    if not getattr(handler, "_inkbox_producer_bridge", False):
        return
    handler._inkbox_adapters.discard(adapter)
    if not handler._inkbox_adapters:
        owner._handle_message = handler._inkbox_original


@asynccontextmanager
async def delivery_scope(adapter, event):
    if _BINDING not in (getattr(event, "metadata", None) or {}):
        yield
        return
    if event.metadata.get("inkbox_interrupted"):
        raise RuntimeError("Native delivery was interrupted")
    binding = event.metadata[_BINDING]
    if binding.get("kind") == "queued":
        queue = adapter._native_turns
        queue._require_owner()
        if queue.rows[binding["key"]].get("blocked"):
            raise RuntimeError("Native delivery ownership is unresolved")
    token = reply_route.set(route_for(adapter, event))
    try:
        yield
    except (Exception, asyncio.CancelledError) as exc:
        await complete(adapter, event, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failure")
        raise
    finally:
        reply_route.reset(token)


def delivery_callback(event, callback):
    """Observe the exact host delivery verdict without changing its collector."""
    def record(result):
        if getattr(event, "_inkbox_producer_execution", False):
            results = getattr(event, "_inkbox_delivery_results", None)
            if results is None:
                results = event._inkbox_delivery_results = []
            results.append(bool(getattr(result, "success", False)))
        callback(result)
    return record
