"""Durable ordinary Slack and native iMessage turns owned by the Hermes adapter."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time

from gateway.platforms.base import MessageEvent, MessageType, SendResult

logger = logging.getLogger(__name__)

try:
    from .companion import CompanionReceiver, digest
    from .conversation import approval_reply, conversation_control, reply_route
    from .imessage_state import bind_context, clear_context, validate_reply_target, ReplyPreflightError
    from .slack import send_reply
except ImportError:  # pragma: no cover - direct local import
    from companion import CompanionReceiver, digest
    from conversation import approval_reply, conversation_control, reply_route
    from imessage_state import bind_context, clear_context, validate_reply_target, ReplyPreflightError
    from slack import send_reply


class NativeTurns(CompanionReceiver):
    """Serial native host admission, checkpointed answers, and immutable routes.

    This queue never creates a host session for a native iMessage subthread.
    One ordinary conversation owns one queue, irrespective of reply ancestry.
    """

    quiet_seconds = .75
    max_burst_seconds = 2.0
    max_burst_sources = 8
    max_burst_characters = 4000
    max_delivery_notices = 8

    def __init__(self, adapter, root):
        super().__init__(adapter, root)
        self.changed: dict[str, asyncio.Event] = {}
        self.events: dict[str, MessageEvent] = {}
        self.monitors: dict[str, asyncio.Task] = {}
        self.delivery_status: dict[str, dict] = {}
        self.unconfirmed_controls: dict[str, tuple[str, str]] = {}

    async def start(self):
        from .host_fencing import observe_native_workers
        observe_native_workers(self._host_owner())
        self._acquire()
        status_path = self.root / "delivery-status"
        if status_path.exists():
            saved = json.loads(status_path.read_text())
            if saved.get("version") != 1 or not isinstance(saved.get("messages"), dict):
                raise ValueError("Invalid native delivery status checkpoint")
            self.delivery_status = saved["messages"]
        for path in self.root.glob("*.json"):
            row = json.loads(path.read_text())
            if row.get("key") != path.stem:
                raise ValueError("Invalid native turn checkpoint")
            self.rows[row["key"]] = row
            for control, state in row.get("controls", {}).items():
                if state == "submitting":
                    row["controls"][control] = "uncertain"
            for turn in row["turns"]:
                # A new process holds the exclusive owner lock. Do not replay
                # an ambiguous model/tool/send attempt, but retain its evidence.
                uncertain_effect = (turn.get("explicit_delivery_state") in {"sending", "uncertain"}
                    or any(item.get("state") in {"sending", "uncertain"}
                           for item in turn.get("media_deliveries", {}).values()))
                if (turn["state"] in {"running", "sending", "control_submitting"}
                        or (turn["state"] in {"pending", "answer_ready"} and uncertain_effect)):
                    turn["state"] = "uncertain"
                turn.pop("session_id", None)
                if turn["state"] == "cancelled":
                    self._release_context(row, turn)
                if self._settled_proof(turn):
                    turn["fenced"] = True
                if turn["state"] == "uncertain" and not turn.get("fenced"):
                    row["blocked"] = True
                    row["error"] = "Original worker or delivery ownership is uncertain; no automatic replay"
            if row.get("blocked") and not any(turn["state"] in {"running", "sending", "uncertain"} and not turn.get("fenced") for turn in row["turns"]):
                row.pop("blocked", None)
            self._save(row)
            self._restore_delivery_records(row)
            self._kick(row)

    def _event_from_turn(self, row, turn):
        value = turn["event"]
        source = self.adapter.build_source(**{**value["source"], "message_id": turn["id"]})
        event = MessageEvent(text=value["text"], message_type=MessageType.TEXT, source=source,
                             message_id=turn["id"], metadata=copy.deepcopy(value["metadata"]),
                             raw_message={"_inkbox_native_turn": turn["id"], "_inkbox_native_key": row["key"]},
                             channel_prompt=value.get("channel_prompt"), auto_skill=value.get("auto_skill"),
                             media_urls=value.get("media_urls") or [], media_types=value.get("media_types") or [])
        event.allow_gateway_control = False
        return event

    def _serialize(self, event):
        source = event.source
        return {"text": event.text, "metadata": copy.deepcopy(event.metadata or {}),
                "source": {key: getattr(source, key) for key in (
                    "chat_id", "chat_name", "chat_type", "thread_id", "user_id", "user_id_alt", "user_name", "message_id",
                ) if hasattr(source, key)},
                "channel_prompt": getattr(event, "channel_prompt", None), "auto_skill": getattr(event, "auto_skill", None),
                "media_urls": getattr(event, "media_urls", None), "media_types": getattr(event, "media_types", None)}

    async def accept(self, event):
        self._require_owner()
        route = event.metadata["inkbox_reply_route"]
        # Native authorization happens before activity and before receipt ACK.
        owner = self._host_owner()
        check = getattr(owner, "_is_user_authorized", None)
        if not callable(check):
            raise RuntimeError("Native channel turns require Hermes authorization")
        if not check(event.source):
            return
        key = digest(json.dumps([str(event.source.chat_id), route["mode"], route.get("conversation_id")]))
        row = self.rows.setdefault(key, {"version": 1, "key": key, "state": "ordinary", "turns": [], "chat_id": str(event.source.chat_id)})
        source_id = str(event.message_id)
        if self.acknowledge_control(source_id):
            return
        if self.acknowledge_source(source_id):
            return
        control_fingerprint = digest(json.dumps({"source": self._serialize(event)["source"], "route": route}, sort_keys=True))
        control_owner = (self.active.get(row["chat_id"]) or {}).get("id", source_id)
        if source_id in self.unconfirmed_controls and self.unconfirmed_controls[source_id] != (control_fingerprint, control_owner):
            raise RuntimeError("Unconfirmed control receipt changed; no native control attempted")
        text = route.get("raw_text", event.text).strip()
        command = conversation_control(text) or text.split(maxsplit=1)[:1] in [["/approve"], ["/deny"]]
        if await self.control(event, row):
            row.setdefault("controls", {})[source_id] = "consumed"
            self._save(row)
            return
        if source_id in self.unconfirmed_controls and not command:
            # A retried prompt answer cannot become ordinary work if the
            # original native prompt has meanwhile been withdrawn.
            row.setdefault("controls", {})[source_id] = "not_owner"
            self.unconfirmed_controls.pop(source_id, None)
            self._save(row)
            return
        if command:
            if self.active.get(row["chat_id"]):
                row.setdefault("controls", {})[source_id] = "not_owner"
                self.unconfirmed_controls.pop(source_id, None)
            else:
                control_turn = next((turn for turn in row["turns"] if turn["id"] == source_id), None)
                if control_turn is None:
                    control_turn = {"id": source_id, "state": "control_submitting", "event": self._serialize(event),
                                    "source_ids": [source_id], "route": copy.deepcopy(route)}
                    row["turns"].append(control_turn)
                self._checkpoint_control(event, row, source_id, control_turn, source_id)
                if text.casefold() in {"/stop", "/cancel", "/clear", "/new"}:
                    for pending in row["turns"]:
                        if pending["state"] == "pending":
                            pending["state"] = "cancelled"
                    self._checkpoint_control(event, row, source_id, control_turn, source_id)
                    for pending in row["turns"]:
                        if pending["state"] == "cancelled":
                            self._release_context(row, pending)
                # The next operation crosses the actual native effect boundary.
                self.unconfirmed_controls.pop(source_id, None)
                await self._forward_control(event, text, control_turn)
                control_turn["state"] = "done"
                row["controls"][source_id] = "consumed"
            self._save(row)
            return
        turn = {"id": source_id, "state": "pending", "event": self._serialize(event), "source_ids": [source_id],
                "first_at": time.time(), "last_at": time.time(), "route": copy.deepcopy(route)}
        row["turns"].append(turn)
        try:
            self._save(row)
        except BaseException:
            # A failure before publication can be retried as a fresh receipt.
            # After atomic replacement, retain the row and its context owner;
            # a duplicate must retry the durable checkpoint before ACK/kick.
            try:
                saved = json.loads((self.root / (key + ".json")).read_text())
                published = any(item == turn for item in saved.get("turns", []))
            except FileNotFoundError:
                published = False
            except (OSError, ValueError):
                published = True  # Unreadable storage is not proof of absence.
            if not published:
                row["turns"].remove(turn)
                self._discard_empty_row(row)
            raise
        self.changed.setdefault(key, asyncio.Event()).set()
        if row.get("blocked") and row["chat_id"] not in self.active:
            await self._recover_fenced_native(row)
        self._kick(row)
        return True

    def _discard_empty_row(self, row):
        if not row["turns"] and not row.get("controls") and self.rows.get(row["key"]) is row:
            self.rows.pop(row["key"])

    def _checkpoint_control(self, event, row, source_id, control_turn=None, native_owner=None):
        """Retain retry authority only until this process attempts a native effect."""
        fingerprint = digest(json.dumps({"source": self._serialize(event)["source"],
            "route": event.metadata["inkbox_reply_route"]}, sort_keys=True))
        proof = (fingerprint, native_owner or source_id)
        previous = self.unconfirmed_controls.get(source_id)
        if previous is not None and previous != proof:
            raise RuntimeError("Unconfirmed control receipt changed; no native control attempted")
        self.unconfirmed_controls[source_id] = proof
        row.setdefault("controls", {})[source_id] = "submitting"
        try:
            self._save(row)
        except BaseException:
            try:
                saved = json.loads((self.root / (row["key"] + ".json")).read_text())
                published = saved.get("controls", {}).get(source_id) == "submitting"
                if control_turn is not None:
                    published = published and any(item == control_turn for item in saved.get("turns", []))
            except FileNotFoundError:
                published = False
            except (OSError, ValueError):
                published = True  # Unreadable storage is not proof of absence.
            if not published:
                row["controls"].pop(source_id, None)
                if control_turn is not None:
                    row["turns"].remove(control_turn)
                self.unconfirmed_controls.pop(source_id, None)
                self._discard_empty_row(row)
            raise

    def _release_context(self, row, turn):
        self.adapter._conversation_state().release(row["chat_id"], turn["id"])

    def _kick(self, row):
        key = row["key"]
        if self.closed or key in self.tasks or row.get("blocked"):
            return
        channel = row["turns"][0]["route"]["mode"] if row["turns"] else None
        if channel == "slack" and not getattr(self.adapter, "_slack_enabled", False):
            return
        if channel == "imessage" and not getattr(self.adapter, "_imessage_threaded_replies", False):
            return
        task = asyncio.create_task(self._drain(row))
        self.tasks[key] = task
        def done(finished):
            self.tasks.pop(key, None)
            if not finished.cancelled() and finished.exception() is not None:
                row["blocked"] = True
                row["error"] = f"Native queue failed ({type(finished.exception()).__name__}); inspect the original turn"
                self._save(row)
            elif not self.closed and any(turn["state"] in {"pending", "answer_ready"} for turn in row["turns"]):
                self._kick(row)
        task.add_done_callback(done)

    async def _batch(self, row, turn):
        if turn["route"]["mode"] != "imessage" or turn["event"].get("media_urls") or turn["route"].get("reaction"):
            return
        signal = self.changed.setdefault(row["key"], asyncio.Event())
        while True:
            tail = row["turns"][row["turns"].index(turn) + 1:]
            for candidate in tail:
                route, next_route = turn["route"], candidate["route"]
                compatible = (candidate["state"] == "pending" and not candidate["event"].get("media_urls") and not next_route.get("reaction")
                              and all(route.get(key) == next_route.get(key) for key in (
                                  "author", "conversation_id", "reply_to_message_id", "thread_id", "thread_root_message_id",
                              )))
                if (not compatible or candidate["first_at"] - turn["first_at"] >= self.max_burst_seconds
                        or len(turn["source_ids"]) + len(candidate["source_ids"]) > self.max_burst_sources
                        or len(turn["event"]["text"]) + len(candidate["event"]["text"]) + 1 > self.max_burst_characters):
                    return
                turn["event"]["text"] += "\n" + candidate["event"]["text"]
                turn["source_ids"].extend(candidate["source_ids"])
                for key in ("imessage_event_ids", "imessage_sources"):
                    turn["route"].setdefault(key, []).extend(next_route.get(key, []))
                turn["event"]["metadata"]["inkbox_reply_route"] = copy.deepcopy(turn["route"])
                turn["last_at"] = candidate["last_at"]
                row["turns"].remove(candidate)
                self._save(row)
            timeout = min(turn["last_at"] + self.quiet_seconds, turn["first_at"] + self.max_burst_seconds) - time.time()
            if timeout <= 0:
                return
            signal.clear()
            try:
                await asyncio.wait_for(signal.wait(), timeout)
            except TimeoutError:
                return

    async def _notify(self, row, turn, state):
        tracker = getattr(self.adapter, "_slack_activity", None)
        if tracker:
            await tracker.notify(row["chat_id"], turn["route"]["mode"], turn["route"], state)

    async def _monitor(self, row, turn, event):
        waiting = False
        while self.active.get(row["chat_id"]) is turn:
            current = self.adapter._pending_conversation_control(event.source)
            if current != waiting:
                await self._notify(row, turn, "waiting" if current else "resumed")
                waiting = current
            await asyncio.sleep(.1)

    async def _drain(self, row):
        for turn in row["turns"]:
            if turn["state"] not in {"pending", "answer_ready"}:
                continue
            if turn["state"] == "answer_ready":
                await self._notify(row, turn, "accepted")
                await self._recover_answer(row, turn)
                await self._notify(row, turn, "completed" if turn["state"] == "done" else "failed")
                if turn["state"] != "done":
                    return
                continue
            await self._batch(row, turn)
            self._prepare_delivery_context(row, turn)
            event = self._event_from_turn(row, turn)
            await self._wait_for_idle(event)
            self._require_owner()
            turn["state"] = "running"
            self._save(row)
            self.active[row["chat_id"]] = turn
            self.events[turn["id"]] = event
            completion = asyncio.get_running_loop().create_future()
            self.completions[turn["id"]] = completion
            self.host_sessions.add(self._session_key(event))
            await self._notify(row, turn, "accepted")
            monitor = asyncio.create_task(self._monitor(row, turn, event))
            self.monitors[turn["id"]] = monitor
            token = reply_route.set(turn["route"])
            try:
                task = asyncio.create_task(self.adapter.handle_message(event))
                self.dispatches.add(task)
                task.add_done_callback(self.dispatches.discard)
                await asyncio.wait_for(asyncio.shield(task), self.completion_timeout)
                if getattr(event, "_gateway_accepted", True) is False:
                    turn["state"] = "cancelled"
                else:
                    await asyncio.wait_for(asyncio.shield(completion), self.completion_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row["error"] = f"Native host processing failed ({type(exc).__name__})"
                # Retry only a proven pre-admission failure. Once accepted,
                # uncertain execution is never automatically replayed.
                if not getattr(event, "_gateway_accepted", False) and turn["state"] == "running":
                    turn["state"] = "pending"
                    self._save(row)
                    await asyncio.sleep(self.retry_delay)
                    return
                turn["state"] = "uncertain"
                row["blocked"] = True
            finally:
                reply_route.reset(token)
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
                self.monitors.pop(turn["id"], None)
                self.events.pop(turn["id"], None)
                self.active.pop(row["chat_id"], None)
                self.completions.pop(turn["id"], None)
                if turn.get("session_id") and not clear_context(turn["session_id"], turn["id"]):
                    row["blocked"] = True
                if self._settled_proof(turn):
                    turn["fenced"] = True
                if turn["state"] not in {"done", "pending"}:
                    self._release_context(row, turn)
                if turn["state"] == "uncertain" and not turn.get("fenced"):
                    row["blocked"] = True
                    row.setdefault("error", "Original native outcome is uncertain; verify worker ownership before continuing")
                if not self.closed:
                    self._save(row)
                if turn["state"] != "pending":
                    await self._notify(row, turn, "completed" if turn["state"] == "done" else "cancelled" if turn["state"] == "cancelled" or self.closed else "failed")
            if row.get("blocked"):
                return

    async def _recover_fenced_native(self, row):
        from .host_fencing import fence_turn
        unresolved = [turn for turn in row["turns"] if not turn.get("fenced")
                      and turn["state"] in {"running", "uncertain", "sending", "cancelled"}]
        if not unresolved:
            return False
        for turn in unresolved:
            event = self._event_from_turn(row, turn)
            if not self._host_owner()._is_user_authorized(event.source) or not await fence_turn(self.adapter, event.source, turn["id"]):
                return False
            await asyncio.gather(*(asyncio.shield(task) for task in self.outbound), return_exceptions=True)
            if turn.get("session_id") and not clear_context(turn["session_id"], turn["id"]):
                return False
            turn["fenced"] = True
            turn["state"] = "quarantined"
        row.pop("blocked", None)
        row.pop("error", None)
        self._save(row)
        return True

    def _owned(self, event):
        raw = event.raw_message
        if not isinstance(raw, dict) or "_inkbox_native_key" not in raw:
            return None
        row = self.rows.get(raw["_inkbox_native_key"])
        turn = self.active.get(str(event.source.chat_id))
        if not row or not turn or turn["id"] != event.message_id or raw.get("_inkbox_native_turn") != turn["id"]:
            return None
        return row, turn

    def processing(self, event, outcome=None):
        owned = self._owned(event)
        if not owned:
            return False
        row, turn = owned
        if outcome is None:
            store = self.adapter._host_session_store()
            if store is not None:
                session = store.get_or_create_session(event.source)
                turn["session_id"] = str(session.session_id)
                if turn["route"]["mode"] == "imessage":
                    def record(content, message):
                        turn.setdefault("explicit_sends", []).append({"content": content, "message_id": str(message.id),
                            "ancestry": self._outbound_ancestry(message)})
                        self._save(row)
                        self._record_outbound(row, turn, message)
                    def tool_state(state):
                        turn["explicit_delivery_state"] = state
                        self._save(row)
                    bind_context(turn["session_id"], {**turn["route"], "turn_id": turn["id"], "record_explicit": record, "explicit_delivery_state": tool_state})
        else:
            success = str(getattr(outcome, "value", outcome)).lower() == "success"
            if (turn.get("preflight_failure") and turn.get("answer") is not None
                    and turn["state"] not in {"cancelled", "quarantined", "uncertain", "sending"}):
                # The model finished, but a read-only preflight did not admit a
                # send. Preserve that answer; never turn it into unknown delivery.
                turn["state"] = "answer_ready"
            elif turn["state"] not in {"done", "cancelled", "uncertain"}:
                turn["state"] = "done" if success else "uncertain"
            future = self.completions.get(turn["id"])
            if future and not future.done():
                future.set_result(success)
        self._save(row)
        return True

    @staticmethod
    def _settled_proof(turn):
        proof = turn.get("worker_completion") or {}
        return (proof.get("kind") == "native_worker_finalizer" and proof.get("source_id") == turn["id"]
                and isinstance(proof.get("generation"), int) and bool(proof.get("session_key")))

    def capture_result(self, event, response):
        owned = self._owned(event)
        if owned and isinstance(response, str):
            row, turn = owned
            from .host_fencing import completed_worker_proof
            proof = completed_worker_proof(self._host_owner(), event)
            if proof is not None:
                turn["worker_completion"] = proof
            if turn["state"] == "running":
                turn["answer"] = response
                turn["state"] = "answer_ready"
                self._save(row)
                self._complete_delivery_context(turn)

    def capture_rendered(self, event, extracted):
        owned = self._owned(event)
        if owned:
            row, turn = owned
            turn["rendered"] = {key: copy.deepcopy(getattr(extracted, key)) for key in (
                "text_content", "images", "media_files", "local_files", "force_document_attachments", "pre_extract")}
            self._save(row)

    async def _recover_answer(self, row, turn):
        from types import SimpleNamespace
        rendered = turn.get("rendered")
        if rendered is None:
            # Reconstruct using the native renderer, never rerun the model.
            extract = getattr(self.adapter, "_extract_response_content", None)
            if callable(extract) and hasattr(super(type(self.adapter), self.adapter), "_extract_response_content"):
                event = self._event_from_turn(row, turn)
                extracted = await extract(turn["answer"], event, self._session_key(event), is_ephemeral_response=False)
                rendered = {key: copy.deepcopy(getattr(extracted, key)) for key in (
                    "text_content", "images", "media_files", "local_files", "force_document_attachments", "pre_extract")}
                turn["rendered"] = rendered
                self._save(row)
        text = rendered["text_content"] if rendered else turn["answer"]
        token = reply_route.set(turn["route"])
        try:
            if text:
                result = await self._send(row, turn, text)
                if not result.success:
                    if turn.get("preflight_failure", {}).get("retryable"):
                        await asyncio.sleep(self.retry_delay)
                    return
            if rendered:
                results = []
                await self.adapter._deliver_attachments(self._event_from_turn(row, turn), SimpleNamespace(**rendered), {},
                    anything_sent=bool(text), record_delivery=results.append)
                if any(not result.success for result in results):
                    if turn.get("preflight_failure"):
                        turn["state"] = "answer_ready"
                        if turn["preflight_failure"].get("retryable"):
                            await asyncio.sleep(self.retry_delay)
                    else:
                        turn["state"] = "uncertain"
                    self._save(row)
                    return
            turn["state"] = "done"
            self._save(row)
        finally:
            reply_route.reset(token)

    def media_owner(self, chat_id, metadata=None):
        route = reply_route.get() or (metadata or {}).get("inkbox_reply_route") or {}
        if route.get("chat_id") != chat_id or route.get("mode") != "imessage":
            return None
        source_id = route.get("message_id")
        for row in self.rows.values():
            if row["chat_id"] == chat_id:
                for turn in row["turns"]:
                    if turn["id"] == source_id:
                        return row, turn
        return None

    def check_media_authority(self, row, turn, *, admitted_send=False):
        self._require_owner()
        if not getattr(self.adapter, "_imessage_threaded_replies", False):
            raise RuntimeError("Native iMessage replies are disabled")
        if (turn["state"] in {"uncertain", "cancelled", "quarantined"}
                or (turn["state"] == "sending" and not admitted_send)
                or turn.get("explicit_delivery_state") in {"sending", "uncertain"}):
            raise RuntimeError("Original native turn cannot send media")
        if not self._host_owner()._is_user_authorized(self._event_from_turn(row, turn).source):
            raise PermissionError("Original native sender is no longer allowed")

    def checked_imessage_send(self, row, turn, identity, payload):
        # The executor may start after a Stop/revocation queued on the loop.
        # Recheck inside that worker immediately before the actual SDK effect.
        try:
            self.check_media_authority(row, turn, admitted_send=True)
        except Exception:
            raise ReplyPreflightError("Original native iMessage ownership changed; no send attempted") from None
        return identity.send_imessage(**payload)

    async def preflight_reply(self, row, turn, identity):
        def guard():
            self.check_media_authority(row, turn)
        guard()
        options = await asyncio.to_thread(validate_reply_target, identity, turn["route"], guard)
        guard()
        turn.pop("preflight_failure", None)
        return options

    def preflight_failed(self, row, turn, exc):
        transient = isinstance(exc, ReplyPreflightError) and exc.retryable
        turn["preflight_failure"] = {"retryable": transient, "error": str(exc)}
        if not transient:
            row["blocked"] = True
            row["error"] = str(exc)
        self._save(row)
        return SendResult(success=False, error=str(exc), retryable=transient,
                          raw_response={"inkbox_no_retry": True, "inkbox_preflight_failure": True})

    async def send_media(self, row, turn, identity, payload, source):
        self.check_media_authority(row, turn)
        fingerprint = digest(json.dumps([source, payload.get("text")]))
        prior = turn.setdefault("media_deliveries", {}).get(fingerprint)
        if prior:
            if prior["state"] == "sent":
                return SendResult(success=True, message_id=prior["message_id"])
            return SendResult(success=False, error="Original media outcome is uncertain", raw_response={"inkbox_no_retry": True})
        try:
            options = await self.preflight_reply(row, turn, identity)
        except Exception as exc:
            return self.preflight_failed(row, turn, exc)
        payload = {**payload, "conversation_id": turn["route"]["conversation_id"], **options,
                   "idempotency_key": "hermes:media:" + digest(turn["id"] + fingerprint)}
        payload.pop("to", None)
        delivery = turn["media_deliveries"][fingerprint] = {"state": "sending"}
        self._save(row)
        task = asyncio.create_task(asyncio.to_thread(self.checked_imessage_send, row, turn, identity, payload))
        self.outbound.add(task)
        task.add_done_callback(self.outbound.discard)
        try:
            result = await asyncio.shield(task)
            delivery.update(state="sent", message_id=str(result.id), ancestry=self._outbound_ancestry(result))
            self._save(row)
            self._record_outbound(row, turn, result)
            return SendResult(success=True, message_id=str(result.id))
        except ReplyPreflightError as exc:
            turn["media_deliveries"].pop(fingerprint, None)
            return self.preflight_failed(row, turn, exc)
        except Exception as exc:
            delivery["state"] = "uncertain"
            turn["state"] = "uncertain"
            self._save(row)
            return SendResult(success=False, error=f"Native media outcome is uncertain ({type(exc).__name__})", raw_response={"inkbox_no_retry": True})

    async def send(self, chat_id, content, reply_to):
        row = next((row for row in self.rows.values() if row["chat_id"] == chat_id and any(
            turn["id"] == reply_to for turn in row["turns"])), None)
        if row is None:
            return None
        turn = next(turn for turn in row["turns"] if turn["id"] == reply_to)
        return await self._send(row, turn, content)

    async def _send(self, row, turn, content):
        self._require_owner()
        route = turn["route"]
        if turn["state"] in {"uncertain", "sending", "cancelled", "quarantined"} or turn.get("explicit_delivery_state") in {"sending", "uncertain"}:
            return SendResult(success=False, error="Original native turn is no longer sendable; no automatic retry", raw_response={"inkbox_no_retry": True})
        if any(item.get("state") in {"sending", "uncertain"} for item in turn.get("media_deliveries", {}).values()):
            return SendResult(success=False, error="Original media delivery is uncertain", raw_response={"inkbox_no_retry": True})
        if route["mode"] == "slack" and len(content) > 12000:
            return SendResult(success=False, error="Slack messages must be at most 12000 characters", raw_response={"inkbox_no_retry": True})
        if turn.get("sent"):
            if turn["sent"]["content"] == content:
                return SendResult(success=True, message_id=turn["sent"]["message_id"])
            return SendResult(success=False, error="Original native turn already sent its answer")
        explicit = next((item for item in turn.get("explicit_sends", []) if item["content"] == content), None)
        if explicit or content.strip().upper() in {"", "[SILENT]"}:
            turn["state"] = "done"
            self._save(row)
            return SendResult(success=True, message_id=explicit["message_id"] if explicit else "suppressed-silent-marker")
        event = self._event_from_turn(row, turn)
        owner = self._host_owner()
        if not owner or not owner._is_user_authorized(event.source):
            return SendResult(success=False, error="Original sender is no longer authorized")
        if route["mode"] == "slack" and not self.adapter._slack_enabled:
            return SendResult(success=False, error="Slack is disabled")
        if route["mode"] == "slack":
            from .slack import validate_connection
            try:
                await asyncio.to_thread(validate_connection, self.adapter._inkbox.slack, str(self.adapter._identity_id), route)
            except Exception as exc:
                return SendResult(success=False, error=f"Original Slack connection is unavailable ({type(exc).__name__})", raw_response={"inkbox_no_retry": True})
        else:
            try:
                reply_options = await self.preflight_reply(row, turn, self.adapter._reply_identity)
            except Exception as exc:
                return self.preflight_failed(row, turn, exc)
        # Store prompts as deliveries, but only the checkpointed final answer
        # settles model work; the host may ask and resume within the same turn.
        final = turn.get("rendered", {}).get("text_content", turn.get("answer")) == content
        previous_state = turn["state"]
        turn["state"] = "sending"
        self._save(row)
        try:
            if route["mode"] == "slack":
                method, args, kwargs = send_reply, (self.adapter._inkbox, route, content), {}
            else:
                payload = {
                    "conversation_id": route["conversation_id"], "text": content, **reply_options,
                    "idempotency_key": "hermes:" + digest(json.dumps([turn["id"], route["conversation_id"], route.get("imessage_reply_target"), content])) ,
                }
                method, args, kwargs = self.checked_imessage_send, (row, turn, self.adapter._reply_identity, payload), {}
            task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
            self.outbound.add(task)
            task.add_done_callback(self.outbound.discard)
            result = await asyncio.shield(task)
            message_id = str(result.id)
            accepted = {"content": content, "message_id": message_id, "final": final}
            if route["mode"] == "imessage":
                accepted["ancestry"] = self._outbound_ancestry(result)
            turn.setdefault("deliveries", []).append(accepted)
            if final:
                turn["sent"] = {key: value for key, value in accepted.items() if key != "final"}
            turn["state"] = "done" if final else previous_state
            self._save(row)
            if route["mode"] == "imessage":
                self._record_outbound(row, turn, result)
            return SendResult(success=True, message_id=message_id)
        except ReplyPreflightError as exc:
            if turn["state"] == "sending":
                turn["state"] = previous_state
            return self.preflight_failed(row, turn, exc)
        except Exception as exc:
            turn["state"] = "uncertain"
            self._save(row)
            return SendResult(success=False, error=f"Native reply outcome is uncertain ({type(exc).__name__}); no plain resend", raw_response={"inkbox_no_retry": True})

    async def control(self, event, row):
        active = self.active.get(row["chat_id"])
        if not active:
            return False
        incoming = event.metadata["inkbox_reply_route"]
        route = active["route"]
        if incoming.get("author") != route.get("author") or (route["mode"] == "slack" and any(
            incoming.get(key) != route.get(key) for key in ("connection_id", "conversation_id", "thread_ts")
        )):
            return False
        text = incoming.get("raw_text", event.text).strip()
        prompted = self.adapter._pending_conversation_control(event.source)
        answer = self.adapter._conversation_prompt_reply(event.source, text) if prompted else None
        if not conversation_control(text) and answer is None:
            if prompted and approval_reply(text) is None:
                # A new instruction is not an approval. Deny the pending native
                # permission and retain this receipt as ordinary queued work.
                from .host_fencing import cancel_pending_permissions, fence_turn
                await cancel_pending_permissions(self._host_owner(), self._session_key(event))
                if await fence_turn(self.adapter, event.source, active["id"]):
                    active["state"] = "cancelled"
                    active["fenced"] = True
                    future = self.completions.get(active["id"])
                    if future and not future.done():
                        future.set_result(False)
                    self._save(row)
                else:
                    row["blocked"] = True
                    row["error"] = "Pending permission denied; original native worker exit could not be confirmed"
            return False
        control_id = str(incoming.get("message_id") or event.message_id)
        if row.get("controls", {}).get(control_id) in {"consumed", "uncertain"}:
            return True
        self._checkpoint_control(event, row, control_id, native_owner=active["id"])
        stopping = text.casefold() in {"/stop", "/cancel", "/clear", "/new"}
        fenced = False
        # No await occurs between durable admission and this effect boundary.
        self.unconfirmed_controls.pop(control_id, None)
        if stopping:
            from .host_fencing import fence_turn
            fenced = await fence_turn(self.adapter, event.source, active["id"])
        await self._forward_control(event, answer or text, active)
        if stopping:
            active["state"] = "cancelled"
            active["fenced"] = fenced
            if not fenced:
                row["blocked"] = True
                row["error"] = "Stop requested; native worker exit could not be confirmed"
            future = self.completions.get(active["id"])
            if future and not future.done():
                future.set_result(False)
            await self._notify(row, active, "cancelled")
        row["controls"][control_id] = "consumed"
        self._save(row)
        if text.casefold() in {"/stop", "/cancel", "/clear", "/new"}:
            for turn in row["turns"]:
                if turn["state"] == "pending":
                    turn["state"] = "cancelled"
                    self._release_context(row, turn)
            self._save(row)
        return True

    async def _forward_control(self, event, text, active):
        event.text = text
        event.message_id = active["id"]
        event.raw_message = {"_inkbox_native_control": True}
        event.metadata["inkbox_control"] = True
        event.metadata["inkbox_reply_route"] = copy.deepcopy(active["route"])
        event.allow_gateway_control = True
        task = await self.adapter._enqueue(event)
        await task

    def record_delivery_failure(self, envelope, rows=None):
        """Retain outbound status without admitting a model turn or a resend."""
        self._require_owner()
        data = envelope.get("data") or {}
        item = data.get("message") or {}
        kind = envelope.get("event_type")
        identity = data.get("identity_id") or item.get("agent_identity_id") or item.get("identity_id")
        message_id = self._delivery_identifier(item.get("id"))
        conversation = self._delivery_identifier(item.get("conversation_id") or item.get("conversationId"))
        if (kind not in {"imessage.delivery_failed", "imessage.delivered"} or not message_id
                or str(item.get("direction") or "").lower() == "inbound"
                or identity is not None and str(identity) != str(self.adapter._identity_id)):
            return False
        with self._checkpoint_lock:
            prior = self.delivery_status.get(message_id, {})
            if conversation and prior.get("conversation_id") not in (None, conversation):
                return False
            if kind == "imessage.delivered":
                return True  # Baseline success bookkeeping still owns these callbacks.
            if prior.get("failed") and (not conversation or prior.get("conversation_id")):
                return True
            self.delivery_status[message_id] = {**prior, "failed": True,
                "conversation_id": prior.get("conversation_id") or conversation}
            try:
                self._save_delivery_status()
            except BaseException:
                # A retried webhook must not mistake an in-memory mutation
                # for a durable receipt after the first write failed.
                if prior:
                    self.delivery_status[message_id] = prior
                else:
                    self.delivery_status.pop(message_id, None)
                raise
        return True

    @staticmethod
    def _delivery_identifier(value):
        return value if isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(char) < 32 for char in value) else None

    def _save_delivery_status(self):
        self._save_checkpoint(self.root / "delivery-status", {"version": 1, "messages": self.delivery_status})

    def _record_outbound(self, row, turn, message):
        message_id = self._delivery_identifier(str(getattr(message, "id", "") or ""))
        if not message_id:
            return
        with self._checkpoint_lock:
            prior = self.delivery_status.get(message_id, {})
            route = turn["route"]
            if prior.get("conversation_id") not in (None, route["conversation_id"]):
                prior = {}  # A callback for another conversation is not this send's outcome.
            self.delivery_status[message_id] = {**prior, "conversation_id": route["conversation_id"],
                "chat_id": row["chat_id"], "source_id": turn["id"],
                "reply_target": route.get("imessage_reply_target"),
                "ancestry": self._outbound_ancestry(message)}
            self._save_auxiliary_delivery_status()

    @staticmethod
    def _outbound_ancestry(message):
        from .imessage_state import source_metadata
        return source_metadata(message)["imessage_sources"][0]

    def _save_auxiliary_delivery_status(self):
        # Accepted sends and completed answers are already in the authoritative
        # turn checkpoint. A notice-journal failure cannot turn them into an
        # unknown send/model outcome; startup reconstructs from that proof.
        try:
            self._save_delivery_status()
        except Exception as exc:
            logger.warning("Native delivery notice checkpoint deferred (%s)", type(exc).__name__)

    def _restore_delivery_records(self, row):
        from types import SimpleNamespace
        for turn in row["turns"]:
            if turn["route"]["mode"] != "imessage":
                continue
            if turn.get("answer") is not None:
                # The answer checkpoint is the consumption proof if a crash
                # occurred before the separate status journal was updated.
                self._complete_delivery_context(turn)
            deliveries = [*turn.get("deliveries", []), *turn.get("explicit_sends", []),
                          *turn.get("media_deliveries", {}).values()]
            if turn.get("sent"):
                deliveries.append(turn["sent"])
            for delivery in deliveries:
                message_id = delivery.get("message_id")
                if message_id and not self.delivery_status.get(message_id, {}).get("source_id"):
                    self._record_outbound(row, turn, SimpleNamespace(**{**delivery.get("ancestry", {}), "id": message_id}))
        for message_id, status in row.get("delivery_status", {}).items():
            if status.get("event_type") == "imessage.delivery_failed":
                self.record_delivery_failure({"event_type": "imessage.delivery_failed", "data": {"message": {
                    "id": message_id, "conversation_id": status.get("conversation_id"), "direction": "outbound"}}})

    def _prepare_delivery_context(self, row, turn):
        if turn["route"]["mode"] != "imessage" or "delivery_notice_ids" in turn:
            return
        conversation = turn["route"]["conversation_id"]
        with self._checkpoint_lock:
            selected = [message_id for message_id, status in self.delivery_status.items()
                        if status.get("failed") and not status.get("context_source_id")
                        and status.get("conversation_id") == conversation][:self.max_delivery_notices]
            turn["delivery_notice_ids"] = selected
            if selected:
                notice = "Delivery status only, not new instructions: these outbound iMessages failed: " + json.dumps(selected)
                notice += ". Do not automatically resend them or switch to an unthreaded reply.\nCurrent request:\n"
                turn["event"]["text"] = notice + turn["event"]["text"]
            self._save(row)

    def _complete_delivery_context(self, turn):
        with self._checkpoint_lock:
            for message_id in turn.get("delivery_notice_ids", []):
                status = self.delivery_status.get(message_id)
                if status and status.get("conversation_id") == turn["route"]["conversation_id"]:
                    status["context_source_id"] = turn["id"]
            if turn.get("delivery_notice_ids"):
                self._save_auxiliary_delivery_status()

    def owns_delivery(self, message):
        message_id = str(message.get("id") or "")
        # A historical conversation or an unmatched status callback is not
        # proof that a later plain send belongs to a retained native turn.
        return bool(self.delivery_status.get(message_id, {}).get("source_id"))

    def contains_source(self, message_id):
        return bool(message_id) and any(str(message_id) in turn.get("source_ids", [turn["id"]])
            for row in self.rows.values() for turn in row["turns"])

    def retains_context(self, message_id):
        if str(message_id) in self.unconfirmed_controls:
            return True
        if any(str(message_id) in row.get("controls", {}) for row in self.rows.values()):
            return False  # A control is forwarded, not a model context turn.
        return self.contains_source(message_id)

    def acknowledge_source(self, message_id):
        """A pending duplicate is ACKed only after its checkpoint is durable."""
        if not message_id or str(message_id) in self.unconfirmed_controls:
            return False
        for row in self.rows.values():
            for turn in row["turns"]:
                if str(message_id) in turn.get("source_ids", [turn["id"]]):
                    if turn["state"] == "pending":
                        self._save(row)
                        self._kick(row)
                    return True
        return False

    def acknowledge_control(self, message_id):
        """ACK retained controls durably, without replaying ambiguous effects."""
        if not message_id or str(message_id) in self.unconfirmed_controls:
            return False
        for row in self.rows.values():
            if str(message_id) in row.get("controls", {}):
                self._save(row)
                return True
        return False
