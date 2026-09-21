"""Shared setup and runtime diagnostics for the Inkbox Hermes plugin."""

from __future__ import annotations

import importlib.metadata
import sys
from typing import Any

SETUP_COMMAND = "hermes inkbox setup"
SETUP_HINT = (
    "Run `hermes inkbox setup` to create or connect an Inkbox identity and "
    "write INKBOX_API_KEY, INKBOX_IDENTITY, and INKBOX_SIGNING_KEY."
)


def missing_config_message(name: str) -> str:
    return f"{name} is not set. {SETUP_HINT}"


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "code"):
        value: Any = getattr(exc, attr, None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def is_inkbox_auth_error(exc: BaseException) -> bool:
    status = _status_code(exc)
    if status in {401, 403}:
        return True
    message = str(exc).lower()
    return "401" in message or "403" in message or "unauthorized" in message or "forbidden" in message


def is_inkbox_identity_error(exc: BaseException) -> bool:
    status = _status_code(exc)
    if status == 404:
        return True
    message = str(exc).lower()
    return "404" in message or "not found" in message


def inkbox_api_error_message(exc: BaseException, action: str) -> str:
    if is_inkbox_auth_error(exc):
        return (
            f"Inkbox authentication failed while {action}: {exc}. "
            "Re-run `hermes inkbox setup` or set a valid INKBOX_API_KEY/INKBOX_IDENTITY pair."
        )
    if is_inkbox_identity_error(exc):
        return (
            f"Inkbox identity lookup failed while {action}: {exc}. "
            "Re-run `hermes inkbox setup` or check that INKBOX_IDENTITY belongs to the configured API key."
        )
    return f"{exc}. {SETUP_HINT}"


WINDOWS_TUNNEL_MIN_VERSION = "0.7.4"


def windows_tunnel_issue(public_url: str = "") -> str | None:
    """Check the local runtime without connecting or changing the identity."""
    if sys.platform != "win32" or public_url:
        return None
    try:
        installed = importlib.metadata.version("inkbox")
        try:
            from packaging.version import Version

            supported = Version(installed) >= Version(WINDOWS_TUNNEL_MIN_VERSION)
        except ImportError:
            supported = tuple(int(part) for part in installed.split(".")) >= (0, 7, 4)
        if supported:
            return None
    except (importlib.metadata.PackageNotFoundError, ValueError):
        installed = "unknown"
    return (
        f"Native Windows inbound delivery requires Inkbox SDK {WINDOWS_TUNNEL_MIN_VERSION} or newer "
        f"(installed: {installed}). Older SDKs can send messages but cannot open the built-in tunnel. "
        "Run `hermes inkbox setup` to upgrade the SDK in the Hermes Python environment, "
        "keep your existing identity and API key, then restart `hermes gateway run`. "
        "WSL is not required with the updated SDK."
    )
