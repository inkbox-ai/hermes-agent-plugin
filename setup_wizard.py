"""Interactive setup wizard for the Inkbox Hermes plugin."""

from __future__ import annotations

import asyncio
import getpass
import importlib
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

try:
    from .config import INKBOX_BASE_URL_DEFAULT
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import INKBOX_BASE_URL_DEFAULT

try:
    from hermes_cli.colors import Colors, color
except Exception:  # pragma: no cover - local tests without Hermes
    class Colors:
        CYAN = ""
        DIM = ""
        GREEN = ""
        RED = ""
        YELLOW = ""
        BOLD = ""

    def color(text: str, *_args: Any) -> str:
        return text

try:
    from hermes_cli.cli_output import print_error, print_info, print_success, print_warning
except Exception:  # pragma: no cover - local tests without Hermes
    def print_error(message: str) -> None:
        print(message)

    def print_info(message: str) -> None:
        print(message)

    def print_success(message: str) -> None:
        print(message)

    def print_warning(message: str) -> None:
        print(message)

try:
    from hermes_cli.secret_prompt import masked_secret_prompt
except Exception:  # pragma: no cover - local tests without Hermes
    masked_secret_prompt = None


INKBOX_MIN_VERSION = "0.4.10"
INKBOX_REQUIREMENTS = (f"inkbox>={INKBOX_MIN_VERSION}", "aiohttp>=3.9", "segno>=1.5")
_BRACKETED_PASTE_PATTERN = re.compile(r"\x1b\[\s*200~|\x1b\[\s*201~")
_AVATAR_PATH = Path(__file__).resolve().parent / "assets" / "hermes_with_iphone.png"
OPENAI_REALTIME_TEST_MODEL = "gpt-realtime-2"
OPENAI_REALTIME_TEST_URL = "wss://api.openai.com/v1/realtime"


def print_header(title: str) -> None:
    print()
    print(color(f"* {title}", Colors.CYAN, Colors.BOLD))


def _show_qr(data: str) -> bool:
    stdout = getattr(sys, "stdout", None)
    if stdout is not None and hasattr(stdout, "isatty") and not stdout.isatty():
        return False
    try:
        import segno
    except ImportError:
        return False
    try:
        segno.make(data).terminal(compact=True)
        return True
    except Exception:
        return False


def _sanitize_pasted_input(value: str) -> str:
    if not isinstance(value, str) or not value:
        return value
    return _BRACKETED_PASTE_PATTERN.sub("", value)


def _is_interactive_stdin() -> bool:
    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except Exception:
        return False


def prompt(question: str, default: str | None = None, *, password: bool = False) -> str:
    display = f"{question} [{default}]: " if default else f"{question}: "
    try:
        if password:
            if masked_secret_prompt is not None:
                value = masked_secret_prompt(color(display, Colors.YELLOW))
            else:
                value = getpass.getpass(display)
        else:
            value = input(color(display, Colors.YELLOW))
    except (KeyboardInterrupt, EOFError):
        print()
        raise SystemExit(1)
    cleaned = _sanitize_pasted_input(value)
    return cleaned.strip() or default or ""


def prompt_yes_no(question: str, default: bool = True) -> bool:
    default_word = "yes" if default else "no"
    while True:
        try:
            value = input(color(f"{question} [y/n] (default: {default_word}): ", Colors.YELLOW)).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print_error("Please enter 'y' or 'n'.")


def prompt_choice(
    question: str,
    choices: list[str],
    default: int = 0,
    *,
    description: str | None = None,
) -> int:
    print(color(question, Colors.YELLOW))
    if description:
        for line in description.splitlines():
            print_info(f"  {line}")
    for idx, choice in enumerate(choices, start=1):
        marker = "*" if idx - 1 == default else " "
        print(f"  {marker} {idx}. {choice}")
    while True:
        try:
            value = input(color(f"  Select [1-{len(choices)}] ({default + 1}): ", Colors.DIM)).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)
        if not value:
            return default
        try:
            selected = int(value) - 1
        except ValueError:
            print_error("Please enter a number.")
            continue
        if 0 <= selected < len(choices):
            return selected
        print_error(f"Please enter a number between 1 and {len(choices)}.")


def _save(name: str, value: str) -> None:
    if value == "":
        return
    from hermes_cli.config import save_env_value

    save_env_value(name, value)


def _env(name: str) -> str:
    try:
        from hermes_cli.config import get_env_value

        return os.getenv(name) or get_env_value(name) or ""
    except Exception:
        return os.getenv(name, "")


def _config_realtime_api_key() -> str:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return ""

    platforms = cfg.get("platforms") if isinstance(cfg, dict) else {}
    inkbox = platforms.get("inkbox") if isinstance(platforms, dict) else {}
    realtime = inkbox.get("realtime") if isinstance(inkbox, dict) else {}
    api_key = realtime.get("api_key") if isinstance(realtime, dict) else ""
    return str(api_key or "").strip()


def _hermes_openai_api_key() -> tuple[str, str] | None:
    try:
        from hermes_cli.auth import has_usable_secret, resolve_api_key_provider_credentials

        creds = resolve_api_key_provider_credentials("openai-api")
    except Exception:
        return None

    api_key = str(creds.get("api_key") or "").strip()
    if not api_key or not has_usable_secret(api_key):
        return None
    source = str(creds.get("source") or "openai-api").strip() or "openai-api"
    return source, api_key


def _detect_openai_realtime_key() -> tuple[str, str] | None:
    config_key = _config_realtime_api_key()
    if config_key:
        return "platforms.inkbox.realtime.api_key", config_key
    realtime_key = _env("INKBOX_REALTIME_API_KEY").strip()
    if realtime_key:
        return "INKBOX_REALTIME_API_KEY", realtime_key
    hermes_key = _hermes_openai_api_key()
    if hermes_key is not None:
        return hermes_key
    openai_key = _env("OPENAI_API_KEY").strip()
    if openai_key:
        return "OPENAI_API_KEY", openai_key
    return None


def _install_commands() -> list[list[list[str]]]:
    plans: list[list[list[str]]] = []
    uv = shutil.which("uv")
    if uv:
        plans.append([[uv, "pip", "install", "--python", sys.executable, *INKBOX_REQUIREMENTS]])
    plans.append([[sys.executable, "-m", "pip", "install", *INKBOX_REQUIREMENTS]])
    plans.append(
        [
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            [sys.executable, "-m", "pip", "install", *INKBOX_REQUIREMENTS],
        ]
    )
    return plans


def _install_command_text() -> str:
    return " && ".join(shlex.join(command) for command in _install_commands()[0])


