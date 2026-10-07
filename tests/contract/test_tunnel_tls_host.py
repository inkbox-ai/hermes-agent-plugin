"""Native host policy and real SDK h2 transport with trusted and rejected TLS."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip('agent.ssl_verify')
ROOT = Path(__file__).resolve().parents[2]


def test_native_verifier_preserved_by_real_sdk_tunnel(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tests/ci/check_tunnel_tls.py')],
        env={**os.environ, 'TMPDIR': str(tmp_path)}, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert 'trusted h2 accepted' in result.stdout
    assert 'wrong-host rejected' in result.stdout
    assert 'untrusted rejected' in result.stdout
    assert 'original contexts, custom roots, SNI and h2 preserved' in result.stdout
