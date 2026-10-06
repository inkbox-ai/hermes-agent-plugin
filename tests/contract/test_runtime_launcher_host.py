"""Exercise arbitrary CI scripts through the real host's isolated bootstrap."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip('hermes_cli._launchers')
from hermes_cli import _launchers

ROOT = Path(__file__).resolve().parents[2]


def _installation(tmp_path):
    source = Path(_launchers.__file__).resolve().parents[1]
    runtime = tmp_path / 'home'
    root = runtime / 'hermes-agent'
    root.mkdir(parents=True)
    for entry in source.iterdir():
        if entry.name not in {'.git', '.hermes', 'venv', '.venv', '__pycache__'}:
            (root / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    output = root / '.hermes/bin'
    output.mkdir(parents=True)
    launcher = _launchers.mint_launcher('hermes', root, output, Path(sys.executable), None)
    assert launcher is not None
    return runtime, root, launcher


def test_native_launcher_exec_preserves_pid_argv_and_dependencies(tmp_path):
    runtime, _, _ = _installation(tmp_path)
    script = tmp_path / 'script with spaces.py'
    script.write_text('import json, os, sys, inkbox, gateway.status\n'
                      'print(json.dumps({"pid":os.getpid(), "argv":sys.argv, '
                      '"isolated":sys.flags.isolated, "bootstrap":"hermes_bootstrap" in sys.modules}))\n')
    child = subprocess.Popen(
        [sys.executable, str(ROOT / 'tests/ci/hermes_python.py'), str(script), 'literal $(not shell)'],
        env={**os.environ, 'HERMES_HOME': str(runtime)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout, stderr = child.communicate(timeout=30)
    assert child.returncode == 0, stderr
    proof = json.loads(stdout)
    assert proof == {'pid': child.pid, 'argv': [str(script), 'literal $(not shell)'], 'isolated': 1, 'bootstrap': True}


def test_foreign_published_launcher_fails_before_script(tmp_path):
    runtime, _, launcher = _installation(tmp_path)
    if os.name == 'nt':
        pytest.skip('POSIX wrapper tampering regression')
    launcher.write_text('#!/bin/sh\nprintf \'["foreign-runtime"]\\n\'\n')
    launcher.chmod(0o755)
    marker = tmp_path / 'must-not-run'
    script = tmp_path / 'script.py'
    script.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tests/ci/hermes_python.py'), str(script)],
        env={**os.environ, 'HERMES_HOME': str(runtime)}, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert 'published Hermes launcher does not match its installation' in result.stderr
    assert not marker.exists()
