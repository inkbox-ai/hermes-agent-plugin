"""Send-intent spy, injected into the gateway process via PYTHONPATH.

When ``INKBOX_SPY_FILE`` is set, this patches the Inkbox SDK's outbound-send
methods to append one JSON line per call (method + kwargs) to that file, then
calls through to the real send. A live test reads the file to assert the agent
*intended* to reply on the right channel — robust even if real delivery is slow
— while the pass-through keeps the round-trip (actual delivery) observable too.

This module is named ``sitecustomize`` so Python imports it automatically at
interpreter startup when its directory is on ``PYTHONPATH``; the gateway needs
no awareness of it.
"""

from __future__ import annotations

import functools
import json
import os

_SPY_FILE = os.environ.get("INKBOX_SPY_FILE")

# (module path, class, method) for each outbound channel we want to observe.
# Patched best-effort: a target missing in this SDK version is simply skipped.
_TARGETS = [
    ("inkbox.mail.resources.messages", "MessagesResource", "send"),       # email
    ("inkbox.phone.resources.texts", "TextsResource", "send"),            # SMS
    ("inkbox.phone.resources.calls", "CallsResource", "place"),           # voice
    ("inkbox.imessage.resources.conversations", "ConversationsResource", "send"),  # iMessage
]


def _record(method: str, kwargs: dict) -> None:
    try:
        safe = {k: str(v)[:500] for k, v in kwargs.items()}
        with open(_SPY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"method": method, "kwargs": safe}) + "\n")
    except Exception:
        pass  # never let the spy break a real send


def _wrap(label: str, original):
    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        _record(label, kwargs)
        return original(self, *args, **kwargs)

    wrapper._inkbox_spied = True  # type: ignore[attr-defined]
    return wrapper


def _install() -> None:
    if not _SPY_FILE:
        return
    import importlib

    for module_path, class_name, method_name in _TARGETS:
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            original = getattr(cls, method_name)
        except Exception:
            continue
        if getattr(original, "_inkbox_spied", False):
            continue
        setattr(cls, method_name, _wrap(f"{class_name}.{method_name}", original))


_install()
