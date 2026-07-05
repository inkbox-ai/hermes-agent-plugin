"""Contract tests for the plugin's assumptions about the real Hermes host.

These run only when the real Hermes host is installed (CI installs
``hermes-agent@main``); they are skipped in the offline unit suite, which stubs
``gateway.*``. They turn silent host-interface drift — a renamed kwarg, a moved
symbol, a dropped field — into a red check instead of a production surprise.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

# Only meaningful against the real host. The offline unit suite stubs gateway.*
# but never hermes_cli, so importorskip on hermes_cli.plugins cleanly skips there.
pytest.importorskip("hermes_cli.plugins")


# Host symbols the plugin imports — see adapter.py, __init__.py, setup_wizard.py.
HOST_SYMBOLS = {
    "gateway.config": ["Platform", "PlatformConfig"],
    "gateway.platforms.base": ["BasePlatformAdapter", "MessageEvent", "MessageType", "SendResult"],
    "gateway.platforms.helpers": ["redact_phone"],
    "gateway.session": ["build_session_key"],
    # The send tools read the current turn's session env through this seam
    # (tools.py get_session_env import); the spin-off lineage layer rides it too.
    "gateway.session_context": ["get_session_env"],
    "hermes_cli.config": ["save_env_value", "get_env_value", "load_config"],
}

# The complete field set the plugin ever passes to an inbound/relay MessageEvent
# (see adapter.py). The spin-off lineage layer deliberately adds NOTHING to this
# — it reuses ``internal`` (relay wake) and ``channel_prompt`` (the injected
# "[Spawned thread]" brief) — so this set stays frozen. A red diff here means the
# feature reached for a new host field instead of an existing seam.
PLUGIN_MESSAGE_EVENT_FIELDS = frozenset(
    {
        "text",
        "message_type",
        "source",
        "raw_message",
        "message_id",
        "auto_skill",
        "channel_prompt",
        "media_urls",
        "media_types",
        "reply_to_message_id",
        "internal",
        "timestamp",
    }
)


@pytest.mark.parametrize("module, names", list(HOST_SYMBOLS.items()))
def test_host_symbols_importable(module, names):
    mod = importlib.import_module(module)
    missing = [n for n in names if not hasattr(mod, n)]
    assert not missing, f"{module} is missing {missing} — Hermes host interface drifted"


def test_register_platform_accepts_our_call():
    """The essentials stay explicit; everything else rides ``**kwargs``.

    The plugin passes ~20 kwargs to ``register_platform``; the host absorbs most
    via ``**kwargs``. Guard the two things that would actually break us: the core
    params disappearing, or ``**kwargs`` going away (which would TypeError our call).
    """
    from hermes_cli.plugins import PluginContext

    params = inspect.signature(PluginContext.register_platform).parameters
    for required in ("name", "label", "adapter_factory"):
        assert required in params, f"register_platform dropped explicit '{required}'"
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), (
        "register_platform no longer accepts **kwargs — our extra kwargs would TypeError"
    )


def test_message_event_accepts_plugin_fields():
    """Every field the plugin sets on an inbound MessageEvent (adapter.py)."""
    from gateway.platforms.base import MessageEvent, MessageType

    MessageEvent(
        text="hi",
        message_type=MessageType.TEXT,
        source=None,
        raw_message={},
        message_id="m1",
        auto_skill="inkbox:inkbox-troubleshooting",
        channel_prompt=None,
        media_urls=[],
        media_types=[],
        reply_to_message_id=None,
        internal=True,
        timestamp=1.0,
    )


def test_send_result_accepts_plugin_fields():
    """The kwargs the plugin passes when returning a SendResult."""
    from gateway.platforms.base import SendResult

    SendResult(success=True, message_id="m", error=None)


@pytest.mark.parametrize(
    "method",
    ["_acquire_platform_lock", "_release_platform_lock", "build_source", "handle_message"],
)
def test_base_adapter_methods_present(method):
    from gateway.platforms.base import BasePlatformAdapter

    assert hasattr(BasePlatformAdapter, method), (
        f"BasePlatformAdapter.{method} is missing — Hermes host interface drifted"
    )


def test_spinoff_relay_rides_existing_message_event():
    """The relay wake + bind brief ride existing MessageEvent fields, not new ones.

    The lineage layer wakes the parent thread by enqueuing a self-directed
    ``MessageEvent(internal=True, ...)`` (adapter.py ``relay_edge``) and injects
    the "[Spawned thread]" brief through the ephemeral ``channel_prompt`` seam on
    the child's inbound turn. This exercises exactly that combination against the
    real host so a dropped/renamed field surfaces here, not in production.
    """
    from gateway.platforms.base import MessageEvent, MessageType

    # The relay wake: internal self-turn carrying the distilled answer.
    MessageEvent(
        text="[relay] the answer is 42",
        message_type=MessageType.TEXT,
        source=None,
        message_id="relay:edge123",
        internal=True,
    )
    # The bind side: the spawn brief merged into channel_prompt on a real inbound.
    MessageEvent(
        text="hi",
        message_type=MessageType.TEXT,
        source=None,
        message_id="m1",
        channel_prompt="[Spawned thread] acting on behalf of A — ask B for X.",
        internal=False,
    )


def test_message_event_gains_no_new_plugin_field():
    """The plugin's MessageEvent field set is a frozen subset of the host's.

    The spin-off lineage layer must ride existing seams only: it introduces no
    new host symbol and no new MessageEvent field. We assert every field the
    plugin passes still exists on the real host constructor, and that the frozen
    plugin field set matches what the acceptance test above constructs — so any
    future edit that reaches for a brand-new host field trips this guard.
    """
    from gateway.platforms.base import MessageEvent

    host_params = set(inspect.signature(MessageEvent).parameters)
    unknown = PLUGIN_MESSAGE_EVENT_FIELDS - host_params
    assert not unknown, (
        f"MessageEvent no longer accepts plugin fields {sorted(unknown)} "
        "— Hermes host interface drifted"
    )
    # The two seams the lineage feature relies on must remain real fields, not
    # something the feature would have had to add to the host contract.
    assert {"internal", "channel_prompt"} <= PLUGIN_MESSAGE_EVENT_FIELDS <= host_params
