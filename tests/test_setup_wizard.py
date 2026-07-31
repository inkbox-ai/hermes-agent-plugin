import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import setup_wizard


def test_avatar_base_url_defaults_to_public_api():
    assert setup_wizard._avatar_base_url("") == "https://inkbox.ai"
    assert setup_wizard._avatar_base_url("https://proxy.example/") == "https://proxy.example"


def test_install_command_prefers_uv_when_available(monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "executable", "/tmp/hermes/venv/bin/python")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)

    assert setup_wizard._install_commands()[0] == [[
        "/bin/uv",
        "pip",
        "install",
        "--python",
        "/tmp/hermes/venv/bin/python",
        "inkbox>=0.5.8,<1.0.0",
        "aiohttp>=3.9",
        "segno>=1.5",
    ]]


def test_install_command_falls_back_to_pip_and_ensurepip(monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "executable", "/tmp/hermes/venv/bin/python")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _name: None)

    assert setup_wizard._install_commands() == [
        [["/tmp/hermes/venv/bin/python", "-m", "pip", "install", "inkbox>=0.5.8,<1.0.0", "aiohttp>=3.9", "segno>=1.5"]],
        [
            ["/tmp/hermes/venv/bin/python", "-m", "ensurepip", "--upgrade"],
            ["/tmp/hermes/venv/bin/python", "-m", "pip", "install", "inkbox>=0.5.8,<1.0.0", "aiohttp>=3.9", "segno>=1.5"],
        ],
    ]


def test_missing_sdk_guidance_prints_hermes_python(monkeypatch, capsys):
    def fail_import():
        raise ImportError("No module named 'inkbox'")

    monkeypatch.setattr(setup_wizard, "_load_inkbox_symbols", fail_import)
    monkeypatch.setattr(setup_wizard, "_is_interactive_stdin", lambda: False)
    monkeypatch.setattr(setup_wizard.sys, "executable", "/tmp/hermes/venv/bin/python")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)

    assert setup_wizard._ensure_inkbox_sdk() is None

    out = capsys.readouterr().out
    assert "/tmp/hermes/venv/bin/python" in out
    assert "uv pip install --python" in out
    assert "inkbox>=0.5.8,<1.0.0" in out
    assert "aiohttp>=3.9" in out


def test_api_key_flow_rejects_unknown_auth_subtype(monkeypatch, capsys):
    class FakeWhoamiApiKeyResponse:
        auth_subtype = "future_scope"
        organization_id = "org_123"

    class FakeInkbox:
        def __init__(self, **_kwargs):
            pass

        def whoami(self):
            return FakeWhoamiApiKeyResponse()

        def list_identities(self):
            raise AssertionError("unknown subtypes must not fall back to identity listing")

    monkeypatch.setattr(setup_wizard, "prompt", lambda *_args, **_kwargs: "ApiKey_test")

    result = setup_wizard._api_key_flow(
        "https://inkbox.ai",
        FakeInkbox,
        Exception,
        FakeWhoamiApiKeyResponse,
        "admin_scoped",
        "agent_scoped_claimed",
        "agent_scoped_unclaimed",
        object,
    )

    assert result == (None, "", False)
    assert "Unsupported API-key subtype" in capsys.readouterr().out


