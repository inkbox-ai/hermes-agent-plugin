"""Doctor checks for the Inkbox Hermes plugin."""

from __future__ import annotations

import json
from typing import Any, Dict, List

try:
    from .config import (
        VoiceStack,
        inkbox_client_kwargs,
        object_summary,
        read_runtime_config,
    )
    from .diagnostics import inkbox_api_error_message, missing_config_message
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import (
        VoiceStack,
        inkbox_client_kwargs,
        object_summary,
        read_runtime_config,
    )
    from diagnostics import inkbox_api_error_message, missing_config_message


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
