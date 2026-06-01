"""Interactive setup wizard for the Inkbox Hermes plugin."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any

try:
    from .config import INKBOX_BASE_URL_DEFAULT, object_summary
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import INKBOX_BASE_URL_DEFAULT, object_summary


def _ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    text = f"{prompt}{suffix}: "
    try:
        value = getpass.getpass(text) if secret else input(text)
    except EOFError:
        return default
    value = value.strip()
    return value or default


def _confirm(prompt: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    value = _ask(f"{prompt} [{default_text}]").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "true", "1", "on"}


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
            "tunnel_public_host": getattr(tunnel, "public_host", None) if tunnel else None,
        }
        path = Path(get_hermes_home()) / "inkbox_identity_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, path)
    except Exception as exc:
        print(f"Warning: could not seed inkbox_identity_state.json: {exc}")


def _pick_identity(client: Any, current: str = "") -> str:
    if current:
        return current
    try:
        identities = list(client.list_identities())
    except Exception:
        identities = []
    if not identities:
        return _ask("Inkbox identity handle")
    print("\nAvailable Inkbox identities:")
    for idx, identity in enumerate(identities, start=1):
        handle = getattr(identity, "agent_handle", None) or getattr(identity, "handle", "")
        email = getattr(identity, "email_address", "") or ""
        print(f"  {idx}. {handle} {email}")
    raw = _ask("Choose identity number or enter a handle", "1")
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(identities):
            chosen = identities[idx - 1]
            return str(getattr(chosen, "agent_handle", None) or getattr(chosen, "handle", "")).strip()
    return raw.strip()


def _maybe_create_signing_key(api_key: str, base_url: str) -> str:
    existing = _env("INKBOX_SIGNING_KEY")
    if existing and not _confirm("Replace existing INKBOX_SIGNING_KEY?", False):
        return existing
    pasted = _ask("Paste Inkbox signing key, or leave blank to generate one", secret=True)
    if pasted:
        return pasted
    if not _confirm("Generate/rotate a new Inkbox signing key now?", True):
        _save("INKBOX_REQUIRE_SIGNATURE", "false")
        return ""
    try:
        from inkbox import Inkbox

        key = Inkbox(api_key=api_key, base_url=base_url).create_signing_key()
        signing = str(getattr(key, "signing_key", "") or "")
        if signing:
            return signing
        print("Signing-key response was missing signing_key; generate one in the Inkbox console.")
    except Exception as exc:
        print(f"Could not generate signing key: {exc}")
    _save("INKBOX_REQUIRE_SIGNATURE", "false")
    return ""


def interactive_setup() -> None:
    print("\nInkbox Hermes Plugin Setup")
    print("Configures email, SMS/MMS, voice, and the Inkbox tunnel for Hermes.\n")

    try:
        from inkbox import Inkbox
    except Exception as exc:
        print("The Python Inkbox SDK is not installed.")
        print("Install it in the Hermes environment with: pip install inkbox aiohttp")
        print(f"Import error: {exc}")
        return

    base_url = _ask("Inkbox base URL", _env("INKBOX_BASE_URL") or INKBOX_BASE_URL_DEFAULT)
    api_key = _ask("Inkbox API key", _env("INKBOX_API_KEY"), secret=True)
    if not api_key:
        print("INKBOX_API_KEY is required. Create one in the Inkbox console, then rerun setup.")
        return

    try:
        client = Inkbox(api_key=api_key, base_url=base_url)
        whoami = client.whoami()
        print("API key validated:")
        print(json.dumps(object_summary(whoami), indent=2))
    except Exception as exc:
        print(f"API key validation failed: {exc}")
        if not _confirm("Save it anyway?", False):
            return
        client = None

    identity_handle = _env("INKBOX_IDENTITY")
    if client is not None:
        identity_handle = _pick_identity(client, identity_handle)
    else:
        identity_handle = _ask("Inkbox identity handle", identity_handle)
    if not identity_handle:
        print("INKBOX_IDENTITY is required.")
        return

    identity = None
    if client is not None:
        try:
            identity = client.get_identity(identity_handle)
            print("Identity:")
            print(json.dumps(object_summary(identity), indent=2))
        except Exception as exc:
            print(f"Could not fetch identity {identity_handle}: {exc}")

    signing_key = _maybe_create_signing_key(api_key, base_url)
    public_url = _ask("Public Hermes URL (blank to use Inkbox tunnel)", _env("INKBOX_PUBLIC_URL"))
    tunnel_name = _ask("Inkbox tunnel name override (blank for identity handle)", _env("INKBOX_TUNNEL_NAME"))
    home_channel = _ask("Home channel/contact id for cron delivery (blank to skip)", _env("INKBOX_HOME_CHANNEL"))

    _save("INKBOX_API_KEY", api_key)
    _save("INKBOX_IDENTITY", identity_handle)
    _save("INKBOX_BASE_URL", base_url)
    if signing_key:
        _save("INKBOX_SIGNING_KEY", signing_key)
        _save("INKBOX_REQUIRE_SIGNATURE", "true")
    if public_url:
        _save("INKBOX_PUBLIC_URL", public_url)
    if tunnel_name:
        _save("INKBOX_TUNNEL_NAME", tunnel_name)
    if home_channel:
        _save("INKBOX_HOME_CHANNEL", home_channel)

    # Inkbox contact rules are the primary authorization layer. This keeps
    # Hermes from maintaining a redundant local allowlist by default.
    if not _env("INKBOX_ALLOWED_USERS"):
        _save("INKBOX_ALLOW_ALL_USERS", "true")

    if _confirm("Enable OpenAI Realtime voice when OPENAI_API_KEY is available?", True):
        _save("INKBOX_REALTIME_ENABLED", "auto")
        model = _ask("Realtime model", _env("INKBOX_REALTIME_MODEL") or "gpt-realtime-2")
        voice = _ask("Realtime voice", _env("INKBOX_REALTIME_VOICE") or "cedar")
        _save("INKBOX_REALTIME_MODEL", model)
        _save("INKBOX_REALTIME_VOICE", voice)
    else:
        _save("INKBOX_REALTIME_ENABLED", "false")

    if identity is not None:
        _seed_identity_state(identity)

    print("\nInkbox plugin configured.")
    print("Next steps:")
    print("  hermes plugins enable hermes-agent-plugin")
    print("  hermes gateway run")
    print("  hermes inkbox doctor")
