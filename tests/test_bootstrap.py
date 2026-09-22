from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import bootstrap as bootstrap_module
from inkbox_plugin import cli


class _Identity:
    def __init__(
        self,
        handle: str = "helper-agent",
        *,
        signing_configured: bool = False,
        voice_instructions: str | None = None,
    ):
        self.id = "identity-1"
        self.agent_handle = handle
        self.email_address = f"{handle}@example.com"
        self.mailbox = types.SimpleNamespace(email_address=self.email_address)
        self.phone_number = types.SimpleNamespace(number="+15551234567")
        self.tunnel = types.SimpleNamespace(public_host=f"{handle}.example.com")
        self.imessage_enabled = True
        self.imessage_number = None
        self.hosted = types.SimpleNamespace(
            voice="cedar",
            model="voice-model",
            instructions=voice_instructions,
            authority_mode="contact_scoped",
        )
        self.incoming = types.SimpleNamespace(
            incoming_call_action="auto_accept",
            client_websocket_url="wss://old.example/ws",
            incoming_call_webhook_url=None,
        )
        self.signing_configured = signing_configured
        self.hosted_updates = []
        self.incoming_updates = []
        self.signing_creations = 0

    def get_hosted_agent_config(self):
        return self.hosted

    def set_hosted_agent_config(self, **kwargs):
        self.hosted_updates.append(kwargs)
        self.hosted = types.SimpleNamespace(authority_mode="contact_scoped", **kwargs)

    def get_incoming_call_action(self):
        return self.incoming

    def set_incoming_call_action(self, **kwargs):
        self.incoming_updates.append(kwargs)
        self.incoming = types.SimpleNamespace(**kwargs)

    def get_signing_key_status(self):
        return types.SimpleNamespace(configured=self.signing_configured)

    def create_signing_key(self):
        self.signing_creations += 1
        self.signing_configured = True
        return types.SimpleNamespace(signing_key="signing-secret")


class _ApiKeys:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.minted.append(kwargs)
        return types.SimpleNamespace(api_key="agent-secret")


class _Client:
    def __init__(self, key: str, identity: _Identity):
        self.key = key
        self.identity = identity
        self.minted = []
        self.api_keys = _ApiKeys(self)
        self.imessages = types.SimpleNamespace(
            get_triage_number=lambda: types.SimpleNamespace(
                number="+15550001111",
                connect_command=f"connect @{identity.agent_handle}",
            ),
        )

    def whoami(self):
        subtype = "api_key.admin_scoped" if self.key == "admin-secret" else "api_key.agent_scoped.claimed"
        return types.SimpleNamespace(auth_type="api_key", auth_subtype=subtype)

    def list_identities(self):
        return [types.SimpleNamespace(agent_handle=self.identity.agent_handle)]

    def get_identity(self, handle):
        if handle != self.identity.agent_handle:
            raise RuntimeError("not found")
        return self.identity


def _install_fakes(monkeypatch, identity: _Identity):
    clients = {}
    saved = {}

    class FakeInkbox:
        def __new__(cls, *, api_key, **_kwargs):
            client = clients.setdefault(api_key, _Client(api_key, identity))
            return client

    monkeypatch.setattr(
        bootstrap_module,
        "_load_inkbox_symbols",
        lambda: {
            "Inkbox": FakeInkbox,
            "ADMIN_SCOPED": "api_key.admin_scoped",
            "AGENT_CLAIMED": "api_key.agent_scoped.claimed",
            "AGENT_UNCLAIMED": "api_key.agent_scoped.unclaimed",
        },
    )
    monkeypatch.setattr(bootstrap_module, "_save", lambda name, value: saved.__setitem__(name, value))
    monkeypatch.setattr(bootstrap_module, "_env", lambda name: saved.get(name, ""))
    monkeypatch.setattr(bootstrap_module, "_seed_identity_state", lambda _identity: None)
    return clients, saved


def test_bootstrap_mints_scoped_key_configures_voice_and_is_resumable(monkeypatch):
    identity = _Identity()
    clients, saved = _install_fakes(monkeypatch, identity)

    first = bootstrap_module.bootstrap(
        identity_handle="@helper-agent",
        api_key="admin-secret",
        voice_ai=True,
    )

    assert first["status"] == "configured"
    assert first["gateway_running"] is False
    assert clients["admin-secret"].minted[0]["scoped_identity_id"] == "identity-1"
    assert saved["INKBOX_API_KEY"] == "agent-secret"
    assert saved["INKBOX_IDENTITY"] == "helper-agent"
    assert saved["INKBOX_SIGNING_KEY"] == "signing-secret"
    assert saved["INKBOX_VOICE_STACK"] == "inkbox_voice_ai"
    assert "Shared iMessage: text 'connect @helper-agent' to +15550001111." in identity.hosted_updates[0]["instructions"]
    assert identity.incoming_updates == [
        {
            "incoming_call_action": "hosted_agent",
            "client_websocket_url": None,
            "incoming_call_webhook_url": None,
        }
    ]

    second = bootstrap_module.bootstrap(
        identity_handle="helper-agent",
        api_key="admin-secret",
        voice_ai=True,
    )

    assert second["status"] == "configured"
    assert "reused_saved_agent_key" in second["actions"]
    assert len(clients["admin-secret"].minted) == 1
    assert identity.signing_creations == 1
    assert len(identity.hosted_updates) == 1
    assert len(identity.incoming_updates) == 1