def test_admin_api_key_flow_selects_existing_identity_and_mints_agent_key(monkeypatch):
    class FakeWhoamiApiKeyResponse:
        auth_subtype = "admin_scoped"
        organization_id = "org_123"

    class FakeApiKeys:
        def __init__(self):
            self.created = []

        def create(self, **kwargs):
            self.created.append(kwargs)
            return types.SimpleNamespace(api_key="ApiKey_agent_selected")

    class FakeInkbox:
        instance = None

        def __init__(self, **_kwargs):
            self.api_keys = FakeApiKeys()
            self.phone_numbers = types.SimpleNamespace()
            self.identities = [
                types.SimpleNamespace(agent_handle="first-agent", email_address=None),
                types.SimpleNamespace(agent_handle="selected-agent", email_address=None),
            ]
            self.details = {
                "first-agent": types.SimpleNamespace(
                    id="identity-1",
                    agent_handle="first-agent",
                    email_address="first@example.com",
                    phone_number=types.SimpleNamespace(number="+15550000001", type="local"),
                ),
                "selected-agent": types.SimpleNamespace(
                    id="identity-2",
                    agent_handle="selected-agent",
                    email_address="selected@example.com",
                    phone_number=types.SimpleNamespace(number="+15550000002", type="local"),
                ),
            }
            FakeInkbox.instance = self

        def whoami(self):
            return FakeWhoamiApiKeyResponse()

        def list_identities(self):
            return self.identities

        def get_identity(self, handle):
            return self.details[handle]

    monkeypatch.setattr(setup_wizard, "prompt", lambda *_args, **_kwargs: "ApiKey_admin")
    monkeypatch.setattr(setup_wizard, "prompt_choice", lambda *_args, **_kwargs: 1)

    identity, agent_key, did_provision_phone = setup_wizard._api_key_flow(
        "https://inkbox.ai",
        FakeInkbox,
        Exception,
        FakeWhoamiApiKeyResponse,
        "admin_scoped",
        "agent_scoped_claimed",
        "agent_scoped_unclaimed",
        object,
    )

    assert identity.agent_handle == "selected-agent"
    assert agent_key == "ApiKey_agent_selected"
    assert did_provision_phone is False
    assert FakeInkbox.instance.api_keys.created == [
        {
            "label": "Hermes gateway - selected-agent",
            "description": (
                "Auto-minted by hermes inkbox setup. Scoped to one agent "
                "identity so the gateway never stores the admin key."
            ),
            "scoped_identity_id": "identity-2",
        }
    ]


def test_admin_api_key_flow_can_create_identity_and_mint_agent_key(monkeypatch):
    class FakeWhoamiApiKeyResponse:
        auth_subtype = "admin_scoped"
        organization_id = "org_123"

    class FakeApiKeys:
        def __init__(self):
            self.created = []

        def create(self, **kwargs):
            self.created.append(kwargs)
            return types.SimpleNamespace(api_key="ApiKey_agent_new")

    class FakeInkbox:
        instance = None

        def __init__(self, **_kwargs):
            self.api_keys = FakeApiKeys()
            self.phone_numbers = types.SimpleNamespace()
            self.created_identities = []
            FakeInkbox.instance = self

        def whoami(self):
            return FakeWhoamiApiKeyResponse()

        def list_identities(self):
            return []

        def create_identity(self, handle, **kwargs):
            self.created_identities.append((handle, kwargs))
            return types.SimpleNamespace(
                id="identity-new",
                agent_handle=handle,
                email_address=f"{handle}@example.com",
                phone_number=None,
            )

    answers = iter(["ApiKey_admin", "new-agent", "New Agent"])
    monkeypatch.setattr(setup_wizard, "prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: False)

    identity, agent_key, did_provision_phone = setup_wizard._api_key_flow(
        "https://inkbox.ai",
        FakeInkbox,
        Exception,
        FakeWhoamiApiKeyResponse,
        "admin_scoped",
        "agent_scoped_claimed",
        "agent_scoped_unclaimed",
        object,
    )

    assert identity.agent_handle == "new-agent"
    assert agent_key == "ApiKey_agent_new"
    assert did_provision_phone is False
    assert FakeInkbox.instance.created_identities == [
        ("new-agent", {"display_name": "New Agent", "phone_number": None})
    ]
    assert FakeInkbox.instance.api_keys.created == [
        {
            "label": "Hermes gateway - new-agent",
            "description": (
                "Auto-minted by hermes inkbox setup. Scoped to one agent "
                "identity so the gateway never stores the admin key."
            ),
            "scoped_identity_id": "identity-new",
        }
    ]


def test_plugin_install_does_not_prompt_for_inkbox_env():
    text = (ROOT / "plugin.yaml").read_text()

    assert "requires_env: []" in text
    assert "name: INKBOX_API_KEY" in text
    assert "name: INKBOX_IDENTITY" in text


def test_detect_openai_realtime_key_prefers_plugin_specific_env(monkeypatch):
    values = {
        "OPENAI_API_KEY": "sk-openai",
        "INKBOX_REALTIME_API_KEY": "sk-realtime",
    }
    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_hermes_openai_api_key", lambda: ("credential_pool:openai-api", "sk-pool"))
    monkeypatch.setattr(setup_wizard, "_env", lambda name: values.get(name, ""))

    assert setup_wizard._detect_openai_realtime_key() == ("INKBOX_REALTIME_API_KEY", "sk-realtime")


def test_detect_openai_realtime_key_prefers_config(monkeypatch):
    values = {
        "OPENAI_API_KEY": "sk-openai",
        "INKBOX_REALTIME_API_KEY": "sk-realtime",
    }
    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "sk-config")
    monkeypatch.setattr(setup_wizard, "_hermes_openai_api_key", lambda: ("credential_pool:openai-api", "sk-pool"))
    monkeypatch.setattr(setup_wizard, "_env", lambda name: values.get(name, ""))

    assert setup_wizard._detect_openai_realtime_key() == (
        "platforms.inkbox.realtime.api_key",
        "sk-config",
    )