def _run_install_plan() -> bool:
    last_exc: Exception | None = None
    for plan in _install_commands():
        try:
            for command in plan:
                subprocess.check_call(command)
            return True
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        print_error(f"Install failed: {last_exc}")
    return False


def _purge_inkbox_modules() -> None:
    for name in list(sys.modules):
        if name == "inkbox" or name.startswith("inkbox."):
            sys.modules.pop(name, None)


def _load_inkbox_symbols() -> dict[str, Any]:
    from inkbox import Inkbox
    from inkbox.exceptions import InkboxAPIError
    from inkbox.identities.types import IdentityPhoneNumberCreateOptions
    from inkbox.whoami.types import (
        AUTH_SUBTYPE_API_KEY_ADMIN_SCOPED,
        AUTH_SUBTYPE_API_KEY_AGENT_SCOPED_CLAIMED,
        AUTH_SUBTYPE_API_KEY_AGENT_SCOPED_UNCLAIMED,
        WhoamiApiKeyResponse,
    )

    return {
        "Inkbox": Inkbox,
        "InkboxAPIError": InkboxAPIError,
        "IdentityPhoneNumberCreateOptions": IdentityPhoneNumberCreateOptions,
        "WhoamiApiKeyResponse": WhoamiApiKeyResponse,
        "ADMIN_SCOPED": AUTH_SUBTYPE_API_KEY_ADMIN_SCOPED,
        "AGENT_CLAIMED": AUTH_SUBTYPE_API_KEY_AGENT_SCOPED_CLAIMED,
        "AGENT_UNCLAIMED": AUTH_SUBTYPE_API_KEY_AGENT_SCOPED_UNCLAIMED,
    }


def _parse_version(value: str) -> tuple[int, ...]:
    # Best-effort numeric parse of "X.Y.Z" so we can compare without packaging.
    parts: list[int] = []
    for chunk in value.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if digits == "":
            break
        parts.append(int(digits))
    return tuple(parts)


def _inkbox_version_ok() -> bool:
    # Treat an outdated SDK like a missing requirement so the install/upgrade path runs.
    try:
        installed = importlib.metadata.version("inkbox")
    except Exception:
        return False
    try:
        from packaging.version import Version

        return Version(installed) >= Version(INKBOX_MIN_VERSION)
    except Exception:
        # Fall back to a simple parsed-tuple comparison when packaging is unavailable.
        return _parse_version(installed) >= _parse_version(INKBOX_MIN_VERSION)


def _ensure_inkbox_sdk() -> dict[str, Any] | None:
    try:
        symbols = _load_inkbox_symbols()
        if _inkbox_version_ok():
            return symbols
        first_error = (
            f"inkbox SDK is older than {INKBOX_MIN_VERSION}; an upgrade is required."
        )
    except Exception as exc:
        first_error = exc

    print_warning("The Python Inkbox SDK is not available in the Hermes environment.")
    print_info("The setup command is running under:")
    print_info(f"  {sys.executable}")
    print_info("Install or upgrade the SDK in that exact environment with:")
    print_info(f"  {_install_command_text()}")
    print_info(f"Import error: {first_error}")

    if not _is_interactive_stdin():
        return None
    if not prompt_yes_no("Install/upgrade Inkbox SDK in this Hermes environment now?", True):
        return None

    if not _run_install_plan():
        print_info("Run this command manually, then rerun setup:")
        print_info(f"  {_install_command_text()}")
        return None

    importlib.invalidate_caches()
    _purge_inkbox_modules()
    try:
        return _load_inkbox_symbols()
    except Exception as retry_exc:
        print_error(f"Inkbox SDK still cannot be imported: {retry_exc}")
        print_info("Run this command manually, then rerun setup:")
        print_info(f"  {_install_command_text()}")
        return None


def _error_status(exc: Exception) -> Any:
    return getattr(exc, "status_code", "?")


def _error_detail(exc: Exception) -> str:
    return str(getattr(exc, "detail", "") or exc)


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _seed_identity_state(identity: Any) -> None:
    try:
        from hermes_cli.config import get_hermes_home

        mailbox = getattr(identity, "mailbox", None)
        phone = getattr(identity, "phone_number", None)
        tunnel = getattr(identity, "tunnel", None)
        state = {
            "handle": getattr(identity, "agent_handle", None),
            "email_address": (
                getattr(identity, "email_address", None)
                or (getattr(mailbox, "email_address", None) if mailbox else None)
            ),
            "phone_number": getattr(phone, "number", None) if phone else None,
            "phone_number_id": str(getattr(phone, "id", "")) if phone else None,
            "imessage_enabled": bool(getattr(identity, "imessage_enabled", False)),
            "tunnel_public_host": getattr(tunnel, "public_host", None) if tunnel else None,
        }
        path = Path(get_hermes_home()) / "inkbox_identity_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, path)
    except Exception as exc:
        print_warning(f"  Could not seed inkbox_identity_state.json: {exc}")
        print_info("  Start the gateway and it will populate the file on connect.")


def _redact_key_source(name: str) -> str:
    if name == "platforms.inkbox.realtime.api_key":
        return "platforms.inkbox.realtime.api_key"
    if name == "INKBOX_REALTIME_API_KEY":
        return "INKBOX_REALTIME_API_KEY"
    if name == "OPENAI_API_KEY":
        return "OPENAI_API_KEY"
    if name == "credential_pool:openai-api":
        return "Hermes credential pool (openai-api)"
    if name == "openai-api":
        return "Hermes OpenAI API credentials"
    return "the configured OpenAI API key"


async def _test_openai_realtime_api_key_async(api_key: str, model: str) -> tuple[bool, str]:
    try:
        import aiohttp
    except Exception as exc:
        return False, f"aiohttp is not available in this Hermes environment: {exc}"

    url = f"{OPENAI_REALTIME_TEST_URL}?{urlencode({'model': model})}"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=12)
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": model,
            "instructions": "Validation probe. Do not speak unless audio is provided.",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "noise_reduction": None,
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": "cedar",
                },
            },
        },
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(url, headers=headers, heartbeat=10) as ws:
                await ws.send_str(json.dumps(session_update))
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 8.0
                saw_session_created = False
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        if saw_session_created:
                            return True, "OpenAI Realtime websocket accepted the key."
                        return False, "Timed out waiting for an OpenAI Realtime session response."
                    msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            event = json.loads(msg.data)
                        except Exception:
                            continue
                        event_type = str(event.get("type") or "")
                        if event_type == "session.updated":
                            return True, "OpenAI Realtime session update succeeded."
                        if event_type == "session.created":
                            saw_session_created = True
                            continue
                        if event_type == "error":
                            error = event.get("error") if isinstance(event.get("error"), dict) else event
                            message = str(error.get("message") or event).strip()
                            code = str(error.get("code") or "").strip()
                            prefix = f"{code}: " if code else ""
                            return False, f"{prefix}{message}"
                    if msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                        detail = str(getattr(ws, "exception", lambda: None)() or "websocket closed")
                        return False, detail
    except aiohttp.WSServerHandshakeError as exc:
        if exc.status in {401, 403}:
            return False, f"OpenAI rejected the key or Realtime permission: HTTP {exc.status}"
        return False, f"OpenAI Realtime websocket handshake failed: HTTP {exc.status} {exc.message}"
    except asyncio.TimeoutError:
        return False, "Timed out connecting to OpenAI Realtime."
    except Exception as exc:
        return False, str(exc)