def test_bootstrap_requires_explicit_rotation_when_remote_key_is_not_local(monkeypatch):
    identity = _Identity(signing_configured=True)
    _install_fakes(monkeypatch, identity)

    result = bootstrap_module.bootstrap(
        identity_handle="helper-agent",
        api_key="agent-secret",
    )

    assert result["status"] == "requires_human"
    assert "--rotate-signing-key" in result["human_actions"][0]
    assert identity.signing_creations == 0


def test_bootstrap_does_not_reuse_another_identitys_local_signing_key(monkeypatch):
    identity = _Identity(signing_configured=True)
    _clients, saved = _install_fakes(monkeypatch, identity)
    saved.update(
        {
            "INKBOX_IDENTITY": "previous-agent",
            "INKBOX_SIGNING_KEY": "previous-signing-secret",
        }
    )

    result = bootstrap_module.bootstrap(
        identity_handle="helper-agent",
        api_key="agent-secret",
    )

    assert result["status"] == "requires_human"
    assert "reused_local_signing_key" not in result["actions"]
    assert identity.signing_creations == 0


def test_bootstrap_preserves_existing_voice_instructions(monkeypatch):
    identity = _Identity(voice_instructions="Keep these operator instructions.")
    _install_fakes(monkeypatch, identity)

    result = bootstrap_module.bootstrap(
        identity_handle="helper-agent",
        api_key="agent-secret",
        voice_ai=True,
    )

    assert result["status"] == "configured"
    assert identity.hosted_updates == []


def test_bootstrap_rotates_only_when_explicitly_requested(monkeypatch):
    identity = _Identity(signing_configured=True)
    _install_fakes(monkeypatch, identity)

    result = bootstrap_module.bootstrap(
        identity_handle="helper-agent",
        api_key="agent-secret",
        rotate_signing_key=True,
    )

    assert result["status"] == "configured"
    assert "rotated_signing_key" in result["actions"]
    assert identity.signing_creations == 1


def test_bootstrap_rejects_agent_key_for_another_identity(monkeypatch):
    identity = _Identity(handle="different-agent")
    _install_fakes(monkeypatch, identity)

    result = bootstrap_module.bootstrap(
        identity_handle="helper-agent",
        api_key="agent-secret",
    )

    assert result["status"] == "error"
    assert result["error"] == "The API key is not scoped to the requested identity."


def test_bootstrap_redacts_credentials_from_errors(monkeypatch):
    monkeypatch.setattr(
        bootstrap_module,
        "_load_inkbox_symbols",
        lambda: (_ for _ in ()).throw(RuntimeError("failed while using top-secret")),
    )

    result = bootstrap_module.bootstrap(
        identity_handle="helper-agent",
        api_key="top-secret",
    )

    assert result["status"] == "error"
    assert result["error"] == "failed while using [redacted]"


def test_cli_reads_bootstrap_key_from_stdin_and_never_prints_it(monkeypatch, capsys):
    parser = argparse.ArgumentParser()
    subparser = parser.add_subparsers(dest="command").add_parser("inkbox")
    cli.setup_argparse(subparser)
    args = parser.parse_args(
        [
            "inkbox",
            "bootstrap",
            "--identity",
            "helper-agent",
            "--api-key-stdin",
            "--voice-ai",
        ]
    )
    monkeypatch.setattr(cli.sys, "stdin", types.SimpleNamespace(read=lambda: "top-secret\n"))
    received = {}

    def fake_bootstrap(**kwargs):
        received.update(kwargs)
        return {"status": "configured", "identity": "helper-agent"}

    monkeypatch.setattr(cli, "bootstrap", fake_bootstrap)
    args.func(args)

    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "configured"
    assert received["api_key"] == "top-secret"
    assert "top-secret" not in output


def test_cli_exits_nonzero_for_incomplete_bootstrap(monkeypatch, capsys):
    args = types.SimpleNamespace(
        inkbox_command="bootstrap",
        api_key_stdin=False,
        identity="helper-agent",
        base_url="",
        voice_ai=False,
        voice_ai_instructions_file=None,
        rotate_signing_key=False,
        start_gateway=False,
    )
    monkeypatch.setenv("INKBOX_API_KEY", "top-secret")
    monkeypatch.setattr(cli, "bootstrap", lambda **_kwargs: {"status": "requires_human"})

    with pytest.raises(SystemExit) as exc:
        cli.handle_cli(args)

    assert exc.value.code == 2
    assert "top-secret" not in capsys.readouterr().out
