"""Native ownership uses the connected SDK origin when no override was supplied."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from inkbox import Inkbox

from inkbox_plugin.config import inkbox_client_kwargs
from inkbox_plugin.native_envelope import identity_scope


@pytest.mark.parametrize("configured,environment,file_url,expected", [
    ("", None, None, "https://inkbox.ai"),
    ("", None, "https://file.example", "https://file.example"),
    ("", "https://env.example", "https://file.example", "https://env.example"),
    ("https://explicit.example/", "https://env.example", "https://file.example", "https://explicit.example/"),
])
def test_scope_follows_sdk_resolution_and_preserves_explicit_checkpoints(monkeypatch, tmp_path, configured, environment, file_url, expected):
    monkeypatch.delenv("INKBOX_VAULT_KEY", raising=False)
    monkeypatch.delenv("INKBOX_BASE_URL", raising=False)
    if environment:
        monkeypatch.setenv("INKBOX_BASE_URL", environment)
    config_file = tmp_path / "sdk-config"
    if file_url:
        config_file.write_text(f"base_url = {file_url}\n")
    monkeypatch.setattr("inkbox._config._CONFIG_PATH", config_file)
    with Inkbox(**inkbox_client_kwargs("synthetic-key", configured)) as client:
        adapter = SimpleNamespace(_base_url=configured, _inkbox=client,
                                  _identity_handle="sample-agent", _identity_id="sample-identity")
        expected_scope = hashlib.sha256(json.dumps([expected, "sample-agent", "sample-identity"]).encode()).hexdigest()
        assert identity_scope(adapter) == expected_scope
        monkeypatch.setenv("INKBOX_BASE_URL", "https://changed-after-connect.example")
        assert identity_scope(adapter) == expected_scope


def test_default_origin_does_not_establish_identity_without_a_connected_client():
    adapter = SimpleNamespace(_base_url="", _inkbox=None, _identity_handle="sample-agent", _identity_id="sample-identity")
    assert identity_scope(adapter) is None


def test_resolved_origin_cannot_replace_a_missing_identity():
    adapter = SimpleNamespace(_base_url="", _inkbox=SimpleNamespace(_base_url="https://example.com"),
                              _identity_handle="sample-agent", _identity_id=None)
    assert identity_scope(adapter) is None
