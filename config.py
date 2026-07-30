"""Shared Inkbox plugin configuration helpers."""

from __future__ import annotations

import importlib.metadata
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, Dict


# Empty means "do not override"; the Inkbox SDK owns its API default.
INKBOX_BASE_URL_DEFAULT = ""
INKBOX_WS_PATH = "/phone/media/ws"

USER_AGENT_NAME = "inkbox-hermes"
DISTRIBUTION_NAME = "hermes-agent-plugin"
_RUNTIME_EXTRA: Dict[str, Any] = {}


class VoiceStack(str, Enum):
    """Supported phone-call voice stacks."""

    INKBOX_VOICE_AI = "inkbox_voice_ai"
    OPENAI_REALTIME = "openai_realtime"
    INKBOX_TTS_STT = "inkbox_tts_stt"


@dataclass
class InkboxPluginConfig:
    api_key: str = ""
    identity: str = ""
    signing_key: str = ""
    base_url: str = INKBOX_BASE_URL_DEFAULT
    public_url: str = ""
    tunnel_name: str = ""
    home_channel: str = ""
    realtime_api_key: str = ""
    realtime_model: str = "gpt-realtime-2"
    realtime_voice: str = "cedar"
    voice_stack: VoiceStack = VoiceStack.INKBOX_TTS_STT
    voice_stack_invalid_value: str = ""
    voice_ai_authority_mode: str = "contact_scoped"
    voicemail_detection: str = "enabled"
    contact_memories_enabled: bool = True


def inkbox_base_url_kwargs(base_url: str | None = None) -> Dict[str, str]:
    normalized = str(base_url or "").strip()
    return {"base_url": normalized} if normalized else {}


@lru_cache(maxsize=1)
def plugin_user_agent() -> str:
    """Identifies this plugin ahead of the SDK's own ``User-Agent`` token."""
    try:
        version = importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return f"{USER_AGENT_NAME}/{version}"


def inkbox_client_kwargs(api_key: str, base_url: str | None = None) -> Dict[str, str]:
    return {
        "api_key": api_key,
        "user_agent_prefix": plugin_user_agent(),
        **inkbox_base_url_kwargs(base_url),
    }


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_voice_stack(
    value: Any,
    *,
    realtime_enabled: Any = None,
    realtime_api_key: str = "",
) -> tuple[VoiceStack, str]:
    """Resolve the canonical stack while preserving legacy Realtime behavior."""
    normalized = str(value or "").strip().lower()
    if normalized:
        try:
            return VoiceStack(normalized), ""
        except ValueError:
            return VoiceStack.INKBOX_TTS_STT, normalized

    if realtime_enabled is not None:
        enabled = str(realtime_enabled).strip().lower() in {"auto", "1", "true", "yes", "on"}
        if not enabled:
            return VoiceStack.INKBOX_TTS_STT, ""
    if realtime_api_key:
        return VoiceStack.OPENAI_REALTIME, ""
    return VoiceStack.INKBOX_TTS_STT, ""


