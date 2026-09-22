"""Non-interactive Inkbox bootstrap for an existing Hermes identity."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .config import INKBOX_BASE_URL_DEFAULT, VoiceStack, inkbox_client_kwargs
    from .setup_wizard import _enum_value, _env, _load_inkbox_symbols, _save, _seed_identity_state
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import INKBOX_BASE_URL_DEFAULT, VoiceStack, inkbox_client_kwargs
    from setup_wizard import _enum_value, _env, _load_inkbox_symbols, _save, _seed_identity_state


def _normalize_handle(value: str) -> str:
    return value.strip().removeprefix("@").strip()


def _redact_error(exc: Exception, secrets: list[str]) -> str:
    message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def _identity_for_agent_key(client: Any, expected_handle: str) -> Any:
    identities = list(client.list_identities())
    handles = {_normalize_handle(str(getattr(item, "agent_handle", ""))) for item in identities}
    if expected_handle not in handles:
        raise ValueError("The API key is not scoped to the requested identity.")
    return client.get_identity(expected_handle)


def _saved_agent_key(
    *,
    expected_handle: str,
    base_url: str,
    Inkbox: Any,
    agent_subtype: str,
) -> tuple[str, Any] | None:
    saved_key = _env("INKBOX_API_KEY").strip()
    saved_handle = _normalize_handle(_env("INKBOX_IDENTITY"))
    if not saved_key or saved_handle != expected_handle:
        return None
    try:
        client = Inkbox(**inkbox_client_kwargs(saved_key, base_url))
        info = client.whoami()
        if _enum_value(getattr(info, "auth_subtype", "")) != agent_subtype:
            return None
        return saved_key, _identity_for_agent_key(client, expected_handle)
    except Exception:
        return None


def _resolve_credentials(
    *,
    api_key: str,
    expected_handle: str,
    base_url: str,
    symbols: dict[str, Any],
    actions: list[str],
) -> tuple[str, Any]:
    Inkbox = symbols["Inkbox"]
    client = Inkbox(**inkbox_client_kwargs(api_key, base_url))
    info = client.whoami()
    if _enum_value(getattr(info, "auth_type", "")) != "api_key":
        raise ValueError("Bootstrap requires an Inkbox API key.")

    subtype = _enum_value(getattr(info, "auth_subtype", ""))
    agent_subtype = _enum_value(symbols["AGENT_CLAIMED"])
    admin_subtype = _enum_value(symbols["ADMIN_SCOPED"])

    if subtype == agent_subtype:
        return api_key, _identity_for_agent_key(client, expected_handle)

    if subtype == _enum_value(symbols["AGENT_UNCLAIMED"]):
        raise ValueError("The API key is not attached to a claimed identity yet.")

    if subtype != admin_subtype:
        raise ValueError("Use an agent-scoped or admin-scoped Inkbox API key.")

    saved = _saved_agent_key(
        expected_handle=expected_handle,
        base_url=base_url,
        Inkbox=Inkbox,
        agent_subtype=agent_subtype,
    )
    if saved is not None:
        actions.append("reused_saved_agent_key")
        return saved

    identity = client.get_identity(expected_handle)
    created = client.api_keys.create(
        label=f"Hermes gateway - {expected_handle}",
        description="Agent-scoped key created by the Hermes Inkbox bootstrap.",
        scoped_identity_id=identity.id,
    )
    agent_key = str(getattr(created, "api_key", "") or "")
    if not agent_key:
        raise RuntimeError("Inkbox did not return the new agent-scoped API key.")
    actions.append("minted_agent_scoped_key")
    return agent_key, Inkbox(**inkbox_client_kwargs(agent_key, base_url)).get_identity(expected_handle)


def _default_voice_ai_instructions(identity: Any, client: Any) -> str:
    handle = _normalize_handle(str(getattr(identity, "agent_handle", "")))
    mailbox = getattr(identity, "mailbox", None)
    email = getattr(identity, "email_address", None) or getattr(mailbox, "email_address", None)
    phone = getattr(getattr(identity, "phone_number", None), "number", None)
    tunnel = getattr(getattr(identity, "tunnel", None), "public_host", None)
    imessage_number = getattr(getattr(identity, "imessage_number", None), "number", None)

    channels = []
    if email:
        channels.append(f"Email: {email}.")
    if phone:
        channels.append(f"VoIP phone: {phone}.")
    if tunnel:
        channels.append(f"Public address: https://{tunnel}.")
    if imessage_number:
        channels.append(f"Dedicated iMessage line: {imessage_number}.")
    elif bool(getattr(identity, "imessage_enabled", False)):
        try:
            triage = client.imessages.get_triage_number()
            command = str(getattr(triage, "connect_command", "") or f"connect @{handle}")
            number = str(getattr(triage, "number", "") or "")
            if number:
                channels.append(f"Shared iMessage: text '{command}' to {number}.")
        except Exception:
            channels.append("Shared iMessage is enabled; use the current Inkbox connection instructions.")

    channel_text = " ".join(channels) or "No direct communication channel is currently configured."
    return (
        f"You are the hosted voice interface for Inkbox agent @{handle}. "
        "Help the caller understand how to connect with this agent and accurately repeat only the "
        f"configured channels below. {channel_text}"
    )


def _configure_voice_ai(identity: Any, client: Any, instructions: str | None) -> None:
    hosted = identity.get_hosted_agent_config()
    existing_instructions = getattr(hosted, "instructions", None)
    desired_instructions = (
        instructions
        if instructions is not None
        else existing_instructions or _default_voice_ai_instructions(identity, client)
    )
    if len(desired_instructions) > 8000:
        raise ValueError("Voice AI instructions must be 8,000 characters or fewer.")

    if getattr(hosted, "instructions", None) != desired_instructions:
        identity.set_hosted_agent_config(
            voice=getattr(hosted, "voice", None),
            model=getattr(hosted, "model", None),
            instructions=desired_instructions,
        )

    incoming = identity.get_incoming_call_action()
    if (
        _enum_value(getattr(incoming, "incoming_call_action", "")) != "hosted_agent"
        or getattr(incoming, "client_websocket_url", None) is not None
        or getattr(incoming, "incoming_call_webhook_url", None) is not None
    ):
        identity.set_incoming_call_action(
            incoming_call_action="hosted_agent",
            client_websocket_url=None,
            incoming_call_webhook_url=None,
        )

    _save("INKBOX_VOICE_STACK", VoiceStack.INKBOX_VOICE_AI.value)
    _save("INKBOX_VOICE_AI_AUTHORITY_MODE", _enum_value(getattr(hosted, "authority_mode", "contact_scoped")))
    _save("INKBOX_REALTIME_ENABLED", "false")


def _configure_signing_key(
    identity: Any,
    *,
    rotate: bool,
    local_key_matches_identity: bool,
    actions: list[str],
) -> str | None:
    local_key = _env("INKBOX_SIGNING_KEY").strip()
    status = identity.get_signing_key_status()
    configured = bool(getattr(status, "configured", False))
    if local_key and local_key_matches_identity and configured and not rotate:
        _save("INKBOX_REQUIRE_SIGNATURE", "true")
        actions.append("reused_local_signing_key")
        return None

    if configured and not rotate:
        return (
            "A signing key already exists for this identity, but it is not available in this Hermes profile. "
            "Set INKBOX_SIGNING_KEY and rerun, or explicitly allow rotation with --rotate-signing-key."
        )

    created = identity.create_signing_key()
    signing_key = str(getattr(created, "signing_key", "") or "")
    if not signing_key:
        raise RuntimeError("Inkbox did not return the new signing key.")
    _save("INKBOX_SIGNING_KEY", signing_key)
    _save("INKBOX_REQUIRE_SIGNATURE", "true")
    actions.append("rotated_signing_key" if configured else "created_signing_key")
    return None


def _gateway_command() -> list[str]:
    executable = shutil.which("hermes")
    return [executable] if executable else [sys.executable, "-m", "hermes_cli.main"]


def _gateway_state() -> tuple[bool, bool]:
    try:
        from hermes_cli.gateway import get_gateway_runtime_snapshot

        snapshot = get_gateway_runtime_snapshot()
        return bool(snapshot.running), bool(snapshot.service_installed)
    except Exception:
        return False, False


def _wait_for_gateway(timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gateway_state()[0]:
            return True
        time.sleep(0.5)
    return False


def _start_gateway(actions: list[str]) -> bool:
    running, service_installed = _gateway_state()
    command = _gateway_command()
    if running:
        result = subprocess.run(
            [*command, "gateway", "restart"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            return False
        actions.append("restarted_gateway")
        return _wait_for_gateway()

    if service_installed:
        result = subprocess.run(
            [*command, "gateway", "start"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            return False
        actions.append("started_gateway_service")
        return _wait_for_gateway()

    try:
        from hermes_cli.config import get_hermes_home

        log_path = Path(get_hermes_home()) / "inkbox-bootstrap-gateway.log"
    except Exception:
        log_path = Path.home() / ".hermes" / "inkbox-bootstrap-gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            [*command, "gateway", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    actions.append("started_gateway_process")
    return _wait_for_gateway()


def bootstrap(
    *,
    identity_handle: str,
    api_key: str,
    base_url: str = INKBOX_BASE_URL_DEFAULT,
    voice_ai: bool = False,
    voice_ai_instructions: str | None = None,
    rotate_signing_key: bool = False,
    start_gateway: bool = False,
) -> dict[str, Any]:
    """Configure an existing Inkbox identity without interactive prompts."""
    handle = _normalize_handle(identity_handle)
    if not handle:
        return {"status": "error", "error": "identity is required"}
    if not api_key.strip():
        return {"status": "error", "error": "API key is required"}

    actions: list[str] = []
    secrets = [api_key.strip()]
    try:
        previous_handle = _normalize_handle(_env("INKBOX_IDENTITY"))
        symbols = _load_inkbox_symbols()
        agent_key, identity = _resolve_credentials(
            api_key=api_key.strip(),
            expected_handle=handle,
            base_url=base_url,
            symbols=symbols,
            actions=actions,
        )
        secrets.append(agent_key)
        client = symbols["Inkbox"](**inkbox_client_kwargs(agent_key, base_url))

        _save("INKBOX_API_KEY", agent_key)
        _save("INKBOX_IDENTITY", handle)
        if base_url:
            _save("INKBOX_BASE_URL", base_url)
        _save("INKBOX_ALLOW_ALL_USERS", "true")
        actions.append("saved_hermes_configuration")

        if voice_ai:
            _configure_voice_ai(identity, client, voice_ai_instructions)
            actions.append("configured_voice_ai")

        signing_blocker = _configure_signing_key(
            identity,
            rotate=rotate_signing_key,
            local_key_matches_identity=not previous_handle or previous_handle == handle,
            actions=actions,
        )
        _seed_identity_state(identity)

        if signing_blocker:
            return {
                "status": "requires_human",
                "identity": handle,
                "actions": actions,
                "human_actions": [signing_blocker],
                "next_commands": ["hermes inkbox bootstrap --identity <handle> --start-gateway"],
            }

        gateway_running = False
        if start_gateway:
            gateway_running = _start_gateway(actions)
            if not gateway_running:
                return {
                    "status": "error",
                    "identity": handle,
                    "actions": actions,
                    "error": "Hermes gateway did not become ready. Check inkbox-bootstrap-gateway.log.",
                }

        return {
            "status": "configured",
            "identity": handle,
            "actions": actions,
            "gateway_running": gateway_running,
            "next_commands": [] if gateway_running else ["hermes inkbox doctor", "hermes gateway run"],
        }
    except Exception as exc:
        return {
            "status": "error",
            "identity": handle,
            "actions": actions,
            "error": _redact_error(exc, secrets),
        }
