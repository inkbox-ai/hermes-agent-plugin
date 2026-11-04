"""Journal-bound native admission and process-local execution ownership."""
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
import hashlib
import json
from weakref import ReferenceType, ref

from .conversation import reply_route

_BINDING = "inkbox_native_binding"
_EVENT_TYPES = {"message.received", "text.received", "imessage.received", "imessage.reaction_received", "whatsapp.received"}


def identity_scope(adapter):
    values = [getattr(adapter, name, None) for name in ("_base_url", "_identity_handle", "_identity_id")]
    if not all(isinstance(value, str) and value for value in values):
        return None
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def event_record(event):
    """Only immutable routing/context; media paths are captured/remapped by the host."""
    # Bind the same deterministic unavailable-attachment note as the core;
    # retain exact text equality, including this fallback, across restoration.
    from gateway.session_envelope import _drop_remote_media
    event = _drop_remote_media(event)
    fields = ("user_id", "user_name", "platform_update_id", "reply_to_message_id", "reply_to_text",
        "reply_to_author_id", "reply_to_author_name", "reply_to_is_own_message", "channel_context",
        "reply_expected", "reply_anchor_override", "edit_revision", "inherit_channel_pin")
    return deepcopy({"source": event.source.to_dict(), "is_bot": event.source.is_bot,
        "message_id": event.message_id, "text": event.text, "metadata": event.metadata,
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


def _record_slot(adapter, event, binding, *, create=False, admission=True):
    if binding.get("kind") == "queued":
        queue = getattr(adapter, "_native_turns", None)
        if queue is None or (admission and queue.closed):
            return None
        row = queue.rows.get(binding.get("key"))
        turn = queue.active.get(str(event.source.chat_id))
        if (not row or not turn or not any(item is turn for item in row["turns"])
                or turn.get("id") != binding.get("turn") or turn.get("id") != event.message_id
                or row.get("chat_id") != str(event.source.chat_id) or (admission and row.get("blocked"))
                or (admission and turn.get("state") != "running")):
            return None
        return turn, "native_admission", lambda: queue._save(row)
    if binding.get("kind") == "ordinary":
        journal = adapter._conversation_state()
        journal_row = journal.row(str(event.source.chat_id))
        if str(event.message_id) not in journal_row["routes"]:
            return None
        records = journal_row.setdefault("native_admissions", {}) if create else journal_row.get("native_admissions", {})
        return records, str(event.message_id), lambda: journal.save(str(event.source.chat_id))
    return None


def stamp(adapter, event):
    """Stamp at the last adapter-owned boundary, never from restored raw input."""
    try:
        from gateway.session_adapter_execution import execute_native_handler
    except ImportError:
        return False
    if not callable(execute_native_handler):
        return False
    from gateway.session_authorities import active_authority
    owner = getattr(adapter, "gateway_runner", None)
    if owner is None or active_authority(owner) is None:
        return False
    metadata = event.metadata or {}
    route = metadata.get("inkbox_reply_route")
    if not metadata.get("inkbox_prepared") or not isinstance(route, dict) or metadata.get("inkbox_control"):
        return False
    scope = identity_scope(adapter)
    if scope is None:
        raise RuntimeError("Native admission requires a connected identity")
    key, turn = native_refs(event)
    binding = {"kind": "queued" if key else "ordinary", "scope": scope}
    if key:
        binding.update(key=key, turn=turn)
    raw = event.raw_message or {}
    event_type = raw.get("event_type") if isinstance(raw, dict) else None
    if set(metadata) - {"inkbox_prepared", "inkbox_reply_route", "inkbox_event_type", "inkbox_buffer_context"}:
        raise RuntimeError("Native admission contains unsupported adapter context")
    record = event_record(event)
    if event_type in _EVENT_TYPES:
        record["metadata"]["inkbox_event_type"] = event_type
    binding["digest"] = _digest(record)
    slot = _record_slot(adapter, event, binding, create=True)
    if slot is None:
        raise RuntimeError("Native admission has no owned journal turn")
    records, record_key, save = slot
    previous = records.get(record_key)
    if previous is not None and previous != record:
        raise RuntimeError("Native admission conflicts with its original journal turn")
    records[record_key] = record
    save()
    event.metadata = _compact_metadata(record, binding)
    return True


def _digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _compact_metadata(record, binding):
    metadata = {"inkbox_prepared": True, _BINDING: deepcopy(binding)}
    for name in ("inkbox_event_type", "inkbox_buffer_context"):
        if name in record["metadata"]:
            metadata[name] = record["metadata"][name]
    return metadata


def _matching_record(adapter, event, *, admission=True):
    binding = (event.metadata or {}).get(_BINDING)
    if (not isinstance(binding, dict) or binding.get("scope") != identity_scope(adapter)
            or binding.get("scope") is None):
        return None
    expected = {"kind", "scope", "digest", "key", "turn"} if binding.get("kind") == "queued" else {"kind", "scope", "digest"}
    if set(binding) != expected:
        return None
    slot = _record_slot(adapter, event, binding, admission=admission)
    record = slot[0].get(slot[1]) if slot is not None else None
    if record is None or binding["digest"] != _digest(record):
        return None
    expected_event = deepcopy(record)
    expected_event["metadata"] = _compact_metadata(record, binding)
    actual = event_record(event)
    if not admission:
        proof = getattr(event, "_inkbox_execution_admission", None)
        if isinstance(proof, tuple) and len(proof) == 2 and isinstance(proof[0], ReferenceType) and proof[0]() is event:
            admitted = json.loads(proof[1])
            if admitted == expected_event:
                # Only this exact executed event may carry host-expanded prompt
                # text. Source, metadata, identity and current journal still match.
                actual["text"] = admitted["text"]
    if not admission and actual["metadata"].get("inkbox_interrupted") is True:
        actual["metadata"].pop("inkbox_interrupted")
    return record if actual == expected_event else None


def validate(adapter, event):
    """Pure validation: a persisted descriptor is a reference, not authorization."""
    return _matching_record(adapter, event) is not None


def route_for(adapter, event):
    if _BINDING not in (event.metadata or {}):
        return (event.metadata or {}).get("inkbox_reply_route")
    record = _matching_record(adapter, event, admission=False)
    if record is None:
        raise RuntimeError("Native event no longer owns its journal route")
    return deepcopy(record["metadata"]["inkbox_reply_route"])


def require_completed_worker(adapter, event):
    """A host refusal/notice is not proof that the owned model worker finished."""
    key, _ = native_refs(event)
    if not key:
        return True
    from .host_fencing import completed_worker_proof
    if completed_worker_proof(adapter.gateway_runner, event) is not None:
        return True
    event._inkbox_execution_failed = True
    owned = adapter._native_turns._owned(event)
    if owned:
        row, turn = owned
        if turn["state"] not in {"cancelled", "done", "uncertain"}:
            turn["state"] = "uncertain"
            row["blocked"] = True
            adapter._native_turns._save(row)
    return False


@asynccontextmanager
async def execution_scope(adapter, event):
    if _BINDING not in (event.metadata or {}):
        yield
        return
    if not validate(adapter, event):
        raise RuntimeError("Native execution no longer owns its journal turn")
    from gateway.session_authorities import active_authority
    authority = active_authority(getattr(adapter, "gateway_runner", None))
    if authority is None:
        raise RuntimeError("Native execution has no serving authority")
    # The authority owns live messaging waiters, including shared-busy inputs.
    # A session guard alone may belong to an earlier/later message, or outlive
    # a waiter released by session_busy while its admission remains queued.
    original = next((owner for owner in getattr(authority, "marker_adopters", {}).values()
                     if owner.message_id == event.message_id and event_record(owner) == event_record(event)), None)
    queue = getattr(adapter, "_native_turns", None)
    native_key, turn_id = native_refs(event)
    if native_key:
        original = queue.events.get(turn_id)
    if original is not None:
        if event_record(original) != event_record(event):
            raise RuntimeError("Native execution differs from its delivery owner")
        original._inkbox_execution_event = event
        event._inkbox_processing_owner = ref(original)
    if original is None and not native_key:
        event._inkbox_recovered_delivery = True
    # Process-local and immutable: snapshot/restore never persists this proof.
    event._inkbox_execution_admission = (ref(event), json.dumps(event_record(event), ensure_ascii=False))
    token = reply_route.set(route_for(adapter, event))
    try:
        if native_key:
            row, turn = queue._owned(event)
            queue.events[turn_id] = event
            event._inkbox_host_dispatched = True
            event._inkbox_worker_exited = lambda actual: queue._stopped_worker_exited(row, turn, actual)
        await adapter.on_processing_start(event)
        yield
    except BaseException:
        event._inkbox_execution_failed = True
        if native_key and turn["state"] not in {"cancelled", "done", "uncertain"}:
            turn["state"] = "uncertain"
            row["blocked"] = True
            queue._save(row)
        raise
    finally:
        reply_route.reset(token)



@contextmanager
def delivery_scope(adapter, event):
    """Recovered delivery has no original adapter task's ambient route context."""
    binding = (event.metadata or {}).get(_BINDING)
    if binding is None:
        yield
        return
    if (event.metadata or {}).get("inkbox_interrupted"):
        raise RuntimeError("Native delivery was interrupted")
    if binding.get("kind") == "queued":
        queue = getattr(adapter, "_native_turns", None)
        row = queue.rows.get(binding.get("key")) if queue is not None else None
        if row is None or queue.closed or row.get("blocked"):
            raise RuntimeError("Native delivery ownership is unresolved")
    token = reply_route.set(route_for(adapter, event))
    try:
        yield
    finally:
        reply_route.reset(token)