def read_config(extra: Dict[str, Any] | None = None) -> InkboxPluginConfig:
    extra = extra or {}
    realtime = extra.get("realtime") if isinstance(extra.get("realtime"), dict) else {}
    realtime_api_key = str(
        realtime.get("api_key")
        or os.getenv("INKBOX_REALTIME_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    realtime_enabled = (
        realtime.get("enabled")
        if "enabled" in realtime
        else os.getenv("INKBOX_REALTIME_ENABLED")
    )
    voice_stack, invalid_voice_stack = resolve_voice_stack(
        extra.get("voice_stack") or os.getenv("INKBOX_VOICE_STACK"),
        realtime_enabled=realtime_enabled,
        realtime_api_key=realtime_api_key,
    )
    return InkboxPluginConfig(
        api_key=str(extra.get("api_key") or os.getenv("INKBOX_API_KEY") or "").strip(),
        identity=str(extra.get("identity") or os.getenv("INKBOX_IDENTITY") or "").strip(),
        signing_key=str(extra.get("signing_key") or os.getenv("INKBOX_SIGNING_KEY") or "").strip(),
        base_url=str(extra.get("base_url") or os.getenv("INKBOX_BASE_URL") or INKBOX_BASE_URL_DEFAULT).strip(),
        public_url=str(extra.get("public_url") or os.getenv("INKBOX_PUBLIC_URL") or "").strip(),
        tunnel_name=str(extra.get("tunnel_name") or os.getenv("INKBOX_TUNNEL_NAME") or "").strip(),
        home_channel=str(os.getenv("INKBOX_HOME_CHANNEL") or extra.get("home_channel") or "").strip(),
        realtime_api_key=realtime_api_key,
        realtime_model=str(realtime.get("model") or os.getenv("INKBOX_REALTIME_MODEL") or "gpt-realtime-2").strip(),
        realtime_voice=str(realtime.get("voice") or os.getenv("INKBOX_REALTIME_VOICE") or "cedar").strip(),
        voice_stack=voice_stack,
        voice_stack_invalid_value=invalid_voice_stack,
        voice_ai_authority_mode=str(
            extra.get("voice_ai_authority_mode")
            or os.getenv("INKBOX_VOICE_AI_AUTHORITY_MODE")
            or "contact_scoped"
        ).strip(),
        voicemail_detection=str(
            extra.get("voicemail_detection")
            or os.getenv("INKBOX_VOICEMAIL_DETECTION")
            or "enabled"
        ).strip().lower(),
        contact_memories_enabled=(
            env_flag("INKBOX_CONTACT_MEMORIES_ENABLED", True)
            if "contact_memories_enabled" not in extra
            else str(extra["contact_memories_enabled"]).strip().lower() in {"1", "true", "yes", "on"}
        ),
    )


def set_runtime_config_extra(extra: Dict[str, Any] | None) -> None:
    """Publish the host-applied platform config for tool handlers."""
    global _RUNTIME_EXTRA
    _RUNTIME_EXTRA = dict(extra or {})


def read_runtime_config() -> InkboxPluginConfig:
    """Read environment plus the platform config applied by Hermes."""
    return read_config(_RUNTIME_EXTRA)


def public_call_ws_url(cfg: InkboxPluginConfig, identity: Any | None = None) -> str:
    """Derive the call WebSocket URL used for outbound Inkbox calls."""
    if cfg.public_url:
        base = cfg.public_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}{INKBOX_WS_PATH}"

    tunnel_host = ""
    if identity is not None:
        tunnel = getattr(identity, "tunnel", None)
        tunnel_host = str(getattr(tunnel, "public_host", "") or "").strip()
    if tunnel_host:
        return f"wss://{tunnel_host}{INKBOX_WS_PATH}"

    if cfg.tunnel_name:
        return f"wss://{cfg.tunnel_name}.inkboxwire.com{INKBOX_WS_PATH}"
    if cfg.identity:
        return f"wss://{cfg.identity}.inkboxwire.com{INKBOX_WS_PATH}"
    return ""


def object_summary(obj: Any) -> Any:
    """Convert simple SDK objects into JSON-safe summaries."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [object_summary(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): object_summary(v) for k, v in obj.items()}
    out: dict[str, Any] = {}
    for name in (
        "id",
        "agent_handle",
        "display_name",
        "email_address",
        "mailbox",
        "phone_number",
        "number",
        "type",
        "sms_status",
        "sms_error_code",
        "imessage_enabled",
        "incoming_call_action",
        "client_websocket_url",
        "public_host",
        "voice",
        "model",
        "effective_voice",
        "effective_model",
        "authority_mode",
    ):
        if hasattr(obj, name):
            value = getattr(obj, name)
            out[name] = object_summary(getattr(value, "value", value))
    return out or str(obj)