def _test_openai_realtime_api_key(api_key: str, model: str = OPENAI_REALTIME_TEST_MODEL) -> tuple[bool, str]:
    try:
        return asyncio.run(_test_openai_realtime_api_key_async(api_key, model))
    except RuntimeError as exc:
        return False, f"Could not run Realtime validation from this setup process: {exc}"


def _configure_realtime_calls(identity: Any) -> None:
    phone = getattr(identity, "phone_number", None)
    if phone is None:
        return

    print()
    print(color("  --- OpenAI Realtime calls ---", Colors.CYAN))
    print_info("  Realtime calls send raw phone audio to OpenAI Realtime.")
    print_info("  This requires an OpenAI API key with /v1/realtime permission.")

    detected = _detect_openai_realtime_key()
    detected_key = ""
    default_opt_in = False
    prompt_for_key = False
    if detected is not None:
        key_source, detected_key = detected
        default_opt_in = True
        print_success(f"  Found existing OpenAI API key in {_redact_key_source(key_source)}.")
    else:
        print_warning("  No OpenAI API key was detected for Realtime.")
        print_info("  If you opt in, paste an OpenAI API key in the next step.")
        print_info("  The wizard will test the key before enabling Realtime calls.")

    while True:
        if not prompt_yes_no("  Use OpenAI Realtime API for phone calls?", default_opt_in):
            _save("INKBOX_REALTIME_ENABLED", "false")
            print_info("  Realtime disabled. Calls will use Inkbox STT/TTS.")
            return

        if prompt_for_key or not detected_key:
            api_key = prompt("  Paste your OpenAI API key for Realtime calls", password=True).strip()
        else:
            api_key = detected_key
        if not api_key:
            _save("INKBOX_REALTIME_ENABLED", "false")
            print_warning("  No OpenAI API key entered. Realtime disabled; calls will use Inkbox STT/TTS.")
            return

        print_info(f"  Testing OpenAI Realtime access with {OPENAI_REALTIME_TEST_MODEL}...")
        ok, detail = _test_openai_realtime_api_key(api_key, OPENAI_REALTIME_TEST_MODEL)
        if not ok:
            _save("INKBOX_REALTIME_ENABLED", "false")
            print_error("  OpenAI Realtime validation failed.")
            print_info(f"  {detail}")
            print_info("  Realtime remains disabled. Try another key, or answer no to use Inkbox STT/TTS.")
            default_opt_in = True
            prompt_for_key = True
            continue

        _save("INKBOX_REALTIME_ENABLED", "true")
        _save("INKBOX_REALTIME_MODEL", OPENAI_REALTIME_TEST_MODEL)
        # Persist the exact validated key under the plugin-specific env var so the
        # gateway does not depend on the operator's shell exporting OPENAI_API_KEY.
        _save("INKBOX_REALTIME_API_KEY", api_key)
        print_success("  OpenAI Realtime validation succeeded.")
        print_info("  Realtime calls are enabled for this Hermes Inkbox gateway.")
        return


def _setup_signing_key(api_key: str, base_url: str, Inkbox: Any) -> None:
    print()
    print(color("  --- Webhook signing key ---", Colors.CYAN))
    print_info("  Inkbox signs outbound webhooks with an HMAC over the body.")
    print_info("  Without the matching key, the gateway cannot verify inbound Inkbox traffic.")

    print_info("  A signing key is required to continue.")

    has_key = prompt_yes_no("  Do you already have an Inkbox signing key?", False)
    if has_key:
        key = prompt("  Paste your Inkbox signing key", password=True).strip()
        if key:
            _save("INKBOX_SIGNING_KEY", key)
            _save("INKBOX_REQUIRE_SIGNATURE", "true")
            print_success("  Saved signing key. Signature verification enabled.")
            return
        # An empty paste can't satisfy the requirement — fall through to mint one.
        print_warning("  No key entered; a signing key is required, so we'll mint one now.")

    print_info("  Minting a new key here rotates any existing key for your org.")
    print_info("  Any other gateway using the old key will fail verification until updated.")
    if not prompt_yes_no("  Generate a new signing key now?", True):
        print_error("  A signing key is required; cannot complete setup without one.")
        print_info("  Re-run setup and paste an existing key, or allow key generation.")
        raise SystemExit(1)

    try:
        new_key = Inkbox(api_key=api_key, base_url=base_url).create_signing_key()
    except Exception as exc:
        print_error(f"  Failed to create signing key: {exc}")
        print_error("  A signing key is required; aborting setup. Retry, or paste an existing key.")
        raise SystemExit(1)

    signing_key = str(getattr(new_key, "signing_key", "") or "")
    if not signing_key:
        print_error("  Signing-key response did not include signing_key.")
        print_error("  A signing key is required; aborting setup.")
        raise SystemExit(1)
    _save("INKBOX_SIGNING_KEY", signing_key)
    _save("INKBOX_REQUIRE_SIGNATURE", "true")
    created_at = getattr(new_key, "created_at", None)
    if created_at is not None and hasattr(created_at, "isoformat"):
        print_success(f"  Generated and saved signing key (created at {created_at.isoformat()}).")
    else:
        print_success("  Generated and saved signing key.")
    print_info("  Signature verification enabled.")


