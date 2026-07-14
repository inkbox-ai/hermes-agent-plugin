from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def _real_host_available() -> bool:
    """True when the real Hermes host package is importable.

    CI installs ``hermes-agent`` so the suite runs against the real ``gateway.*``
    interface; locally (and in the offline unit job) the host is absent and we
    fall back to the stub below. This is what lets the contract tests catch real
    host drift instead of validating against a fiction.
    """
    try:
        import gateway.config  # noqa: F401
        import gateway.platforms.base  # noqa: F401

        return True
    except Exception:
        return False


if not _real_host_available() and "gateway.config" not in sys.modules:
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    sys.modules.setdefault("gateway", gateway)

    config_mod = types.ModuleType("gateway.config")

    class Platform(str):
        pass

    @dataclass
    class PlatformConfig:
        enabled: bool = True
        api_key: str = ""
        extra: dict[str, Any] = field(default_factory=dict)

    config_mod.Platform = Platform
    config_mod.PlatformConfig = PlatformConfig
    sys.modules["gateway.config"] = config_mod

    platforms_mod = types.ModuleType("gateway.platforms")
    platforms_mod.__path__ = []
    sys.modules["gateway.platforms"] = platforms_mod

    base_mod = types.ModuleType("gateway.platforms.base")

    class MessageType(Enum):
        TEXT = "text"
        COMMAND = "command"

    @dataclass
    class MessageEvent:
        chat_id: str = ""
        text: str = ""
        message_type: MessageType = MessageType.TEXT
        user_id: str | None = None
        thread_id: str | None = None
        message_id: str | None = None
        attachments: list[Any] = field(default_factory=list)
        media_urls: list[str] = field(default_factory=list)
        media_types: list[str] = field(default_factory=list)
        metadata: dict[str, Any] = field(default_factory=dict)
        auto_skill: "str | list[str] | None" = None
        channel_prompt: str | None = None
        source: Any = None
        raw_message: dict[str, Any] | None = None
        reply_to_message_id: str | None = None
        internal: bool = False
        chat_name: str | None = None
        user_name: str | None = None
        platform: Any = None
        message_text: str = ""
        timestamp: float | None = None
        raw_event: dict[str, Any] | None = None

        def __post_init__(self):
            if not self.text and self.message_text:
                self.text = self.message_text
            if self.raw_message is None and self.raw_event is not None:
                self.raw_message = self.raw_event

    @dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None
        raw_response: Any = None
        retryable: bool = False

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform

        def build_source(self, **kwargs):
            return types.SimpleNamespace(**kwargs)

    def validate_media_delivery_path(path):
        from pathlib import Path

        candidate = Path(path).expanduser().resolve()
        return str(candidate) if candidate.is_file() else None

    base_mod.BasePlatformAdapter = BasePlatformAdapter
    BasePlatformAdapter.validate_media_delivery_path = staticmethod(validate_media_delivery_path)
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    base_mod.SendResult = SendResult
    base_mod.validate_media_delivery_path = validate_media_delivery_path
    sys.modules["gateway.platforms.base"] = base_mod

    helpers_mod = types.ModuleType("gateway.platforms.helpers")
    helpers_mod.redact_phone = lambda phone: phone
    sys.modules["gateway.platforms.helpers"] = helpers_mod