def test_detect_openai_realtime_key_uses_hermes_openai_api_credentials(monkeypatch):
    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_hermes_openai_api_key", lambda: ("credential_pool:openai-api", "sk-pool"))
    monkeypatch.setattr(setup_wizard, "_env", lambda _name: "")

    assert setup_wizard._detect_openai_realtime_key() == ("credential_pool:openai-api", "sk-pool")


def test_configure_realtime_calls_existing_key_success(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []
    tested = []

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_hermes_openai_api_key", lambda: None)
    monkeypatch.setattr(setup_wizard, "_env", lambda name: "sk-existing" if name == "OPENAI_API_KEY" else "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(
        setup_wizard,
        "_test_openai_realtime_api_key",
        lambda key, model: tested.append((key, model)) or (True, "ok"),
    )

    setup_wizard._configure_realtime_calls(identity)

    assert tested == [("sk-existing", "gpt-realtime-2")]
    assert ("INKBOX_REALTIME_ENABLED", "true") in saved
    assert ("INKBOX_REALTIME_MODEL", "gpt-realtime-2") in saved
    assert ("INKBOX_REALTIME_API_KEY", "sk-existing") in saved


def test_configure_realtime_calls_reuses_hermes_openai_api_credentials(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []
    tested = []

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_hermes_openai_api_key", lambda: ("credential_pool:openai-api", "sk-pool"))
    monkeypatch.setattr(setup_wizard, "_env", lambda _name: "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        setup_wizard,
        "prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("prompted for key")),
    )
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(
        setup_wizard,
        "_test_openai_realtime_api_key",
        lambda key, model: tested.append((key, model)) or (True, "ok"),
    )

    setup_wizard._configure_realtime_calls(identity)

    assert tested == [("sk-pool", "gpt-realtime-2")]
    assert ("INKBOX_REALTIME_ENABLED", "true") in saved
    assert ("INKBOX_REALTIME_API_KEY", "sk-pool") in saved


def test_configure_realtime_calls_prompts_for_missing_key(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_hermes_openai_api_key", lambda: None)
    monkeypatch.setattr(setup_wizard, "_env", lambda _name: "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "prompt", lambda *_args, **_kwargs: "sk-pasted")
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(setup_wizard, "_test_openai_realtime_api_key", lambda *_args: (True, "ok"))

    setup_wizard._configure_realtime_calls(identity)

    assert ("INKBOX_REALTIME_ENABLED", "true") in saved
    assert ("INKBOX_REALTIME_API_KEY", "sk-pasted") in saved


def test_configure_realtime_calls_validation_failure_disables(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []
    answers = iter([True, False])

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_hermes_openai_api_key", lambda: None)
    monkeypatch.setattr(setup_wizard, "_env", lambda name: "sk-bad" if name == "OPENAI_API_KEY" else "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(
        setup_wizard,
        "_test_openai_realtime_api_key",
        lambda *_args: (False, "OpenAI rejected the key or Realtime permission: HTTP 403"),
    )

    setup_wizard._configure_realtime_calls(identity)

    assert saved == [
        ("INKBOX_REALTIME_ENABLED", "false"),
        ("INKBOX_REALTIME_ENABLED", "false"),
    ]


def test_configure_realtime_calls_retries_after_validation_failure(monkeypatch):
    identity = types.SimpleNamespace(phone_number=types.SimpleNamespace(number="+15551234567"))
    saved = []
    tested = []
    answers = iter([True, True])
    keys = iter(["sk-bad", "sk-good"])

    def test_key(key, model):
        tested.append((key, model))
        if key == "sk-good":
            return True, "ok"
        return False, "invalid_api_key: Incorrect API key provided"

    monkeypatch.setattr(setup_wizard, "_config_realtime_api_key", lambda: "")
    monkeypatch.setattr(setup_wizard, "_hermes_openai_api_key", lambda: None)
    monkeypatch.setattr(setup_wizard, "_env", lambda _name: "")
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(setup_wizard, "prompt", lambda *_args, **_kwargs: next(keys))
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(setup_wizard, "_test_openai_realtime_api_key", test_key)

    setup_wizard._configure_realtime_calls(identity)

    assert tested == [
        ("sk-bad", "gpt-realtime-2"),
        ("sk-good", "gpt-realtime-2"),
    ]
    assert saved == [
        ("INKBOX_REALTIME_ENABLED", "false"),
        ("INKBOX_REALTIME_ENABLED", "true"),
        ("INKBOX_REALTIME_MODEL", "gpt-realtime-2"),
        ("INKBOX_REALTIME_API_KEY", "sk-good"),
    ]


def test_configure_realtime_calls_without_phone_skips(monkeypatch):
    saved = []
    monkeypatch.setattr(setup_wizard, "_save", lambda name, value: saved.append((name, value)))

    setup_wizard._configure_realtime_calls(types.SimpleNamespace(phone_number=None))

    assert saved == []


class _FakeIMessageIdentity:
    def __init__(self, enabled=False):
        self.imessage_enabled = enabled
        self.updates = []
        self.sent = []
        self.marked_read = []
        self._inbox = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        if "imessage_enabled" in kwargs:
            self.imessage_enabled = kwargs["imessage_enabled"]
        return self

    def list_imessages(self, **_kwargs):
        return list(self._inbox)

    def send_imessage(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(id="im-1")

    def mark_imessage_conversation_read(self, conversation_id):
        self.marked_read.append(conversation_id)


class _FakeIMessageClient:
    def __init__(self, identity):
        self._identity = identity
        self.imessages = types.SimpleNamespace(
            get_triage_number=lambda: types.SimpleNamespace(
                number="+15550009999",
                connect_command="connect @agent",
            ),
        )

    def get_identity(self, _handle):
        return self._identity


def test_configure_imessage_enables_and_offers_connect(monkeypatch):
    identity = _FakeIMessageIdentity(enabled=False)
    client = _FakeIMessageClient(identity)
    walked = []

    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        setup_wizard,
        "_wait_for_imessage_first_message",
        lambda _client, _identity, handle: walked.append(handle),
    )

    setup_wizard._configure_imessage(
        "ApiKey_test", "https://inkbox.ai", "agent", lambda **_kwargs: client,
    )

    assert identity.updates == [{"imessage_enabled": True}]
    assert walked == ["agent"]


def test_configure_imessage_declined_leaves_identity_untouched(monkeypatch):
    identity = _FakeIMessageIdentity(enabled=False)
    client = _FakeIMessageClient(identity)

    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        setup_wizard,
        "_wait_for_imessage_first_message",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not walk through connect")),
    )

    setup_wizard._configure_imessage(
        "ApiKey_test", "https://inkbox.ai", "agent", lambda **_kwargs: client,
    )

    assert identity.updates == []


def test_wait_for_imessage_first_message_greets_back(monkeypatch):
    from datetime import datetime, timedelta, timezone

    identity = _FakeIMessageIdentity(enabled=True)
    client = _FakeIMessageClient(identity)
    identity._inbox = [
        types.SimpleNamespace(
            id="im-old",
            direction="inbound",
            conversation_id="imconv-old",
            remote_number="+15555550101",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ),
        types.SimpleNamespace(
            id="im-new",
            direction="inbound",
            conversation_id="imconv-123",
            remote_number="+15555550101",
            created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        ),
    ]

    monkeypatch.setattr(setup_wizard.time, "sleep", lambda _s: None)
    monkeypatch.setattr(setup_wizard, "_show_qr", lambda _data: True)

    setup_wizard._wait_for_imessage_first_message(client, identity, "agent")

    assert len(identity.sent) == 1
    assert identity.sent[0]["conversation_id"] == "imconv-123"
    assert "@agent" in identity.sent[0]["text"]
    assert identity.marked_read == ["imconv-123"]


def test_sms_opt_in_qr_uses_smsto_scheme(monkeypatch):
    identity = types.SimpleNamespace(
        agent_handle="agent",
        email_address="agent@inkbox.ai",
        mailbox=None,
        phone_number=types.SimpleNamespace(
            number="+16614031457",
            type="local",
            sms_status=None,
        ),
    )

    captured = {}
    monkeypatch.setattr(
        setup_wizard,
        "_show_qr",
        lambda data: captured.update(payload=data) or True,
    )

    setup_wizard._print_agent_summary(identity)

    assert captured["payload"] == "SMSTO:+16614031457:START"


def test_connect_qr_uses_smsto_scheme(monkeypatch):
    from datetime import datetime, timedelta, timezone

    identity = _FakeIMessageIdentity(enabled=True)
    client = _FakeIMessageClient(identity)
    identity._inbox = [
        types.SimpleNamespace(
            id="im-1",
            direction="inbound",
            conversation_id="imconv-1",
            remote_number="+15555550101",
            created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        ),
    ]

    captured = {}
    monkeypatch.setattr(
        setup_wizard,
        "_show_qr",
        lambda data: captured.update(payload=data) or True,
    )
    monkeypatch.setattr(setup_wizard.time, "sleep", lambda _s: None)

    setup_wizard._wait_for_imessage_first_message(client, identity, "agent")

    assert captured["payload"] == "SMSTO:+15550009999:connect @agent"


def test_configure_imessage_already_connected_defaults_to_skip(monkeypatch):
    identity = _FakeIMessageIdentity(enabled=True)
    identity.list_imessage_assignments = lambda **_kwargs: [
        types.SimpleNamespace(remote_number="+15555550101"),
    ]
    client = _FakeIMessageClient(identity)
    prompts = []

    def _prompt_yes_no(question, default=True):
        prompts.append((question.strip(), default))
        return default

    monkeypatch.setattr(setup_wizard, "prompt_yes_no", _prompt_yes_no)
    monkeypatch.setattr(
        setup_wizard,
        "_wait_for_imessage_first_message",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not walk through connect")),
    )

    setup_wizard._configure_imessage(
        "ApiKey_test", "https://inkbox.ai", "agent", lambda **_kwargs: client,
    )

    assert prompts == [("Connect another iPhone to this agent now?", False)]


def test_avatar_auto_attached_on_signup(monkeypatch):
    uploaded = {}
    monkeypatch.setattr(
        setup_wizard,
        "_upload_avatar",
        lambda _base, _key, handle, image: (
            uploaded.update(handle=handle, size=len(image)) or (True, "ok")
        ),
    )
    monkeypatch.setattr(
        setup_wizard,
        "prompt_yes_no",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no prompt on signup")),
    )
    monkeypatch.setattr(
        setup_wizard,
        "_identity_has_avatar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no probe on signup")),
    )

    identity = types.SimpleNamespace(agent_handle="dev-agent")
    setup_wizard._configure_avatar("https://inkbox.ai", "ApiKey_x", identity, is_signup=True)

    assert uploaded["handle"] == "dev-agent"
    assert uploaded["size"] > 0


def test_avatar_skipped_when_existing_agent_already_has_one(monkeypatch):
    monkeypatch.setattr(setup_wizard, "_identity_has_avatar", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        setup_wizard,
        "_upload_avatar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not upload")),
    )
    monkeypatch.setattr(
        setup_wizard,
        "prompt_yes_no",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    identity = types.SimpleNamespace(agent_handle="dev-agent")
    setup_wizard._configure_avatar("https://inkbox.ai", "ApiKey_x", identity, is_signup=False)


def test_avatar_offered_and_uploaded_for_existing_agent_without_one(monkeypatch):
    uploaded = {}
    monkeypatch.setattr(setup_wizard, "_identity_has_avatar", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        setup_wizard,
        "_upload_avatar",
        lambda _base, _key, handle, _image: uploaded.update(handle=handle) or (True, "ok"),
    )

    identity = types.SimpleNamespace(agent_handle="dev-agent")
    setup_wizard._configure_avatar("https://inkbox.ai", "ApiKey_x", identity, is_signup=False)

    assert uploaded["handle"] == "dev-agent"


def test_gateway_restart_offered_when_running(monkeypatch, capsys):
    ran = []
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (True, True))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_run_gateway_command", lambda action: ran.append(action) or True)

    assert setup_wizard._offer_gateway_restart() is True

    assert ran == ["restart"]
    assert "Detected a running Hermes gateway" in capsys.readouterr().out


