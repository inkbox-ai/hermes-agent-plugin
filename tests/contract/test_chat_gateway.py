"""Chat interactions cross the real native HTTP and SSE boundary."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("hermes_cli.plugins")


def test_native_chat_approval_and_questions():
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "PYTHONPATH")}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("chat_gateway_child.py"))],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]
