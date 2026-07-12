import logging
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod


def _record(message, *args, level=logging.WARNING):
    return logging.LogRecord(
        "inkbox.tunnels",
        level,
        __file__,
        1,
        message,
        args,
        None,
    )


def test_expected_intake_idle_cap_warning_is_filtered():
    record = _record(
        "/_system/intake slot=%d -> status=%s reason=%r body=%r",
        10,
        "408",
        "intake-idle-cap",
        b"",
    )

    assert adapter_mod._ExpectedTunnelIdleFilter().filter(record) is False


def test_other_tunnel_warnings_remain_visible():
    filter_ = adapter_mod._ExpectedTunnelIdleFilter()

    assert filter_.filter(_record(
        "/_system/intake slot=%d -> status=%s reason=%r body=%r",
        10,
        "401",
        "owner-token-invalid",
        b"",
    )) is True
    assert filter_.filter(_record("tunnel runtime disconnected")) is True


def test_installing_filter_is_idempotent():
    logger = logging.getLogger("inkbox.tunnels")
    original_filters = list(logger.filters)
    try:
        logger.filters = [
            item for item in logger.filters
            if not isinstance(item, adapter_mod._ExpectedTunnelIdleFilter)
        ]

        adapter_mod._install_tunnel_log_filter()
        adapter_mod._install_tunnel_log_filter()

        installed = [
            item for item in logger.filters
            if isinstance(item, adapter_mod._ExpectedTunnelIdleFilter)
        ]
        assert len(installed) == 1
    finally:
        logger.filters = original_filters