def test_gateway_restart_declined_still_reports_live(monkeypatch, capsys):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (True, True))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        setup_wizard,
        "_run_gateway_command",
        lambda *_args: (_ for _ in ()).throw(AssertionError("declined should not run a gateway command")),
    )

    # A declining operator still has a gateway up, so the closing next-steps
    # list must not tell them to start one.
    assert setup_wizard._offer_gateway_restart() is True
    assert "hermes gateway restart" in capsys.readouterr().out


def test_gateway_launch_offered_when_service_installed(monkeypatch, capsys):
    ran = []
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (False, True))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))
    monkeypatch.setattr(setup_wizard, "_wait_for_gateway_running", lambda *_a, **_k: True)
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_run_gateway_command", lambda action: ran.append(action) or True)

    assert setup_wizard._offer_gateway_restart() is True

    assert ran == ["start"]
    assert "Did not detect a running Hermes gateway" in capsys.readouterr().out


def test_gateway_install_offered_when_no_service_slot(monkeypatch, capsys):
    ran = []
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (False, False))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))
    monkeypatch.setattr(setup_wizard, "_wait_for_gateway_running", lambda *_a, **_k: True)
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_run_gateway_command", lambda action: ran.append(action) or True)

    assert setup_wizard._offer_gateway_restart() is True

    assert ran == ["install"]
    assert "Gateway installed and running" in capsys.readouterr().out