def _wait_for_sms_opt_in(api_key: str, base_url: str, phone: Any, Inkbox: Any) -> None:
    if phone is None or getattr(phone, "type", None) != "local":
        return
    phone_id = getattr(phone, "id", None)
    if phone_id is None:
        return

    def find_start(texts: Any) -> Any | None:
        for text in texts:
            direction = (getattr(text, "direction", "") or "").lower()
            body = (getattr(text, "text", "") or "").strip().upper()
            if direction == "inbound" and body == "START":
                return text
        return None

    try:
        client = Inkbox(api_key=api_key, base_url=base_url)
    except Exception:
        return

    print()
    print(color("  --- Waiting for your START text ---", Colors.YELLOW))
    print_info(f"  Polling every 3s for an inbound START to {phone.number}.")
    print_info("  Without it, the agent cannot send outbound SMS to that phone later.")
    print_info("  Press Ctrl+C to skip; you can text START anytime.")

    spinner = "|/-\\"
    idx = 0
    next_poll_at = time.monotonic()
    clear_line = "\r" + " " * 72 + "\r"

    try:
        while True:
            now = time.monotonic()
            if now >= next_poll_at:
                try:
                    texts = client.texts.list(phone_id, limit=20)
                except Exception:
                    texts = []
                match = find_start(texts)
                if match is not None:
                    remote = getattr(match, "remote_phone_number", "")
                    sys.stdout.write(clear_line)
                    sys.stdout.flush()
                    print_success(f"  Got it. SMS opt-in confirmed from {remote}")
                    return
                next_poll_at = now + 3.0
            sys.stdout.write(f"\r  {spinner[idx]} Listening for START...  ")
            sys.stdout.flush()
            idx = (idx + 1) % len(spinner)
            time.sleep(0.25)
    except KeyboardInterrupt:
        sys.stdout.write(clear_line)
        sys.stdout.flush()
        print()
        print_warning(f"  Skipped. Text START to {phone.number} anytime to enable outbound SMS.")


async def _identity_has_avatar_async(base_url: str, api_key: str, handle: str) -> bool | None:
    """Check whether an identity already has a contact-card avatar."""
    import aiohttp

    url = f"{base_url.rstrip('/')}/api/v1/identities/{handle}/avatar"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"X-API-Key": api_key}) as resp:
                if resp.status == 200:
                    return True
                if resp.status == 404:
                    return False
                return None
    except Exception:
        return None


async def _upload_avatar_async(
    base_url: str, api_key: str, handle: str, image: bytes
) -> tuple[bool, str]:
    """PUT the Hermes avatar image to the identity's avatar endpoint."""
    import aiohttp

    url = f"{base_url.rstrip('/')}/api/v1/identities/{handle}/avatar"
    timeout = aiohttp.ClientTimeout(total=30)
    form = aiohttp.FormData()
    form.add_field("file", image, filename="hermes_with_iphone.png", content_type="image/png")
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(url, headers={"X-API-Key": api_key}, data=form) as resp:
                if resp.status in (200, 201):
                    return True, "ok"
                return False, f"HTTP {resp.status} {(await resp.text())[:200]}"
    except Exception as exc:
        return False, str(exc)


def _identity_has_avatar(base_url: str, api_key: str, handle: str) -> bool | None:
    try:
        return asyncio.run(_identity_has_avatar_async(base_url, api_key, handle))
    except RuntimeError:
        return None


def _upload_avatar(base_url: str, api_key: str, handle: str, image: bytes) -> tuple[bool, str]:
    try:
        return asyncio.run(_upload_avatar_async(base_url, api_key, handle, image))
    except RuntimeError as exc:
        return False, f"could not run avatar upload from this setup process: {exc}"


def _configure_avatar(base_url: str, api_key: str, identity: Any, *, is_signup: bool) -> None:
    """Attach the bundled Hermes avatar to the agent's Inkbox contact card."""
    handle = getattr(identity, "agent_handle", "") or ""
    if not handle or not _AVATAR_PATH.exists():
        return

    if not is_signup:
        if _identity_has_avatar(base_url, api_key, handle) is True:
            return
        print()
        print(color("  --- Agent avatar ---", Colors.CYAN))
        print_info("  This agent has no avatar on its Inkbox contact card.")
        if not prompt_yes_no("  Add the Hermes avatar?", True):
            print_info("  Skipped. You can set an avatar later in the Inkbox console.")
            return

    try:
        image = _AVATAR_PATH.read_bytes()
    except Exception as exc:
        print_warning(f"  Could not read the bundled avatar: {exc}")
        return

    ok, detail = _upload_avatar(base_url, api_key, handle, image)
    if ok:
        print_success("  Attached the Hermes avatar to this agent.")
    else:
        print_warning(f"  Could not attach the avatar: {detail}")
        print_info("  You can set one later in the Inkbox console.")


def _configure_imessage(api_key: str, base_url: str, handle: str, Inkbox: Any) -> None:
    """Offer to enable iMessage for the agent and walk through connecting.

    Args:
        api_key (str): The agent-scoped Inkbox API key the wizard saved.
        base_url (str): Inkbox API base URL.
        handle (str): Agent identity handle being configured.
        Inkbox (Any): The Inkbox SDK client class.

    Returns:
        None: Prints progress; failures degrade to a warning and return.
    """
    print()
    print(color("  --- iMessage ---", Colors.CYAN))
    print_info("  Inkbox can make this agent reachable over iMessage from your iPhone.")
    print_info("  No number to provision — you connect through the Inkbox iMessage router.")

    try:
        client = Inkbox(api_key=api_key, base_url=base_url)
        identity = client.get_identity(handle)
    except Exception as exc:
        print_warning(f"  Could not load the identity for iMessage setup: {exc}")
        return

    # Old SDKs predate iMessage entirely — detect by surface, not version.
    if not hasattr(client, "imessages") or not hasattr(identity, "imessage_enabled"):
        print_warning("  The installed Inkbox SDK does not support iMessage yet.")
        print_info("  Upgrade it and rerun setup:")
        print_info(f"    {_install_command_text()}")
        return

    if identity.imessage_enabled:
        print_success("  iMessage is already enabled for this agent.")
    else:
        if not prompt_yes_no("  Enable iMessage for this agent?", True):
            print_info("  Skipped. Rerun `hermes inkbox setup` anytime to enable iMessage.")
            return
        try:
            identity.update(imessage_enabled=True)
        except Exception as exc:
            print_error(f"  Could not enable iMessage: {exc}")
            print_info("  You can enable it later from the Inkbox console and rerun setup.")
            return
        print_success("  iMessage enabled for this agent.")
        try:
            # Re-fetch so the local object reflects the new flag (the SDK
            # gates its iMessage helpers on it).
            identity = client.get_identity(handle)
        except Exception as exc:
            print_warning(f"  Could not refresh the identity after enabling: {exc}")
            return

    # Surface phones already connected through the router so reruns don't
    # read like a first-time setup, and default the walkthrough off when a
    # connection already exists (connecting another phone is the rare case).
    connected = []
    list_assignments = getattr(identity, "list_imessage_assignments", None)
    if callable(list_assignments):
        try:
            connected = list(list_assignments(limit=5))
        except Exception:
            connected = []
        if connected:
            numbers = ", ".join(
                str(getattr(a, "remote_number", "") or "") for a in connected
            )
            print_success(f"  Already connected: {numbers}")

    question = (
        "  Connect another iPhone to this agent now?"
        if connected
        else "  Connect your iPhone to this agent now?"
    )
    if not prompt_yes_no(question, not connected):
        print_info("  You can connect anytime — rerun `hermes inkbox setup` for the walkthrough.")
        return
    _wait_for_imessage_first_message(client, identity, handle)


