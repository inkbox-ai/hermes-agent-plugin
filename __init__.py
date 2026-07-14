"""Inkbox Hermes plugin registration."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    from .adapter import InkboxAdapter, check_inkbox_requirements, send_inkbox_direct
    from .cli import setup_argparse, handle_cli, slash_handler
    from .config import read_config
    from .diagnostics import SETUP_HINT
    from .setup_wizard import interactive_setup
    from .tools import register_tools
    from .http_routes import register_http_route
    from .webhook_providers import WebhookProvider, register_provider as register_webhook_provider
except ImportError:  # pragma: no cover - direct local import/test fallback
    import importlib
    import sys
    import types

    _LOCAL_PACKAGE = "_hermes_agent_plugin_local"
    if _LOCAL_PACKAGE not in sys.modules:
        pkg = types.ModuleType(_LOCAL_PACKAGE)
        pkg.__path__ = [str(Path(__file__).parent)]
        sys.modules[_LOCAL_PACKAGE] = pkg

    _adapter = importlib.import_module(f"{_LOCAL_PACKAGE}.adapter")
    _cli = importlib.import_module(f"{_LOCAL_PACKAGE}.cli")
    _config = importlib.import_module(f"{_LOCAL_PACKAGE}.config")
    _diagnostics = importlib.import_module(f"{_LOCAL_PACKAGE}.diagnostics")
    _setup_wizard = importlib.import_module(f"{_LOCAL_PACKAGE}.setup_wizard")
    _tools = importlib.import_module(f"{_LOCAL_PACKAGE}.tools")
    _http_routes = importlib.import_module(f"{_LOCAL_PACKAGE}.http_routes")
    _webhook_providers = importlib.import_module(f"{_LOCAL_PACKAGE}.webhook_providers")

    InkboxAdapter = _adapter.InkboxAdapter
    check_inkbox_requirements = _adapter.check_inkbox_requirements
    send_inkbox_direct = _adapter.send_inkbox_direct
    setup_argparse = _cli.setup_argparse
    handle_cli = _cli.handle_cli
    slash_handler = _cli.slash_handler
    read_config = _config.read_config
    SETUP_HINT = _diagnostics.SETUP_HINT
    interactive_setup = _setup_wizard.interactive_setup
    register_tools = _tools.register_tools
    register_http_route = _http_routes.register_http_route
    WebhookProvider = _webhook_providers.WebhookProvider
    register_webhook_provider = _webhook_providers.register_provider

logger = logging.getLogger(__name__)
_unconfigured_warning_emitted = False


def _validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    cfg = read_config(extra)
    return bool(cfg.api_key and cfg.identity)


def _is_connected(config: Any) -> bool:
    return _validate_config(config)


def _env_enablement() -> dict | None:
    global _unconfigured_warning_emitted

    cfg = read_config()
    if not (cfg.api_key and cfg.identity):
        if not _unconfigured_warning_emitted:
            missing = [
                name
                for name, value in (
                    ("INKBOX_API_KEY", cfg.api_key),
                    ("INKBOX_IDENTITY", cfg.identity),
                )
                if not value
            ]
            logger.warning(
                "[Inkbox] Plugin is enabled but not configured: missing %s. %s",
                " and ".join(missing),
                SETUP_HINT,
            )
            _unconfigured_warning_emitted = True
        return None
    seed: dict[str, Any] = {
        "api_key": cfg.api_key,
        "identity": cfg.identity,
    }
    if cfg.base_url:
        seed["base_url"] = cfg.base_url
    if cfg.signing_key:
        seed["signing_key"] = cfg.signing_key
    if cfg.public_url:
        seed["public_url"] = cfg.public_url
    if cfg.tunnel_name:
        seed["tunnel_name"] = cfg.tunnel_name
    if cfg.home_channel:
        seed["home_channel"] = {
            "chat_id": cfg.home_channel,
            "name": os.getenv("INKBOX_HOME_CHANNEL_NAME", "Inkbox Home"),
        }
    if cfg.realtime_api_key or os.getenv("INKBOX_REALTIME_ENABLED"):
        seed["realtime"] = {
            "enabled": os.getenv("INKBOX_REALTIME_ENABLED", "auto"),
            "api_key": cfg.realtime_api_key,
            "model": cfg.realtime_model,
            "voice": cfg.realtime_voice,
        }
    return seed


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    del yaml_cfg
    if not isinstance(platform_cfg, dict):
        return None
    extra = dict(platform_cfg.get("extra") or {})
    mapping = {
        "api_key": "api_key",
        "apiKey": "api_key",
        "identity": "identity",
        "signing_key": "signing_key",
        "signingKey": "signing_key",
        "base_url": "base_url",
        "baseUrl": "base_url",
        "public_url": "public_url",
        "publicUrl": "public_url",
        "tunnel_name": "tunnel_name",
        "tunnelName": "tunnel_name",
        "require_signature": "require_signature",
        "requireSignature": "require_signature",
        "sms_text_batch_delay_seconds": "sms_text_batch_delay_seconds",
        "smsTextBatchDelaySeconds": "sms_text_batch_delay_seconds",
    }
    for source, target in mapping.items():
        if source in platform_cfg and target not in extra:
            extra[target] = platform_cfg[source]
    if "realtime" in platform_cfg and "realtime" not in extra:
        extra["realtime"] = platform_cfg["realtime"]
    # Per-channel overrides: an ephemeral system prompt and/or extra skills to
    # auto-load, keyed by Inkbox modality or contact id. Passed straight through
    # to config.extra so the adapter can resolve them per inbound event.
    for passthrough in ("channel_prompts", "channel_skill_bindings"):
        if passthrough in platform_cfg and passthrough not in extra:
            extra[passthrough] = platform_cfg[passthrough]
    return extra or None


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    del media_files, force_document
    extra = getattr(pconfig, "extra", {}) or {}
    mode = None
    subject = None
    if isinstance(thread_id, str) and thread_id.startswith("email:"):
        mode = "email"
    return await send_inkbox_direct(extra, chat_id, message, mode=mode, subject=subject, thread_id=thread_id)


def _register_skills(ctx) -> None:
    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.exists():
        return
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)


def register(ctx) -> None:
    """Plugin entry point called by Hermes."""
    ctx.register_platform(
        name="inkbox",
        label="Inkbox",
        adapter_factory=lambda cfg: InkboxAdapter(cfg),
        check_fn=check_inkbox_requirements,
        validate_config=_validate_config,
        is_connected=_is_connected,
        required_env=["INKBOX_API_KEY", "INKBOX_IDENTITY"],
        install_hint=(
            "Run `hermes inkbox setup`; it installs or upgrades the Inkbox SDK "
            "inside the Hermes Python environment when needed."
        ),
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="INKBOX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="INKBOX_ALLOWED_USERS",
        allow_all_env="INKBOX_ALLOW_ALL_USERS",
        max_message_length=4096,
        pii_safe=True,
        emoji="📨",
        platform_hint=(
            "You are chatting through Inkbox email, SMS/MMS, iMessage, or "
            "voice. Inbound messages may start with an [inkbox:...] routing "
            "marker; use it for channel/contact context and never echo it. "
            "During live voice calls, answer conversationally in text; the "
            "adapter speaks the response over the active call."
        ),
    )
    register_tools(ctx)
    ctx.register_cli_command(
        name="inkbox",
        help="Inkbox plugin commands",
        setup_fn=setup_argparse,
        handler_fn=handle_cli,
        description="Configure and inspect the Inkbox Hermes plugin.",
    )
    ctx.register_command(
        "inkbox",
        slash_handler,
        description="Show Inkbox plugin status.",
        args_hint="[doctor|whoami]",
    )
    _register_skills(ctx)
    logger.info("Inkbox Hermes plugin registered")