def test_unconfirmed_install_never_tells_the_operator_to_start_one(monkeypatch, capsys):
    # `install` can come up as a plain background process when the platform's
    # service manager refuses the bootstrap. Telling the operator to start
    # another one on top of that would duplicate the gateway.
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (False, False))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))
    monkeypatch.setattr(setup_wizard, "_wait_for_gateway_running", lambda *_a, **_k: False)
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_run_gateway_command", lambda _action: True)

    assert setup_wizard._offer_gateway_restart() is True

    out = capsys.readouterr().out
    assert "hermes gateway status" in out
    assert "hermes gateway start" not in out
    assert "hermes gateway run" not in out


def test_wait_for_gateway_running_polls_until_it_appears(monkeypatch):
    # A gateway takes its runtime lock partway through startup, so the first
    # probe after a start can legitimately miss it.
    states = iter([(False, False), (False, False), (True, True)])
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: next(states))
    monkeypatch.setattr(setup_wizard.time, "sleep", lambda _s: None)

    assert setup_wizard._wait_for_gateway_running(timeout=30.0) is True


def test_wait_for_gateway_running_gives_up_at_the_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(
        setup_wizard, "_gateway_runtime_state", lambda: calls.append(1) or (False, True)
    )
    monkeypatch.setattr(setup_wizard.time, "sleep", lambda _s: None)

    assert setup_wizard._wait_for_gateway_running(timeout=0.0) is False
    # Timeout 0 still probes once before giving up.
    assert calls == [1]