def _wait_for_imessage_first_message(client: Any, identity: Any, handle: str) -> None:
    """Walk the user through the iMessage connect flow and greet them back.

    Args:
        client (Any): Authenticated Inkbox SDK client (agent-scoped key).
        identity (Any): The iMessage-enabled agent identity object.
        handle (str): Agent identity handle, used in the welcome message.

    Returns:
        None: Polls until the first inbound iMessage arrives (Ctrl+C skips),
        then sends the channel-introduction reply into that conversation.
    """
    from datetime import datetime, timezone

    try:
        triage = client.imessages.get_triage_number()
    except Exception as exc:
        print_warning(f"  Could not fetch the iMessage router number: {exc}")
        print_info("  Rerun `hermes inkbox setup` later to finish connecting.")
        return

    connect_command = str(getattr(triage, "connect_command", "") or "").strip()
    if not connect_command or "your-handle" in connect_command:
        connect_command = f"connect @{handle}"

    print()
    print_info("  From your iPhone, in the Messages app:")
    print(color(f"      1. Text \"{connect_command}\" to {triage.number}", Colors.BOLD))
    print_info("    2. Inkbox texts you back from the number now assigned to this agent.")
    print_info("    3. Send any first message (e.g. \"hi\") in that NEW thread.")
    print_info("  The agent can only message you after you message it first.")

    sms_link = str(getattr(triage, "sms_link", "") or "").strip()
    if not sms_link or "your-handle" in sms_link:
        sms_link = f"sms:{triage.number}?&body={quote(connect_command)}"

    qr_payload = f"SMSTO:{triage.number}:{connect_command}"
    print()
    print_info("  Or just scan this with your iPhone camera to do step 1 in one tap:")
    print()
    if not _show_qr(qr_payload):
        print_info(f"    (install 'segno' to show a scannable QR here: {sms_link})")

    print()
    print(color("  --- Waiting for your first iMessage ---", Colors.YELLOW))
    print_info("  Polling every 3s for an inbound iMessage to this agent.")
    print_info("  Press Ctrl+C to skip; you can connect anytime.")

    started_at = datetime.now(timezone.utc)

    def find_first_inbound(messages: Any) -> Any | None:
        for message in messages:
            direction = (_enum_value(getattr(message, "direction", "")) or "").lower()
            if direction != "inbound":
                continue
            created_at = getattr(message, "created_at", None)
            # Ignore traffic from a connection that predates this run.
            # Naive timestamps can't be compared to the aware cutoff —
            # accept those rather than crash the poll loop.
            if (
                created_at is not None
                and created_at.tzinfo is not None
                and created_at < started_at
            ):
                continue
            return message
        return None

    spinner = "|/-\\"
    idx = 0
    next_poll_at = time.monotonic()
    clear_line = "\r" + " " * 72 + "\r"
    match = None

    try:
        while match is None:
            now = time.monotonic()
            if now >= next_poll_at:
                try:
                    messages = identity.list_imessages(limit=10)
                except Exception:
                    messages = []
                match = find_first_inbound(messages)
                next_poll_at = now + 3.0
            if match is None:
                sys.stdout.write(f"\r  {spinner[idx]} Listening for your first iMessage...  ")
                sys.stdout.flush()
                idx = (idx + 1) % len(spinner)
                time.sleep(0.25)
    except KeyboardInterrupt:
        sys.stdout.write(clear_line)
        sys.stdout.flush()
        print()
        print_warning("  Skipped. The agent replies over iMessage once you connect and message it.")
        return

    sys.stdout.write(clear_line)
    sys.stdout.flush()
    remote = getattr(match, "remote_number", "") or "your phone"
    print_success(f"  Got it. First iMessage received from {remote}.")

    conversation_id = getattr(match, "conversation_id", None)
    welcome = (
        f"You're connected! This is your iMessage channel to your Hermes agent "
        f"@{handle}. Anything you send here goes straight to the agent, and its "
        f"replies will show up right in this thread."
    )
    try:
        identity.send_imessage(conversation_id=conversation_id, text=welcome)
        print_success("  Sent a welcome message back on that thread.")
    except Exception as exc:
        print_warning(f"  Could not send the welcome message: {exc}")
    try:
        # Clear the unread flag the walkthrough message left behind.
        identity.mark_imessage_conversation_read(conversation_id)
    except Exception:
        pass
    print_info("  Start the gateway (`hermes gateway run`) and keep chatting there.")
    print_info("  If the gateway is already running, restart it (`hermes gateway restart`)")
    print_info("  so it picks up this new iMessage connection.")


