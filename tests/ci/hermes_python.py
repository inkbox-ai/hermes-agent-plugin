#!/usr/bin/env python3
"""Execute a CI script through this installation's native runtime bootstrap.

A managed Hermes install has no stable checkout venv. The published launcher
and its runtime_command factory own Python selection and generation leasing;
never persist a selected generation or install packages into store Python.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


_SCRIPT = """import runpy, sys
script = sys.argv.pop(1)
sys.argv[0] = script
sys.path.insert(0, str(__import__('pathlib').Path(script).resolve().parent))
runpy.run_path(script, run_name='__main__')
"""


def native_command(root: Path, args: list[str]) -> list[str]:
    if not args or args[0].startswith("-"):
        raise ValueError("a script entrypoint is required")
    root = root.resolve()
    launcher = root / ".hermes" / "bin" / "hermes"
    published = json.loads(subprocess.check_output(
        [str(launcher), "--print-runtime-command"], text=True,
    ))
    # This factory is part of the *same* source installation as the launcher.
    # Its code hook retains the native isolated bootstrap and fresh dependency
    # generation lease. Refuse a stale/foreign launcher instead of guessing.
    sys.path.insert(0, str(root))
    from hermes_cli._launchers import runtime_command
    if published != runtime_command(root):
        raise RuntimeError("published Hermes launcher does not match its installation")
    return runtime_command(root, args, code=_SCRIPT)


def main() -> None:
    root = Path(os.environ["HERMES_HOME"]) / "hermes-agent"
    command = native_command(root, sys.argv[1:])
    # Keep the same PID for native runtime status, readiness and signal cleanup.
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