def test_gateway_install_declined_points_at_foreground_run(monkeypatch, capsys):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (False, False))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        setup_wizard,
        "_run_gateway_command",
        lambda *_args: (_ for _ in ()).throw(AssertionError("declined should not run a gateway command")),
    )

    assert setup_wizard._offer_gateway_restart() is False
    assert "hermes gateway run" in capsys.readouterr().out


def test_gateway_install_failure_falls_back_to_foreground_run(monkeypatch, capsys):
    # Termux / bare WSL have no service manager and `install` exits non-zero.
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (False, False))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_run_gateway_command", lambda _action: False)

    assert setup_wizard._offer_gateway_restart() is False
    assert "hermes gateway run" in capsys.readouterr().out


def test_gateway_restart_not_offered_from_inside_the_gateway(monkeypatch, capsys):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setattr(
        setup_wizard,
        "_run_gateway_command",
        lambda *_args: (_ for _ in ()).throw(AssertionError("self-targeting restart must not run")),
    )

    assert setup_wizard._offer_gateway_restart() is True
    assert "inside the gateway process" in capsys.readouterr().out


def test_gateway_command_prefers_hermes_on_path(monkeypatch):
    calls = []
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "/bin/hermes" if name == "hermes" else None)
    monkeypatch.setattr(setup_wizard.subprocess, "check_call", lambda cmd, **_kwargs: calls.append(cmd))

    assert setup_wizard._run_gateway_command("restart") is True
    assert calls == [["/bin/hermes", "gateway", "restart"]]