def _self_signup_flow(base_url: str, Inkbox: Any, InkboxAPIError: Any) -> tuple[Any | None, str, bool]:
    print()
    print_info("No problem. We will create a fresh agent identity for you.")
    print_info("You will get an Inkbox-hosted mailbox plus an API key.")
    print_info("A short verification email goes to you to claim full capabilities.")
    print()

    note = "Setting up a Hermes agent on Inkbox."
    human_email = ""
    handle = ""

    while True:
        if not human_email:
            human_email = prompt("  Your email address (for the verification step)").strip()
            if not human_email or "@" not in human_email:
                print_error("  A valid email address is required for signup.")
                return None, "", False

        if not handle:
            handle = prompt(
                "  Desired agent handle (e.g. on-call-agent, recruiting-agent) - "
                "globally unique, also becomes the mailbox local part"
            ).strip()
            if not handle:
                print_error("  Agent handle is required.")
                return None, "", False

        print()
        print_info("Calling agent-signup...")
        try:
            resp = Inkbox.signup(
                human_email=human_email,
                note_to_human=note,
                agent_handle=handle,
                base_url=base_url,
                harness="hermes",  # tag which harness drove this signup
            )
            break
        except InkboxAPIError as exc:
            detail = _error_detail(exc)
            detail_lc = detail.lower()
            status = _error_status(exc)
            print_error(f"  Signup failed: HTTP {status} {detail}")

            if status == 429 and "unclaimed agents" in detail_lc:
                ctx = (
                    f"Signup blocked: HTTP 429 - {detail}\n\n"
                    "Inkbox caps unclaimed agents per human email. To free a slot,\n"
                    "verify one of your existing unclaimed agents in the Inkbox console,\n"
                    "or use a different email below.\n\n"
                    f"Email tried: {human_email}"
                )
                if _retry_or_abort("Try a different email", error_context=ctx):
                    human_email = ""
                    continue
                return None, "", False

            if status == 409 or (status == 422 and ("handle" in detail_lc or "unavailable" in detail_lc)):
                ctx = (
                    f"Signup blocked: HTTP {status} - {detail}\n\n"
                    "Agent handles are globally unique. Pick another.\n\n"
                    f"Handle tried: {handle}"
                )
                if _retry_or_abort("Pick a different handle", error_context=ctx):
                    handle = ""
                    continue
                return None, "", False

            ctx = (
                f"Signup failed: HTTP {status} - {detail}\n\n"
                "Inputs tried:\n"
                f"  Email:  {human_email}\n"
                f"  Handle: {handle}"
            )
            if _retry_or_abort("Re-enter all details and try again", error_context=ctx):
                human_email = handle = ""
                continue
            return None, "", False
        except Exception as exc:
            print_error(f"  Signup failed: {exc}")
            return None, "", False

    print_success(f"Agent created - mailbox: {resp.email_address}")
    print_info(f"  Handle: {resp.agent_handle}")
    print_info(f"  A verification email was sent to {human_email}.")
    print_info("  Enter the 6-digit code from that email to claim the agent.")
    print()

    max_attempts = 3
    attempts_used = 0
    verified = False
    while True:
        attempts_left = max_attempts - attempts_used
        if attempts_left <= 0:
            prompt_text = "  Type 'resend' for a new code (Ctrl+C to abort)"
        else:
            prompt_text = f"  Verification code, or 'resend' for a new email ({attempts_left}/{max_attempts} attempts left)"

        entry = prompt(prompt_text).strip()
        if entry.lower() in {"resend", "r"}:
            if _try_resend(Inkbox, InkboxAPIError, resp.api_key, base_url, human_email):
                attempts_used = 0
            continue
        if not entry:
            print_warning("  Type the 6-digit code, or 'resend' for a fresh email.")
            continue
        if attempts_left <= 0:
            print_warning("  This code is dead. Type 'resend' before trying another code.")
            continue
        try:
            verify = Inkbox.verify_signup(api_key=resp.api_key, verification_code=entry, base_url=base_url)
            print_success(f"  Verified - claim status: {verify.claim_status}")
            verified = True
            break
        except InkboxAPIError as exc:
            attempts_used += 1
            print_error(
                f"  Wrong code: HTTP {_error_status(exc)} {_error_detail(exc)} "
                f"({attempts_used}/{max_attempts} attempts used)"
            )
            if attempts_used >= max_attempts:
                print_warning("  This code is now dead. Type 'resend' for a fresh one.")
        except Exception as exc:
            print_error(f"  Verification failed: {exc}")

    provisioned_phone = None
    if verified:
        print()
        print_info("Phone number - optional, but unlocks SMS and voice.")
        print_info("  We provision a local US number so SMS is supported.")
        if prompt_yes_no("  Provision a phone number for this agent?", True):
            try:
                client = Inkbox(api_key=resp.api_key, base_url=base_url)
                provisioned_phone = client.phone_numbers.provision(agent_handle=resp.agent_handle, type="local")
                print_success(f"  Provisioned: {provisioned_phone.number}")
            except InkboxAPIError as exc:
                print_warning(f"  Phone provisioning failed: HTTP {_error_status(exc)} {_error_detail(exc)}")
                print_info("  You can provision a number later in the Inkbox console.")
            except Exception as exc:
                print_warning(f"  Phone provisioning failed: {exc}")

    class MailboxShim:
        email_address = resp.email_address
        display_name = None

    class PhoneShim:
        def __init__(self, phone: Any):
            self.number = phone.number
            self.type = getattr(phone, "type", "local")
            self.sms_status = getattr(phone, "sms_status", None)
            self.id = getattr(phone, "id", None)

    class SignupIdentityShim:
        agent_handle = resp.agent_handle
        email_address = resp.email_address
        mailbox = MailboxShim()
        phone_number = PhoneShim(provisioned_phone) if provisioned_phone else None

    return SignupIdentityShim(), resp.api_key, provisioned_phone is not None


def _retry_or_abort(retry_label: str, *, error_context: str = "") -> bool:
    print()
    choice = prompt_choice(
        "  What now?",
        [retry_label, "Abort - keep existing Inkbox configuration unchanged"],
        0,
        description=error_context or None,
    )
    if choice == 0:
        return True
    print_warning("  Aborted. No credentials saved.")
    print_info("  Your existing INKBOX_IDENTITY in .env is unchanged.")
    return False


def _try_resend(Inkbox: Any, InkboxAPIError: Any, api_key: str, base_url: str, human_email: str) -> bool:
    try:
        Inkbox.resend_signup_verification(api_key=api_key, base_url=base_url)
        print_success(f"  Resent. Check {human_email}.")
        return True
    except InkboxAPIError as exc:
        print_warning(f"  Resend failed: HTTP {_error_status(exc)} {_error_detail(exc)}")
        if _error_status(exc) == 429:
            print_info("  Wait out the cooldown before trying again.")
        return False
    except Exception as exc:
        print_warning(f"  Resend failed: {exc}")
        return False


def _api_key_flow(
    base_url: str,
    Inkbox: Any,
    InkboxAPIError: Any,
    WhoamiApiKeyResponse: Any,
    ADMIN_SCOPED: Any,
    AGENT_CLAIMED: Any,
    AGENT_UNCLAIMED: Any,
    IdentityPhoneNumberCreateOptions: Any,
) -> tuple[Any | None, str, bool]:
    print()
    api_key = prompt("  Paste your Inkbox API key (ApiKey_...)", password=True).strip()
    if not api_key:
        print_error("  No key provided.")
        return None, "", False

    try:
        client = Inkbox(api_key=api_key, base_url=base_url)
        info = client.whoami()
    except InkboxAPIError as exc:
        print_error(f"  whoami failed: HTTP {_error_status(exc)} {_error_detail(exc)}")
        print_info("  Double-check the key and the environment it was issued in.")
        return None, "", False
    except Exception as exc:
        print_error(f"  whoami failed: {exc}")
        return None, "", False

    if WhoamiApiKeyResponse is not None and not isinstance(info, WhoamiApiKeyResponse):
        print_error("  This wizard requires an API key, but the credential is a JWT.")
        return None, "", False

    subtype = _enum_value(getattr(info, "auth_subtype", ""))
    org_id = getattr(info, "organization_id", "")
    print_success(f"  Key validated - org {org_id}, scope: {subtype or 'unknown'}")

    if subtype in {_enum_value(AGENT_CLAIMED), _enum_value(AGENT_UNCLAIMED)}:
        return _pick_agent_scoped(client, api_key)
    if subtype == _enum_value(ADMIN_SCOPED):
        return _pick_admin_scoped(client, api_key, IdentityPhoneNumberCreateOptions, InkboxAPIError)

    print_warning(f"  Unrecognized API-key subtype: {subtype!r}.")
    print_info("  Falling back to list_identities().")
    return _pick_admin_scoped(client, api_key, IdentityPhoneNumberCreateOptions, InkboxAPIError)


