"""Native tool registration exercises real encrypted SDK Vault HTTP contracts."""
import json

import httpx
import pytest

from inkbox_plugin.extended_tools import dispatch
from tests.fixtures.vault_api import LOGIN_ID, TOKEN_ID, TOTP_SEED, VAULT_KEY, VaultAPI


@pytest.fixture
def vault(monkeypatch, tmp_path):
    api = VaultAPI()
    monkeypatch.setenv("INKBOX_API_KEY", "synthetic-agent-key")
    monkeypatch.setenv("INKBOX_IDENTITY", "example-agent")
    monkeypatch.setenv("INKBOX_BASE_URL", "https://api.example.com")
    monkeypatch.delenv("INKBOX_VAULT_KEY", raising=False)
    monkeypatch.delenv("INKBOX_HERMES_VAULT_KEY", raising=False)
    monkeypatch.setattr("inkbox._config._CONFIG_PATH", tmp_path / "missing-config")
    monkeypatch.setattr("inkbox._http.httpx.HTTPTransport", lambda **kw: httpx.MockTransport(api.respond))
    from inkbox_plugin.config import set_runtime_config_extra
    set_runtime_config_extra({})
    return api


def call(name, **args):
    return json.loads(dispatch(name, args))


@pytest.mark.parametrize("key", ["", "Wrong-example-key-42!"])
def test_metadata_without_unlock_or_payload(vault, monkeypatch, key):
    monkeypatch.setenv("INKBOX_HERMES_VAULT_KEY", key)
    result = call("inkbox_list_vault_secrets", secret_type="login")
    assert result["ok"]
    assert [item["id"] for item in result["result"]] == [LOGIN_ID]
    assert [request.url.path for request in vault.requests] == ["/api/v1/identities/example-agent", "/api/v1/vault/secrets"]
    assert vault.requests[-1].url.params["secret_type"] == "login"
    assert "payload" not in json.dumps(result)
    assert TOTP_SEED not in json.dumps(result)


@pytest.mark.parametrize("name", ["inkbox_get_vault_secret", "inkbox_get_totp_code"])
def test_locked_key_guidance_without_request(vault, name):
    result = call(name, secret_id=LOGIN_ID)
    assert "INKBOX_HERMES_VAULT_KEY" in result["error"]
    assert "restart" in result["error"]
    assert not vault.requests


def test_totp_real_decryption_rfc_code_expiry_and_fresh_read(vault, monkeypatch):
    monkeypatch.setenv("INKBOX_HERMES_VAULT_KEY", VAULT_KEY)
    monkeypatch.setattr("inkbox.vault.totp.time.time", lambda: 1111111109)
    result = call("inkbox_get_totp_code", secret_id=LOGIN_ID)
    assert result["result"] == {"code": "07081804", "period_start": 1111111080,
                                "period_end": 1111111110, "seconds_remaining": 1}
    for value in (VAULT_KEY, TOTP_SEED, "synthetic-password", "synthetic-api-token"):
        assert value not in json.dumps(result)
    monkeypatch.setattr("inkbox.vault.totp.time.time", lambda: 1111111111)
    result = call("inkbox_get_totp_code", secret_id=LOGIN_ID)
    assert result["result"]["code"] == "14050471"
    assert result["result"]["seconds_remaining"] == 29
    assert sum(request.url.path.endswith(f"/secrets/{LOGIN_ID}") for request in vault.requests) == 2


@pytest.mark.parametrize("secret_id", [LOGIN_ID, TOKEN_ID])
def test_decrypt_secret_without_seed(vault, monkeypatch, secret_id):
    monkeypatch.setenv("INKBOX_HERMES_VAULT_KEY", VAULT_KEY)
    result = call("inkbox_get_vault_secret", secret_id=secret_id)
    assert result["ok"] and result["result"]["id"] == secret_id
    assert result["result"]["payload"] == ({"username": "agent@example.com", "password": "synthetic-password", "email": None, "url": None, "notes": None} if secret_id == LOGIN_ID else {"api_key": "synthetic-api-token", "endpoint": None, "notes": None})
    if secret_id == LOGIN_ID:
        assert result["result"]["has_totp"] is True
    assert TOTP_SEED not in json.dumps(result)
    assert VAULT_KEY not in json.dumps(result)


@pytest.mark.parametrize("name", ["inkbox_get_vault_secret", "inkbox_get_totp_code"])
@pytest.mark.parametrize("failure", ["denied", "deleted", "wrong_key", "uninitialized", "timeout"])
def test_failures_no_credentials_and_fresh_access_check(vault, monkeypatch, name, failure):
    monkeypatch.setenv("INKBOX_HERMES_VAULT_KEY", VAULT_KEY)
    assert call(name, secret_id=LOGIN_ID)["ok"]
    if failure == "denied":
        vault.denied = True
    elif failure == "deleted":
        del vault.details[LOGIN_ID]
    elif failure == "wrong_key":
        monkeypatch.setenv("INKBOX_HERMES_VAULT_KEY", "Wrong-example-key-42!")
    elif failure == "uninitialized":
        vault.initialized = False
    else:
        vault.unlock_timeout = True
    result = call(name, secret_id=LOGIN_ID)
    assert "error" in result
    for value in (VAULT_KEY, TOTP_SEED, "synthetic-password", "synthetic-api-token", "Wrong-example-key-42!"):
        assert value not in json.dumps(result)
    assert "payload" not in result and "code" not in result
    # Bad decryption keys do not prevent metadata reads through a new client.
    assert call("inkbox_list_vault_secrets")["ok"]


@pytest.mark.parametrize("value", ["", "../keys", "not-a-uuid"])
def test_invalid_id_rejected_before_network(vault, value):
    assert "UUID" in call("inkbox_get_vault_secret", secret_id=value)["error"]
    assert not vault.requests


def test_plugin_key_does_not_reuse_sdk_global_unlock(vault, monkeypatch):
    monkeypatch.setenv("INKBOX_VAULT_KEY", VAULT_KEY)
    monkeypatch.setenv("INKBOX_HERMES_VAULT_KEY", "Wrong-example-key-42!")
    result = call("inkbox_get_vault_secret", secret_id=LOGIN_ID)
    assert "error" in result
    assert "synthetic-password" not in json.dumps(result)
    assert sum(request.url.path.endswith("/unlock") for request in vault.requests) == 2


@pytest.mark.parametrize("tool", ["inkbox_get_vault_secret", "inkbox_get_totp_code"])
def test_admin_key_cannot_read_another_identity_secret_or_stale_grant(vault, monkeypatch, tool):
    monkeypatch.setenv("INKBOX_HERMES_VAULT_KEY", VAULT_KEY)
    assert call(tool, secret_id=LOGIN_ID)["ok"]
    vault.granted.remove(LOGIN_ID)
    vault.requests.clear()
    result = call(tool, secret_id=LOGIN_ID)
    assert "error" in result
    assert [request.url.path for request in vault.requests] == ["/api/v1/identities/example-agent", f"/api/v1/vault/secrets/{LOGIN_ID}/access"]
    listed = call("inkbox_list_vault_secrets")
    assert [secret["id"] for secret in listed["result"]] == [TOKEN_ID]
    assert "synthetic-password" not in json.dumps(result)