def test_gateway_command_falls_back_to_hermes_cli_module(monkeypatch):
    calls = []
    monkeypatch.setattr(setup_wizard.sys, "executable", "/tmp/hermes/venv/bin/python")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _name: None)
    monkeypatch.setattr(setup_wizard.subprocess, "check_call", lambda cmd, **_kwargs: calls.append(cmd))

    assert setup_wizard._run_gateway_command("start") is True
    assert calls == [["/tmp/hermes/venv/bin/python", "-m", "hermes_cli.main", "gateway", "start"]]


def test_gateway_install_runs_without_a_timeout(monkeypatch):
    # `install` prompts on this terminal; a clock on it would kill the run
    # while the operator is still answering.
    seen = {}
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _name: "/bin/hermes")
    monkeypatch.setattr(
        setup_wizard.subprocess, "check_call", lambda _cmd, **kwargs: seen.update(kwargs)
    )

    assert setup_wizard._run_gateway_command("install") is True
    assert seen["timeout"] is None

    assert setup_wizard._run_gateway_command("restart") is True
    assert seen["timeout"] == setup_wizard.GATEWAY_COMMAND_TIMEOUT


def test_gateway_command_failure_is_reported_not_raised(monkeypatch, capsys):
    def boom(_cmd, **_kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _name: "/bin/hermes")
    monkeypatch.setattr(setup_wizard.subprocess, "check_call", boom)

    assert setup_wizard._run_gateway_command("restart") is False
    assert "no such file" in capsys.readouterr().out


def test_avatar_declined_for_existing_agent(monkeypatch):
    monkeypatch.setattr(setup_wizard, "_identity_has_avatar", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        setup_wizard,
        "_upload_avatar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("declined should not upload")),
    )

    identity = types.SimpleNamespace(agent_handle="dev-agent")
    setup_wizard._configure_avatar("https://inkbox.ai", "ApiKey_x", identity, is_signup=False)


def test_duplicate_gateways_are_reported(monkeypatch, capsys):
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111, 222, 333))

    assert setup_wizard._warn_if_multiple_gateways() is True

    out = capsys.readouterr().out
    assert "Found 3 running gateway processes: 111, 222, 333" in out
    assert "hermes gateway stop --all" in out


def test_single_gateway_is_not_reported(monkeypatch, capsys):
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))

    assert setup_wizard._warn_if_multiple_gateways() is False
    assert capsys.readouterr().out == ""


def test_restart_does_not_claim_success_over_a_stale_sibling(monkeypatch, capsys):
    # A second gateway holds the Inkbox platform lock and listen port, so the
    # one we just restarted comes up with Inkbox dead. Saying "restarted with
    # the new Inkbox config" there would be a lie.
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (True, True))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111, 222))
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_run_gateway_command", lambda _action: True)

    assert setup_wizard._offer_gateway_restart() is True

    out = capsys.readouterr().out
    assert "Found 2 running gateway processes" in out
    assert "restarted with the new Inkbox config" not in out


