"""Interactive setup wizard for the Inkbox Hermes plugin."""

from __future__ import annotations

import getpass
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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


INKBOX_REQUIREMENTS = ("inkbox>=0.4.6", "aiohttp>=3.9")
_BRACKETED_PASTE_PATTERN = re.compile(r"\x1b\[\s*200~|\x1b\[\s*201~")


def print_header(title: str) -> None:
    print()
    print(color(f"* {title}", Colors.CYAN, Colors.BOLD))


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


def _install_command() -> list[str]:
    return [sys.executable, "-m", "pip", "install", *INKBOX_REQUIREMENTS]


def _install_command_text() -> str:
    return shlex.join(_install_command())


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


def _ensure_inkbox_sdk() -> dict[str, Any] | None:
    try:
        return _load_inkbox_symbols()
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

    try:
        subprocess.check_call(_install_command())
    except Exception as install_exc:
        print_error(f"Install failed: {install_exc}")
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


def _setup_signing_key(api_key: str, base_url: str, Inkbox: Any) -> None:
    print()
    print(color("  --- Webhook signing key ---", Colors.CYAN))
    print_info("  Inkbox signs outbound webhooks with an HMAC over the body.")
    print_info("  Without the matching key, the gateway cannot verify inbound Inkbox traffic.")

    has_key = prompt_yes_no("  Do you already have an Inkbox signing key?", False)
    if has_key:
        key = prompt("  Paste your signing key (starts with whsec_)", password=True).strip()
        if not key:
            print_warning("  No key entered; leaving signature verification off.")
            _save("INKBOX_REQUIRE_SIGNATURE", "false")
            return
        _save("INKBOX_SIGNING_KEY", key)
        _save("INKBOX_REQUIRE_SIGNATURE", "true")
        print_success("  Saved signing key. Signature verification enabled.")
        return

    print_info("  Minting a new key here rotates any existing key for your org.")
    print_info("  Any other gateway using the old key will fail verification until updated.")
    if not prompt_yes_no("  Generate a new signing key now?", True):
        print_info("  Skipping; gateway will accept unsigned webhooks.")
        print_info("  Generate later in the Inkbox console.")
        _save("INKBOX_REQUIRE_SIGNATURE", "false")
        return

    try:
        new_key = Inkbox(api_key=api_key, base_url=base_url).create_signing_key()
    except Exception as exc:
        print_error(f"  Failed to create signing key: {exc}")
        print_info("  Leaving signature verification off; retry later in the Inkbox console.")
        _save("INKBOX_REQUIRE_SIGNATURE", "false")
        return

    signing_key = str(getattr(new_key, "signing_key", "") or "")
    if not signing_key:
        print_error("  Signing-key response did not include signing_key.")
        _save("INKBOX_REQUIRE_SIGNATURE", "false")
        return
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

    _save("INKBOX_ALLOW_ALL_USERS", "true")
    print()
    print_info("Inkbox authorization lives server-side via contact rules:")
    print_info("  https://console.inkbox.ai -> Mailboxes / Phone Numbers -> Contact Rules")
    print_info("Anyone Inkbox lets through reaches the agent. No second allowlist to maintain.")

    _seed_identity_state(identity)
    _print_agent_summary(identity)

    if did_provision_phone:
        _wait_for_sms_opt_in(api_key, base_url, getattr(identity, "phone_number", None), Inkbox)

    _setup_signing_key(api_key, base_url, Inkbox)

    print()
    print("Next steps:")
    print("  hermes inkbox doctor")
    print("  hermes gateway run")
