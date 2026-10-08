"""Task-stream access through the authenticated Inkbox SDK resource."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote
from uuid import UUID


@dataclass(frozen=True)
class StreamOperation:
    id: str
    status: str
    message_ts: str | None
    error_code: str | None
    retry_after: int | None


class SlackTaskStreams:
    """Prefer typed stream methods while older supported SDKs finish upgrading.

    Keep credentials, base URL, timeouts, and connection ownership in the SDK;
    never access Slack tokens or accept arbitrary paths. Remove the transport
    fallback when typed methods are available at the minimum SDK version.
    """

    def __init__(self, resource):
        self.resource = resource
        methods = ("start_stream", "append_stream", "stop_stream", "get_operation_by_key")
        # Inspect the class so dynamic proxy/mock attributes cannot advertise
        # methods which do not actually exist on the resource.
        self.typed = all(callable(getattr(type(resource), name, None)) for name in methods)
        self.http = None if self.typed else vars(resource).get("_http")

    @staticmethod
    def _base(route):
        return f"/slack/connections/{UUID(str(route['connection_id']))}"

    def capable(self, route):
        # Ordinary inline DMs must not acquire an unexpected thread just to show
        # task cards. Recipient and thread coordinates come only from admission.
        if (not self.typed and self.http is None) or not all(route.get(k) for k in ("thread_ts", "actor_id", "workspace_id")):
            return False
        try:
            capabilities = self.resource.capabilities(route["connection_id"])
            feature = capabilities.capabilities.get("task_streaming")
            return feature is not None and feature.scopes_satisfied is True
        except Exception:
            return False  # This read performed no send; ordinary progress is safe.

    @staticmethod
    def _parse(raw, route, kind):
        if not isinstance(raw, dict):
            raw = vars(raw)
        if (not isinstance(raw, dict) or raw.get("operation") != kind
                or str(raw.get("connection_id")) != str(route["connection_id"])
                or raw.get("conversation_id") != route["conversation_id"]
                or raw.get("status") not in {"in_progress", "succeeded", "failed", "unknown"}):
            raise ValueError("Invalid task-stream operation")
        operation_id = str(UUID(str(raw.get("id"))))
        timestamp = raw.get("message_ts")
        if raw["status"] == "succeeded" and (not isinstance(timestamp, str) or not timestamp):
            raise ValueError("Unconfirmed task-stream message")
        return StreamOperation(operation_id, raw["status"], timestamp, raw.get("error_code"), raw.get("retry_after"))

    def write(self, route, *, kind, key, chunks, stream_id=None):
        if self.typed:
            args = (route["connection_id"], route["conversation_id"])
            kwargs = {"chunks": chunks, "idempotency_key": key}
            if kind == "stream_start":
                result = self.resource.start_stream(*args, **kwargs, thread_ts=route["thread_ts"],
                    recipient_user_id=route["actor_id"], recipient_team_id=route["workspace_id"],
                    task_display_mode="timeline")
            elif kind in {"stream_append", "stream_stop"}:
                method = self.resource.append_stream if kind == "stream_append" else self.resource.stop_stream
                result = method(*args, str(UUID(str(stream_id))), **kwargs)
            else:
                raise ValueError("Invalid task-stream operation kind")
            # A typed call may already have reached Slack. Never fall through to
            # the older transport after a failure or an unconfirmed response.
            return self._parse(result, route, kind)
        path = f"{self._base(route)}/conversations/{quote(route['conversation_id'], safe='')}/streams"
        body = {"chunks": chunks}
        if kind == "stream_start":
            body.update(thread_ts=route["thread_ts"], recipient_user_id=route["actor_id"],
                        recipient_team_id=route["workspace_id"], task_display_mode="timeline")
        elif kind in {"stream_append", "stream_stop"}:
            path += f"/{UUID(str(stream_id))}/{kind.removeprefix('stream_')}"
        else:
            raise ValueError("Invalid task-stream operation kind")
        raw = self.http.post(path, json=body, headers={"Idempotency-Key": key})
        return self._parse(raw, route, kind)

    def lookup(self, route, *, kind, key):
        if self.typed:
            raw = self.resource.get_operation_by_key(route["connection_id"], idempotency_key=key)
        else:
            raw = self.http.get(f"{self._base(route)}/operations/by-key", headers={"Idempotency-Key": key})
        return self._parse(raw, route, kind)
