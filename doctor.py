"""Doctor checks for the Inkbox Hermes plugin."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit

try:
    from .config import (
        VoiceStack,
        inkbox_client_kwargs,
        inkbox_state_path,
        object_summary,
        read_runtime_config,
    )
    from .diagnostics import inkbox_api_error_message, missing_config_message
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import (
        VoiceStack,
        inkbox_client_kwargs,
        inkbox_state_path,
        object_summary,
        read_runtime_config,
    )
    from diagnostics import inkbox_api_error_message, missing_config_message


_DESIRED_A2A_EVENTS = frozenset({
    "a2a.task.created",
    "a2a.task.message",
    "a2a.task.canceled",
})


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _safe_receiver_url(value: Any) -> str | None:
    """Return a useful receiver location without query credentials."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    try:
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _local_a2a_tasks(limit: int = 10) -> List[Dict[str, Any]]:
    """Read bounded lifecycle metadata without exposing stored task content."""
    registry_path = inkbox_state_path().with_name("inkbox_a2a_tasks.json")
    try:
        loaded = json.loads(registry_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(loaded, dict):
        return []

    rows: List[Dict[str, Any]] = []

    def updated_at(entry: Dict[str, Any]) -> float:
        try:
            return float(entry.get("updated_at") or 0)
        except (TypeError, ValueError):
            return 0

    entries = sorted(
        (entry for entry in loaded.values() if isinstance(entry, dict)),
        key=updated_at,
        reverse=True,
    )
    for entry in entries[:limit]:
        phases = entry.get("phases")
        last_error = entry.get("last_error")
        row: Dict[str, Any] = {
            "taskId": str(entry.get("task_id") or ""),
            "contextId": str(entry.get("context_id") or ""),
            "messageId": str(entry.get("message_id") or ""),
            "state": str(entry.get("state") or ""),
            "attempt": _positive_int(entry.get("attempt")),
            "updatedAt": _json_value(entry.get("updated_at")),
        }
        if entry.get("outcome"):
            row["outcome"] = str(entry["outcome"])
        if isinstance(phases, dict):
            row["phases"] = {
                str(name): _json_value(timestamp)
                for name, timestamp in phases.items()
            }
        if isinstance(last_error, dict):
            row["lastError"] = {
                "phase": str(last_error.get("phase") or ""),
                "code": str(last_error.get("code") or ""),
                "at": _json_value(last_error.get("at")),
            }
        rows.append(row)
    return rows


def _delivery_result(row: Any) -> str:
    status = _field(row, "response_status")
    if isinstance(status, int) and 200 <= status < 300:
        return "delivered"
    if status in {401, 403}:
        return "authentication"
    if isinstance(status, int):
        return "http_error"
    return "transport_error"


def _a2a_diagnostics(client: Any, identity_id: Any) -> Dict[str, Any]:
    subscriptions = client.webhooks.subscriptions.list(
        agent_identity_id=identity_id,
    )
    a2a_subscriptions = [
        row
        for row in subscriptions
        if any(
            str(event_type).startswith("a2a.")
            for event_type in (_field(row, "event_types", []) or [])
        )
    ]
    ready = any(
        set(
            str(event_type)
            for event_type in (_field(row, "event_types", []) or [])
        ) == _DESIRED_A2A_EVENTS
        and str(_field(row, "status", "")) == "active"
        for row in a2a_subscriptions
    )
    a2a_subscriptions.sort(
        key=lambda row: str(_field(row, "updated_at", "") or ""),
        reverse=True,
    )
    subscription_rows: List[Dict[str, Any]] = []
    delivery_rows: List[Dict[str, Any]] = []
    for subscription in a2a_subscriptions[:10]:
        subscription_id = _field(subscription, "id")
        event_types = sorted(
            str(event_type)
            for event_type in (_field(subscription, "event_types", []) or [])
        )
        subscription_rows.append({
            "id": str(subscription_id),
            "eventTypes": event_types,
            "status": str(_field(subscription, "status", "")),
            "receiverUrl": _safe_receiver_url(_field(subscription, "url")),
            "createdAt": _json_value(_field(subscription, "created_at")),
            "updatedAt": _json_value(_field(subscription, "updated_at")),
        })
        for delivery in client.webhooks.deliveries.list(
            subscription_id=subscription_id,
            limit=10,
        ):
            delivery_rows.append({
                "id": str(_field(delivery, "id", "")),
                "subscriptionId": str(subscription_id),
                "eventId": str(_field(delivery, "event_id", "")),
                "eventType": str(_field(delivery, "event_type", "")),
                "responseStatus": _field(delivery, "response_status"),
                "result": _delivery_result(delivery),
                "isReplay": bool(_field(delivery, "is_replay", False)),
                "createdAt": _json_value(_field(delivery, "created_at")),
            })

    delivery_rows.sort(
        key=lambda row: str(row.get("createdAt") or ""),
        reverse=True,
    )
    return {
        "ready": ready,
        "subscriptions": subscription_rows,
        "recentDeliveries": delivery_rows[:10],
        "localTasks": _local_a2a_tasks(),
    }


def run_doctor() -> Dict[str, Any]:
    cfg = read_runtime_config()
    findings: List[Dict[str, str]] = []

    if not cfg.api_key:
        findings.append({
            "id": "inkbox/config-missing-api-key",
            "severity": "error",
            "message": missing_config_message("INKBOX_API_KEY"),
        })
    if not cfg.identity:
        findings.append({
            "id": "inkbox/config-missing-identity",
            "severity": "error",
            "message": missing_config_message("INKBOX_IDENTITY"),
        })
    if not cfg.signing_key:
        findings.append({
            "id": "inkbox/config-missing-signing-key",
            "severity": "warning",
            "message": "INKBOX_SIGNING_KEY is not set. Inbound webhook signature verification will fail unless disabled.",
        })
    if cfg.voice_stack_invalid_value:
        findings.append({
            "id": "inkbox/config-invalid-voice-stack",
            "severity": "error",
            "message": (
                f"INKBOX_VOICE_STACK={cfg.voice_stack_invalid_value!r} is invalid. "
                "Run `hermes inkbox setup` and choose a phone call voice stack."
            ),
        })
    if (
        cfg.voice_stack is VoiceStack.INKBOX_VOICE_AI
        and cfg.voice_ai_authority_mode not in {"contact_scoped", "yolo"}
    ):
        findings.append({
            "id": "inkbox/config-invalid-voice-ai-authority",
            "severity": "error",
            "message": (
                "INKBOX_VOICE_AI_AUTHORITY_MODE must be `contact_scoped` or `yolo`."
            ),
        })
    if cfg.voicemail_detection not in {"enabled", "disabled"}:
        findings.append({
            "id": "inkbox/config-invalid-voicemail-detection",
            "severity": "error",
            "message": (
                "INKBOX_VOICEMAIL_DETECTION must be `enabled` or `disabled`."
            ),
        })
    if (
        cfg.voice_stack is VoiceStack.OPENAI_REALTIME
        and not cfg.realtime_api_key
    ):
        findings.append({
            "id": "inkbox/realtime-key-missing",
            "severity": "error",
            "message": (
                "OpenAI Realtime is selected but no Realtime API key is configured. "
                "Run `hermes inkbox setup` to validate and save one."
            ),
        })

    sdk_available = True
    try:
        from inkbox import Inkbox
    except Exception as exc:
        sdk_available = False
        findings.append({
            "id": "inkbox/sdk-missing",
            "severity": "error",
            "message": f"Python inkbox SDK is not importable: {exc}. Install with `pip install inkbox aiohttp`.",
        })

    summary: Dict[str, Any] = {
        "configured": bool(cfg.api_key and cfg.identity),
        "sdkAvailable": sdk_available,
        "baseUrl": cfg.base_url,
        "identity": cfg.identity or None,
        "publicUrl": cfg.public_url or None,
        "voiceStack": cfg.voice_stack.value,
        "voiceAiAuthorityMode": cfg.voice_ai_authority_mode,
        "voicemailDetection": cfg.voicemail_detection,
        "realtimeConfigured": bool(cfg.realtime_api_key),
        "a2a": {
            "ready": False,
            "subscriptions": [],
            "recentDeliveries": [],
            "localTasks": _local_a2a_tasks(),
        },
        "findings": findings,
    }

    if sdk_available and cfg.api_key:
        try:
            client = Inkbox(**inkbox_client_kwargs(cfg.api_key, cfg.base_url))
            summary["whoami"] = object_summary(client.whoami())
            if cfg.identity:
                identity = client.get_identity(cfg.identity)
                summary["identityRecord"] = object_summary(identity)
                incoming = identity.get_incoming_call_action()
                incoming_action = str(
                    getattr(
                        getattr(incoming, "incoming_call_action", ""),
                        "value",
                        getattr(incoming, "incoming_call_action", ""),
                    )
                )
                summary["incomingCallAction"] = incoming_action
                expected_action = (
                    "hosted_agent"
                    if cfg.voice_stack is VoiceStack.INKBOX_VOICE_AI
                    else "auto_accept"
                )
                if incoming_action and incoming_action != expected_action:
                    findings.append({
                        "id": "inkbox/incoming-call-action-drift",
                        "severity": "warning",
                        "message": (
                            f"Saved incoming-call action is {incoming_action!r}; "
                            f"{cfg.voice_stack.value} expects {expected_action!r}. "
                            "Restart the gateway to reconcile it."
                        ),
                    })
                if cfg.voice_stack is VoiceStack.INKBOX_VOICE_AI:
                    hosted = identity.get_hosted_agent_config()
                    summary["hostedAgentConfig"] = object_summary(hosted)
                    remote_authority = str(
                        getattr(
                            getattr(hosted, "authority_mode", ""),
                            "value",
                            getattr(hosted, "authority_mode", ""),
                        )
                    )
                    if remote_authority != cfg.voice_ai_authority_mode:
                        findings.append({
                            "id": "inkbox/voice-ai-authority-drift",
                            "severity": "warning",
                            "message": (
                                f"Saved Voice AI authority is {remote_authority!r}; "
                                f"local setup expects {cfg.voice_ai_authority_mode!r}. "
                                "Rerun `hermes inkbox setup` to reconcile it."
                            ),
                        })
                try:
                    a2a = _a2a_diagnostics(client, identity.id)
                    summary["a2a"] = a2a
                    if not a2a["ready"]:
                        findings.append({
                            "id": "inkbox/a2a-subscription-not-ready",
                            "severity": "warning",
                            "message": (
                                "The active A2A webhook subscription does not "
                                "match the required event set. Restart the gateway "
                                "to reconcile it."
                            ),
                        })
                    recent_deliveries = a2a["recentDeliveries"]
                    if (
                        recent_deliveries
                        and recent_deliveries[0]["result"] != "delivered"
                    ):
                        result = recent_deliveries[0]["result"]
                        findings.append({
                            "id": "inkbox/a2a-latest-delivery-failed",
                            "severity": "warning",
                            "message": (
                                "The latest A2A webhook delivery was not accepted "
                                f"({result}). Check the receiver and signing-key "
                                "configuration before replaying it."
                            ),
                        })
                except Exception:
                    summary["a2a"] = {
                        "ready": False,
                        "subscriptions": [],
                        "recentDeliveries": [],
                        "localTasks": _local_a2a_tasks(),
                    }
                    findings.append({
                        "id": "inkbox/a2a-diagnostics-unavailable",
                        "severity": "warning",
                        "message": (
                            "A2A webhook diagnostics are unavailable. Confirm the "
                            "installed Inkbox SDK supports webhook delivery logs."
                        ),
                    })
        except Exception as exc:
            findings.append({
                "id": "inkbox/api-check-failed",
                "severity": "error",
                "message": inkbox_api_error_message(exc, "checking the Inkbox API"),
            })

    summary["ok"] = not any(f["severity"] == "error" for f in findings)
    return summary


def print_doctor() -> None:
    print(json.dumps(run_doctor(), indent=2, sort_keys=True))
