"""Durable ordinary Slack and native iMessage turns owned by the Hermes adapter."""
from __future__ import annotations

import asyncio
import copy
import json
import time

from gateway.platforms.base import MessageEvent, MessageType, SendResult

try:
    from .companion import CompanionReceiver, digest
    from .conversation import approval_reply, conversation_control, reply_route
    from .imessage_state import auto_reply_kwargs, bind_context, clear_context
    from .slack import send_reply
except ImportError:  # pragma: no cover - direct local import
    from companion import CompanionReceiver, digest
    from conversation import approval_reply, conversation_control, reply_route
    from imessage_state import auto_reply_kwargs, bind_context, clear_context
    from slack import send_reply


class NativeTurns(CompanionReceiver):
    """Serial native host admission, checkpointed answers, and immutable routes.

    This queue never creates a host session for a native iMessage subthread.
    One ordinary conversation owns one queue, irrespective of reply ancestry.
    """

    quiet_seconds = .75
    max_burst_seconds = 2.0

    def __init__(self, adapter, root):
        super().__init__(adapter, root)
        self.changed: dict[str, asyncio.Event] = {}
        self.events: dict[str, MessageEvent] = {}
        self.monitors: dict[str, asyncio.Task] = {}

    async def start(self):
        self._acquire()
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
                if turn["state"] in {"running", "sending", "control_submitting"}:
                    turn["state"] = "uncertain"
                turn.pop("session_id", None)
                if turn["state"] == "uncertain" and not turn.get("fenced"):
                    row["blocked"] = True
                    row["error"] = "Original worker or delivery ownership is uncertain; no automatic replay"
            self._save(row)
            self._kick(row)

    def _event_from_turn(self, row, turn):
        value = turn["event"]
        source = self.adapter.build_source(**value["source"])
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
                    "chat_id", "chat_name", "chat_type", "thread_id", "user_id", "user_id_alt", "user_name",
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
        if source_id in row.get("controls", {}) or any(source_id in turn.get("source_ids", [turn["id"]]) for turn in row["turns"]):
            return
        text = route.get("raw_text", event.text).strip()
        command = conversation_control(text) or text.split(maxsplit=1)[:1] in [["/approve"], ["/deny"]]
        if command:
            row.setdefault("controls", {})[source_id] = "submitting"
            self._save(row)
        if await self.control(event, row):
            row.setdefault("controls", {})[source_id] = "consumed"
            self._save(row)
            return
        if command:
            if self.active.get(row["chat_id"]):
                row["controls"][source_id] = "not_owner"
            else:
                if text.casefold() in {"/stop", "/cancel", "/clear", "/new"}:
                    for pending in row["turns"]:
                        if pending["state"] == "pending":
                            pending["state"] = "cancelled"
                control_turn = {"id": source_id, "state": "control_submitting", "event": self._serialize(event),
                                "source_ids": [source_id], "route": copy.deepcopy(route)}
                row["turns"].append(control_turn)
                self._save(row)
                await self._forward_control(event, text, control_turn)
                control_turn["state"] = "done"
                row["controls"][source_id] = "consumed"
            self._save(row)
            return
        turn = {"id": source_id, "state": "pending", "event": self._serialize(event), "source_ids": [source_id],
                "first_at": time.time(), "last_at": time.time(), "route": copy.deepcopy(route)}
        row["turns"].append(turn)
        self._save(row)
        self.changed.setdefault(key, asyncio.Event()).set()
        if row.get("blocked") and row["chat_id"] not in self.active:
            await self._recover_fenced_native(row)
        self._kick(row)

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
                if not compatible or candidate["first_at"] - turn["first_at"] >= self.max_burst_seconds:
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
                continue
            await self._batch(row, turn)
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
        unresolved = [turn for turn in row["turns"] if turn["state"] in {"running", "uncertain", "sending"}
                      or turn["state"] == "cancelled" and not turn.get("fenced")]
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
                        turn.setdefault("explicit_sends", []).append({"content": content, "message_id": str(message.id)})
                        self._save(row)
                    def tool_state(state):
                        turn["explicit_delivery_state"] = state
                        self._save(row)
                    bind_context(turn["session_id"], {**turn["route"], "turn_id": turn["id"], "record_explicit": record, "explicit_delivery_state": tool_state})
        else:
            success = str(getattr(outcome, "value", outcome)).lower() == "success"
            if turn["state"] not in {"done", "cancelled", "uncertain"}:
                turn["state"] = "done" if success else "uncertain"
            future = self.completions.get(turn["id"])
            if future and not future.done():
                future.set_result(success)
        self._save(row)
        return True

    def capture_result(self, event, response):
        owned = self._owned(event)
        if owned and isinstance(response, str):
            row, turn = owned
            if turn["state"] == "running":
                turn["answer"] = response
                turn["state"] = "answer_ready"
                self._save(row)

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
                    return
            if rendered:
                results = []
                await self.adapter._deliver_attachments(self._event_from_turn(row, turn), SimpleNamespace(**rendered), {},
                    anything_sent=bool(text), record_delivery=results.append)
                if any(not result.success for result in results):
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

    def check_media_authority(self, row, turn):
        self._require_owner()
        if turn["state"] in {"uncertain", "sending", "cancelled", "quarantined"} or turn.get("explicit_delivery_state") in {"sending", "uncertain"}:
            raise RuntimeError("Original native turn cannot send media")
        if not self._host_owner()._is_user_authorized(self._event_from_turn(row, turn).source):
            raise PermissionError("Original native sender is no longer allowed")

    async def send_media(self, row, turn, identity, payload, source):
        self.check_media_authority(row, turn)
        fingerprint = digest(json.dumps([source, payload.get("text")]))
        prior = turn.setdefault("media_deliveries", {}).get(fingerprint)
        if prior:
            if prior["state"] == "sent":
                return SendResult(success=True, message_id=prior["message_id"])
            return SendResult(success=False, error="Original media outcome is uncertain", raw_response={"inkbox_no_retry": True})
        payload = {**payload, "conversation_id": turn["route"]["conversation_id"], **auto_reply_kwargs(turn["route"]),
                   "idempotency_key": "hermes:media:" + digest(turn["id"] + fingerprint)}
        payload.pop("to", None)
        delivery = turn["media_deliveries"][fingerprint] = {"state": "sending"}
        self._save(row)
        task = asyncio.create_task(asyncio.to_thread(identity.send_imessage, **payload))
        self.outbound.add(task)
        task.add_done_callback(self.outbound.discard)
        try:
            result = await asyncio.shield(task)
            delivery.update(state="sent", message_id=str(result.id))
            self._save(row)
            return SendResult(success=True, message_id=str(result.id))
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
                method, args, kwargs = self.adapter._reply_identity.send_imessage, (), {
                    "conversation_id": route["conversation_id"], "text": content, **auto_reply_kwargs(route),
                    "idempotency_key": "hermes:" + digest(json.dumps([turn["id"], route["conversation_id"], route.get("imessage_reply_target"), content])) ,
                }
            task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
            self.outbound.add(task)
            task.add_done_callback(self.outbound.discard)
            result = await asyncio.shield(task)
            message_id = str(result.id)
            turn.setdefault("deliveries", []).append({"content": content, "message_id": message_id, "final": final})
            if final:
                turn["sent"] = {"content": content, "message_id": message_id}
            turn["state"] = "done" if final else previous_state
            self._save(row)
            return SendResult(success=True, message_id=message_id)
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
        row.setdefault("controls", {})[control_id] = "submitting"
        self._save(row)
        stopping = text.casefold() in {"/stop", "/cancel", "/clear", "/new"}
        fenced = False
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
        self._require_owner()
        item = (envelope.get("data") or {}).get("message") or {}
        message_id = str(item.get("id") or "")
        conversation = str(item.get("conversation_id") or "")
        if not message_id or not conversation:
            return
        for row in self.rows.values():
            if any(turn["route"]["mode"] == "imessage" and turn["route"].get("conversation_id") == conversation for turn in row["turns"]):
                row.setdefault("delivery_status", {})[message_id] = {"event_type": envelope.get("event_type"), "status": str(item.get("status") or ""), "conversation_id": conversation}
                self._save(row)

    def contains_source(self, message_id):
        return bool(message_id) and any(str(message_id) in turn.get("source_ids", [turn["id"]])
            for row in self.rows.values() for turn in row["turns"])