def _pick_agent_scoped(client: Any, api_key: str) -> tuple[Any | None, str, bool]:
    try:
        identities = list(client.list_identities())
    except Exception as exc:
        print_error(f"  list_identities failed: {exc}")
        return None, "", False

    if not identities:
        print_error("  Agent-scoped key but no identity returned.")
        return None, "", False
    if len(identities) > 1:
        print_warning(f"  Agent-scoped key returned {len(identities)} identities; using the first.")

    summary = identities[0]
    try:
        identity = client.get_identity(summary.agent_handle)
    except Exception as exc:
        print_error(f"  get_identity failed: {exc}")
        return None, "", False

    print()
    print_info(f"  This API key is bound to identity: {identity.agent_handle}")
    identity, did_provision_phone = _offer_phone_for_existing(client, identity)
    return identity, api_key, did_provision_phone


def _mint_agent_scoped_key(client: Any, identity: Any, InkboxAPIError: Any) -> str | None:
    try:
        created = client.api_keys.create(
            label=f"Hermes gateway - {identity.agent_handle}",
            description=(
                "Auto-minted by hermes inkbox setup. Scoped to one agent "
                "identity so the gateway never stores the admin key."
            ),
            scoped_identity_id=identity.id,
        )
    except InkboxAPIError as exc:
        print_error(f"  Could not mint agent-scoped key: HTTP {_error_status(exc)} {_error_detail(exc)}")
        return None
    except Exception as exc:
        print_error(f"  Could not mint agent-scoped key: {exc}")
        return None
    return str(getattr(created, "api_key", "") or "") or None


def _pick_admin_scoped(
    client: Any,
    api_key: str,
    IdentityPhoneNumberCreateOptions: Any,
    InkboxAPIError: Any,
) -> tuple[Any | None, str, bool]:
    try:
        identities = list(client.list_identities())
    except Exception as exc:
        print_error(f"  list_identities failed: {exc}")
        return None, "", False

    print()
    if identities:
        print_info(f"  Found {len(identities)} identity(ies). Fetching mailbox and phone details.")
        full_records: list[Any | None] = []
        for summary in identities:
            try:
                full_records.append(client.get_identity(summary.agent_handle))
            except Exception as exc:
                print_warning(f"    {summary.agent_handle}: details unavailable ({exc})")
                full_records.append(None)

        choices = []
        for summary, full in zip(identities, full_records):
            mailbox_str = (getattr(full, "email_address", None) if full else None) or getattr(summary, "email_address", None) or "no mailbox"
            phone_str = "no phone"
            phone = getattr(full, "phone_number", None) if full else None
            if phone is not None:
                phone_str = getattr(phone, "number", "phone")
            choices.append(f"{summary.agent_handle}  -  {mailbox_str}  -  {phone_str}")
        choices.append("Create a new identity")

        idx = prompt_choice("  Select the identity this Hermes gateway should run as:", choices, 0)
        if idx < len(identities):
            identity = full_records[idx]
            if identity is None:
                try:
                    identity = client.get_identity(identities[idx].agent_handle)
                except Exception as exc:
                    print_error(f"  get_identity failed: {exc}")
                    return None, "", False
            identity, did_provision_phone = _offer_phone_for_existing(client, identity)
            agent_key = _mint_agent_scoped_key(client, identity, InkboxAPIError)
            if agent_key is None:
                return None, "", False
            return identity, agent_key, did_provision_phone
    else:
        print_info("  No identities exist yet under this org. Let's create the first one.")

    identity, _, did_provision_phone = _create_identity(
        client,
        api_key,
        IdentityPhoneNumberCreateOptions,
        InkboxAPIError,
    )
    if identity is None:
        return None, "", False
    agent_key = _mint_agent_scoped_key(client, identity, InkboxAPIError)
    if agent_key is None:
        return None, "", False
    return identity, agent_key, did_provision_phone


def _create_identity(
    client: Any,
    api_key: str,
    IdentityPhoneNumberCreateOptions: Any,
    InkboxAPIError: Any,
) -> tuple[Any | None, str, bool]:
    del api_key
    try:
        from inkbox.identities.exceptions import HandleUnavailableError
    except Exception:  # pragma: no cover - old SDK fallback
        HandleUnavailableError = ()

    print()
    print_header("Create new agent identity")

    while True:
        handle = prompt(
            "  Agent handle (e.g. on-call-agent, recruiting-agent) - "
            "globally unique, also the mailbox local part"
        ).strip()
        if handle:
            break
        print_error("  Handle is required.")

    display_name = prompt("  Display name for the identity (shown to recipients, optional)").strip()

    print()
    print_info("Phone number - optional, but unlocks SMS and voice.")
    print_info("  We provision a local US number so SMS is supported.")
    create_phone = prompt_yes_no("  Provision a phone number for this agent?", True)

    phone_opts = None
    if create_phone:
        phone_opts = IdentityPhoneNumberCreateOptions(type="local", incoming_call_action="auto_reject")

    print()
    print_info("Creating identity...")
    while True:
        try:
            identity = client.create_identity(
                handle,
                display_name=display_name or None,
                phone_number=phone_opts,
            )
            break
        except HandleUnavailableError as exc:
            namespace = getattr(exc, "blocking_namespace", None) or "the global namespace"
            print_error(f"  Handle '{handle}' is unavailable in {namespace}.")
            handle = prompt("  Pick a different handle").strip()
            if not handle:
                return None, "", False
        except InkboxAPIError as exc:
            print_error(f"  Creation failed: HTTP {_error_status(exc)} {_error_detail(exc)}")
            return None, "", False
        except Exception as exc:
            print_error(f"  Creation failed: {exc}")
            return None, "", False

    print_success(f"  Created identity '{identity.agent_handle}'")
    did_provision_phone = create_phone and getattr(identity, "phone_number", None) is not None
    return identity, "", did_provision_phone


