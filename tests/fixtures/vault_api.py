"""Synthetic encrypted Vault responses for SDK and host contract tests."""

import httpx
from inkbox.vault.crypto import (
    encrypt_payload,
    generate_org_encryption_key,
    generate_vault_key_material,
)


LOGIN_ID = "00000000-0000-4000-8000-000000000001"
TOKEN_ID = "00000000-0000-4000-8000-000000000002"
VAULT_KEY = "Example-vault-key-42!"
TOTP_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 SHA-1 test vector.


class VaultAPI:
    def __init__(self):
        self.org_id = "00000000-0000-4000-8000-000000000003"
        self.org_key = generate_org_encryption_key()
        self.key = generate_vault_key_material(VAULT_KEY, self.org_id, self.org_key)
        self.requests = []
        self.denied = False
        self.initialized = True
        self.unlock_timeout = False
        self.details = {}
        self.set_secret(LOGIN_ID, "login", {
            "username": "agent@example.com", "password": "synthetic-password",
            "totp": {"secret": TOTP_SEED, "digits": 8},
        })
        self.set_secret(TOKEN_ID, "api_key", {"api_key": "synthetic-api-token"})

    def set_secret(self, secret_id, secret_type, payload):
        self.details[secret_id] = {
            "id": secret_id, "name": f"Example {secret_type}",
            "secret_type": secret_type, "description": "Test credential",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "encrypted_payload": encrypt_payload(self.org_key, payload, secret_id=secret_id),
        }

    def respond(self, request):
        self.requests.append(request)
        assert request.method == "GET"
        assert request.headers["X-API-Key"] == "synthetic-agent-key"
        if request.url.path == "/api/v1/contacts":
            return httpx.Response(200, json=[])
        path = request.url.path.removeprefix("/api/v1/vault")
        if path == "/info":
            if not self.initialized:
                return httpx.Response(404, json={"detail": "Vault not initialized"})
            return httpx.Response(200, json={
                "id": self.org_id, "organization_id": self.org_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "key_count": 1, "secret_count": len(self.details), "recovery_key_count": 0,
            })
        if path == "/unlock":
            if self.unlock_timeout:
                raise httpx.ReadTimeout("Vault request timed out", request=request)
            if request.url.params["auth_hash"] != self.key.auth_hash:
                return httpx.Response(200, json={"wrapped_org_encryption_key": None})
            return httpx.Response(200, json={
                "wrapped_org_encryption_key": self.key.wrapped_org_encryption_key,
                "encrypted_secrets": list(self.details.values()),
            })
        if path == "/keys":
            return httpx.Response(200, json=[{"id": str(self.key.id), "key_type": "primary"}])
        if path == "/secrets":
            secret_type = request.url.params.get("secret_type")
            return httpx.Response(200, json=[
                {k: v for k, v in secret.items() if k != "encrypted_payload"}
                for secret in self.details.values()
                if secret_type is None or secret["secret_type"] == secret_type
            ])
        if path.startswith("/secrets/"):
            if self.denied:
                return httpx.Response(403, json={"detail": "Secret access denied"})
            secret = self.details.get(path.removeprefix("/secrets/"))
            if secret is None:
                return httpx.Response(404, json={"detail": "Secret not found"})
            return httpx.Response(200, json=secret)
        raise AssertionError(f"Unexpected SDK request: {path}")