def test_start_confirms_liveness_before_claiming_success(monkeypatch, capsys):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(setup_wizard, "_gateway_runtime_state", lambda: (False, True))
    monkeypatch.setattr(setup_wizard, "_running_gateway_pids", lambda: (111,))
    monkeypatch.setattr(setup_wizard, "_wait_for_gateway_running", lambda *_a, **_k: False)
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(setup_wizard, "_run_gateway_command", lambda _action: True)

    assert setup_wizard._offer_gateway_restart() is True

    out = capsys.readouterr().out
    assert "hermes gateway status" in out
    assert "started with the new Inkbox config" not in out


def _stub_setup_dependencies(monkeypatch, *, gateway_live):
    """Short-circuit every step of interactive_setup except its closing lines."""
    identity = types.SimpleNamespace(agent_handle="stub-agent", mailbox=None, phone_number=None)

    monkeypatch.setattr(setup_wizard, "_ensure_inkbox_sdk", lambda: {
        "Inkbox": object,
        "InkboxAPIError": Exception,
        "IdentityPhoneNumberCreateOptions": object,
        "WhoamiApiKeyResponse": object,
        "ADMIN_SCOPED": "admin",
        "AGENT_CLAIMED": "claimed",
        "AGENT_UNCLAIMED": "unclaimed",
    })
    monkeypatch.setattr(setup_wizard, "_env", lambda _name: "")
    monkeypatch.setattr(setup_wizard, "_save", lambda *_a, **_k: None)
    monkeypatch.setattr(setup_wizard, "prompt_yes_no", lambda *_a, **_k: False)
    monkeypatch.setattr(
        setup_wizard, "_self_signup_flow", lambda *_a, **_k: (identity, "ApiKey_stub", False)
    )
    monkeypatch.setattr(setup_wizard, "_configure_avatar", lambda *_a, **_k: None)
    monkeypatch.setattr(setup_wizard, "_configure_imessage", lambda *_a, **_k: False)
    monkeypatch.setattr(setup_wizard, "_offer_dedicated_number", lambda *_a, **_k: (identity, False))
    monkeypatch.setattr(setup_wizard, "_seed_identity_state", lambda *_a, **_k: None)
    monkeypatch.setattr(setup_wizard, "_print_agent_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(setup_wizard, "_configure_realtime_calls", lambda *_a, **_k: None)
    monkeypatch.setattr(setup_wizard, "_setup_signing_key", lambda *_a, **_k: None)
    monkeypatch.setattr(setup_wizard, "_offer_gateway_restart", lambda: gateway_live)
    return identity


def test_ready_banner_names_the_identity_and_the_health_command(capsys):
    setup_wizard._print_ready_banner("dimas-clanker")

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert any("dimas-clanker" in line for line in lines)
    assert any("hermes inkbox doctor" in line for line in lines)
    # Every row is the same width, so the box closes cleanly.
    assert len({len(line) for line in lines}) == 1


def test_ready_banner_box_fits_a_long_handle(capsys):
    setup_wizard._print_ready_banner("a-very-long-agent-handle-that-sets-the-width")

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len({len(line) for line in lines}) == 1


def test_live_gateway_closes_on_the_banner_not_a_todo_list(monkeypatch, capsys):
    _stub_setup_dependencies(monkeypatch, gateway_live=True)

    setup_wizard.interactive_setup()

    out = capsys.readouterr().out
    assert "Your Hermes agent is set up and running on Inkbox." in out
    assert "stub-agent" in out
    assert "Next steps:" not in out
    assert "hermes gateway run" not in out


def test_dead_gateway_still_gets_the_next_steps_list(monkeypatch, capsys):
    _stub_setup_dependencies(monkeypatch, gateway_live=False)

    setup_wizard.interactive_setup()

    out = capsys.readouterr().out
    assert "Next steps:" in out
    assert "hermes gateway run" in out
    assert "Your Hermes agent is set up and running" not in out