def _offer_phone_for_existing(client: Any, identity: Any) -> tuple[Any, bool]:
    if getattr(identity, "phone_number", None) is not None:
        return identity, False

    print()
    print_info("  This agent has no phone number attached.")
    print_info("  A local US number unlocks SMS and voice for this agent.")
    if not prompt_yes_no("  Provision a local phone number now?", True):
        return identity, False

    try:
        provisioned = client.phone_numbers.provision(agent_handle=identity.agent_handle, type="local")
        print_success(f"  Provisioned: {provisioned.number}")
    except Exception as exc:
        print_warning(f"  Phone provisioning failed: {exc}")
        print_info("  You can provision a number later in the Inkbox console.")
        return identity, False

    try:
        return client.get_identity(identity.agent_handle), True
    except Exception:
        class PhoneShim:
            def __init__(self, phone: Any):
                self.number = phone.number
                self.type = getattr(phone, "type", "local")
                self.sms_status = getattr(phone, "sms_status", None)
                self.id = getattr(phone, "id", None)

        identity.phone_number = PhoneShim(provisioned)
        return identity, True


def _print_agent_summary(identity: Any) -> None:
    print()
    print(color("Inkbox configured", Colors.GREEN, Colors.BOLD))
    print()
    print(color(f"  Handle:   {identity.agent_handle}", Colors.GREEN, Colors.BOLD))

    mailbox = getattr(identity, "mailbox", None)
    email = getattr(identity, "email_address", None) or (getattr(mailbox, "email_address", None) if mailbox else None)
    if email:
        print(color(f"  Mailbox:  {email}", Colors.GREEN, Colors.BOLD))
    else:
        print_info("  Mailbox:  (none - set up later in the Inkbox console)")

    phone = getattr(identity, "phone_number", None)
    if phone is not None:
        sms_status = getattr(phone, "sms_status", None)
        sms_value = getattr(sms_status, "value", sms_status)
        sms_str = f" - SMS: {sms_value}" if sms_value else ""
        print(color(f"  Phone:    {phone.number} ({phone.type}){sms_str}", Colors.GREEN, Colors.BOLD))
        if sms_value == "pending":
            print_info("            (carrier propagation can take a few minutes)")
    else:
        print_info("  Phone:    (none - provision later in the Inkbox console)")

    print()
    print_info("  Wrote INKBOX_API_KEY and INKBOX_IDENTITY to .env.")
    print_info("  Start the gateway with: hermes gateway run")

    if phone is not None and getattr(phone, "type", None) == "local":
        print()
        print(color("  --- SMS opt-in ---", Colors.YELLOW))
        print_info(f"  Text START to {phone.number} to enable SMS from this agent")
        print_info("  to your phone. Do this from every phone you want to message it from.")

        qr_payload = f"SMSTO:{phone.number}:START"
        sms_link = f"sms:{phone.number}?&body=START"
        print()
        print_info("  Or just scan this with your phone camera to draft that text in one tap:")
        print()
        if not _show_qr(qr_payload):
            print_info(f"    (install 'segno' to show a scannable QR here: {sms_link})")

    print()
    print(color("  --- Reachability rules ---", Colors.CYAN))
    print_info("  Open the Inkbox console to control who can reach this agent:")
    print_info("    https://inkbox.ai/console/contact-rules")
    print_info("  You can allow or block specific contacts, phone numbers, email addresses, and domains.")


def interactive_setup() -> None:
    print_header("Inkbox")
    print_info("API-first email + SMS + voice + identity for AI agents.")
    print_info("Inkbox is the recommended way to give your Hermes agent")
    print_info("its own real mailbox, phone number, and contact graph.")

    symbols = _ensure_inkbox_sdk()
    if symbols is None:
        return

    Inkbox = symbols["Inkbox"]
    InkboxAPIError = symbols["InkboxAPIError"]
    IdentityPhoneNumberCreateOptions = symbols["IdentityPhoneNumberCreateOptions"]
    WhoamiApiKeyResponse = symbols["WhoamiApiKeyResponse"]
    ADMIN_SCOPED = symbols["ADMIN_SCOPED"]
    AGENT_CLAIMED = symbols["AGENT_CLAIMED"]
    AGENT_UNCLAIMED = symbols["AGENT_UNCLAIMED"]

    existing_key = _env("INKBOX_API_KEY")
    existing_identity = _env("INKBOX_IDENTITY")
    if existing_key and existing_identity:
        print()
        print_success(f"Inkbox is already configured for identity '{existing_identity}'.")
        if not prompt_yes_no("  Reconfigure Inkbox?", False):
            return

    base_url = os.getenv("INKBOX_BASE_URL") or _env("INKBOX_BASE_URL") or INKBOX_BASE_URL_DEFAULT

    print()
    print_info("If you do not have an Inkbox API key yet, that is fine.")
    print_info("We can create a fresh agent identity for you via self-signup.")
    has_key = prompt_yes_no("  Do you already have an Inkbox API key?", False)

    if not has_key:
        identity, api_key, did_provision_phone = _self_signup_flow(base_url, Inkbox, InkboxAPIError)
        if identity is None:
            return
    else:
        identity, api_key, did_provision_phone = _api_key_flow(
            base_url,
            Inkbox,
            InkboxAPIError,
            WhoamiApiKeyResponse,
            ADMIN_SCOPED,
            AGENT_CLAIMED,
            AGENT_UNCLAIMED,
            IdentityPhoneNumberCreateOptions,
        )
        if identity is None:
            return

    _save("INKBOX_API_KEY", api_key)
    _save("INKBOX_IDENTITY", identity.agent_handle)
    if base_url != INKBOX_BASE_URL_DEFAULT or _env("INKBOX_BASE_URL"):
        _save("INKBOX_BASE_URL", base_url)

    _configure_avatar(base_url, api_key, identity, is_signup=not has_key)

    _save("INKBOX_ALLOW_ALL_USERS", "true")
    print()
    print_info("Inkbox authorization lives server-side via contact rules:")
    print_info("  https://console.inkbox.ai -> Mailboxes / Phone Numbers -> Contact Rules")
    print_info("Anyone Inkbox lets through reaches the agent. No second allowlist to maintain.")

    _seed_identity_state(identity)
    _print_agent_summary(identity)

    # Block on the START text right after the number + QR are shown, before
    # moving on to realtime — otherwise the "text START" prompt and its
    # blocking wait get split by the realtime questions and it looks skipped.
    if did_provision_phone:
        _wait_for_sms_opt_in(api_key, base_url, getattr(identity, "phone_number", None), Inkbox)

    _configure_realtime_calls(identity)

    _configure_imessage(api_key, base_url, identity.agent_handle, Inkbox)

    _setup_signing_key(api_key, base_url, Inkbox)

    print()
    print("Next steps:")
    print("  hermes inkbox doctor")
    print("  hermes gateway run")
