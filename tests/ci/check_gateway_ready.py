"""Require the current Hermes process to have published a live Inkbox adapter."""

from __future__ import annotations

import sys


def gateway_ready(expected_pid: int) -> bool:
    from gateway.status import (
        read_runtime_status,
        runtime_status_is_stale,
        runtime_status_pid_is_live,
    )

    state = read_runtime_status()
    if (
        not state
        or state.get("pid") != expected_pid
        or state.get("gateway_state") != "running"
        or runtime_status_is_stale(state)
        or not runtime_status_pid_is_live(state)
    ):
        return False
    platform = (state.get("platforms") or {}).get("inkbox") or {}
    return (
        platform.get("state") == "connected"
        and platform.get("writer_pid") == state["pid"]
        and platform.get("writer_start_time") == state.get("start_time")
    )


if __name__ == "__main__":
    raise SystemExit(0 if gateway_ready(int(sys.argv[1])) else 1)
