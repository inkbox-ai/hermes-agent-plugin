from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


if "gateway.config" not in sys.modules:
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
        chat_id: str
        text: str
        message_type: MessageType = MessageType.TEXT
        user_id: str | None = None
        thread_id: str | None = None
        message_id: str | None = None
        attachments: list[Any] = field(default_factory=list)
        metadata: dict[str, Any] = field(default_factory=dict)
        auto_skill: str | None = None

    @dataclass
    class SendResult:
        success: bool
        message_id: str = ""
        error: str = ""
        retryable: bool = False

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform

    base_mod.BasePlatformAdapter = BasePlatformAdapter
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    base_mod.SendResult = SendResult
    sys.modules["gateway.platforms.base"] = base_mod

    helpers_mod = types.ModuleType("gateway.platforms.helpers")
    helpers_mod.redact_phone = lambda phone: phone
    sys.modules["gateway.platforms.helpers"] = helpers_mod
