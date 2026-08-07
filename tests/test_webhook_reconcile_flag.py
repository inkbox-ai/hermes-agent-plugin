"""
tests/test_webhook_reconcile_flag.py

By default the adapter installs its own webhook subscriptions on connect, so
whatever URL it just came up on is the one that receives traffic.

Some deployments provision those subscriptions ahead of time instead. There the
destination is already fixed, and the agent's API key may not be permitted to
change it, so writing on every connect is at best redundant and at worst the
thing that stops the agent from starting. INKBOX_SKIP_WEBHOOK_RECONCILE turns
that write off while leaving the rest of connect untouched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapter import InkboxAdapter  # noqa: E402


def _adapter(*, skip: bool):
    """An adapter with only the state _patch_identity_objects reads."""
    adapter = object.__new__(InkboxAdapter)
    adapter._skip_webhook_reconcile = skip
    adapter._public_url = "https://agent.example"
    adapter._webhook_path = "/webhooks/inkbox"
    adapter._public_host = "agent.example"
    adapter._ws_path = "/ws"
    adapter._identity_handle = "agent"

    # Reaching the SDK means the skip did not take effect.
    def _boom(*args, **kwargs):
        raise AssertionError("reconcile touched the API despite the skip flag")

    adapter._inkbox = type("_Inkbox", (), {"get_identity": staticmethod(_boom)})()
    return adapter


def _flag(raw: str | None) -> bool:
    """Resolve the flag exactly as the constructor does."""
    if raw is None:
        os.environ.pop("INKBOX_SKIP_WEBHOOK_RECONCILE", None)
    else:
        os.environ["INKBOX_SKIP_WEBHOOK_RECONCILE"] = raw
    value = os.getenv("INKBOX_SKIP_WEBHOOK_RECONCILE", "false")
    return str(value).lower() in ("true", "1", "yes")


def test_skipping_makes_no_api_calls() -> None:
    """The whole point: connect proceeds without writing subscriptions."""
    _adapter(skip=True)._patch_identity_objects()


def test_not_skipping_still_reconciles() -> None:
    """Default behavior is unchanged; the fixture asserts by exploding."""
    with pytest.raises(AssertionError):
        _adapter(skip=False)._patch_identity_objects()


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "YES"])
def test_truthy_spellings_enable_the_skip(raw: str) -> None:
    """Operators write this by hand, so accept the obvious spellings."""
    assert _flag(raw) is True


@pytest.mark.parametrize("raw", [None, "false", "False", "0", "no", ""])
def test_everything_else_leaves_reconcile_on(raw: str | None) -> None:
    """Unset or unrecognized must not silently disable subscription setup."""
    assert _flag(raw) is False


def teardown_module(module) -> None:
    """Leave the environment as it was found."""
    os.environ.pop("INKBOX_SKIP_WEBHOOK_RECONCILE", None)
