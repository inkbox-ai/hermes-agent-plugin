import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import doctor
from inkbox_plugin.config import set_runtime_config_extra
from inkbox_plugin.diagnostics import inkbox_api_error_message, is_inkbox_auth_error, missing_config_message


def _clear_inkbox_env(monkeypatch):
    for name in (
        "INKBOX_API_KEY",
        "INKBOX_IDENTITY",
        "INKBOX_SIGNING_KEY",
        "INKBOX_BASE_URL",
        "INKBOX_PUBLIC_URL",
        "INKBOX_REALTIME_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_config_messages_point_to_setup():
    message = missing_config_message("INKBOX_API_KEY")

    assert "INKBOX_API_KEY is not set" in message
    assert "hermes inkbox setup" in message


def test_auth_error_message_classifies_unauthorized_setup_failure():
    exc = RuntimeError("HTTP 401: Unauthorized")

    assert is_inkbox_auth_error(exc)
    message = inkbox_api_error_message(exc, "opening the SDK tunnel")
    assert "authentication failed" in message
    assert "valid INKBOX_API_KEY/INKBOX_IDENTITY pair" in message
    assert "hermes inkbox setup" in message


def test_identity_error_message_points_to_identity_key_pairing():
    exc = RuntimeError("HTTP 404: identity not found")

    message = inkbox_api_error_message(exc, "checking the Inkbox API")
    assert "identity lookup failed" in message
    assert "INKBOX_IDENTITY belongs to the configured API key" in message
    assert "hermes inkbox setup" in message


def test_doctor_missing_config_findings_include_setup_hint(monkeypatch):
    _clear_inkbox_env(monkeypatch)

    summary = doctor.run_doctor()

    messages = {finding["id"]: finding["message"] for finding in summary["findings"]}
    assert "hermes inkbox setup" in messages["inkbox/config-missing-api-key"]
    assert "hermes inkbox setup" in messages["inkbox/config-missing-identity"]


def test_doctor_api_check_failure_is_actionable(monkeypatch):
    _clear_inkbox_env(monkeypatch)
    monkeypatch.setenv("INKBOX_API_KEY", "ApiKey_fake")
    monkeypatch.setenv("INKBOX_IDENTITY", "fake-agent")

    class FakeInkbox:
        def __init__(self, **_kwargs):
            pass

        def whoami(self):
            raise RuntimeError("HTTP 401: Unauthorized")

    monkeypatch.setitem(sys.modules, "inkbox", types.SimpleNamespace(Inkbox=FakeInkbox))

    summary = doctor.run_doctor()

    api_finding = next(finding for finding in summary["findings"] if finding["id"] == "inkbox/api-check-failed")
    assert "authentication failed" in api_finding["message"]
    assert "valid INKBOX_API_KEY/INKBOX_IDENTITY pair" in api_finding["message"]
    assert "hermes inkbox setup" in api_finding["message"]


def test_doctor_reports_voice_ai_config_and_drift(monkeypatch):
    _clear_inkbox_env(monkeypatch)
    monkeypatch.setenv("INKBOX_API_KEY", "ApiKey_fake")
    monkeypatch.setenv("INKBOX_IDENTITY", "fake-agent")
    monkeypatch.setenv("INKBOX_SIGNING_KEY", "whsec_fake")
    monkeypatch.setenv("INKBOX_VOICE_STACK", "inkbox_voice_ai")
    monkeypatch.setenv("INKBOX_VOICE_AI_AUTHORITY_MODE", "yolo")

    class Identity:
        id = "identity-1"
        mailbox = None
        phone_number = None
        imessage_enabled = False

        def get_incoming_call_action(self):
            return types.SimpleNamespace(incoming_call_action="auto_accept")

        def get_hosted_agent_config(self):
            return types.SimpleNamespace(
                authority_mode="contact_scoped",
                voice=None,
                model=None,
                effective_voice="cedar",
                effective_model="gpt-realtime",
            )

    class FakeInkbox:
        def __init__(self, **_kwargs):
            pass

        def whoami(self):
            return {"scope": "agent"}

        def get_identity(self, _handle):
            return Identity()

    monkeypatch.setitem(
        sys.modules, "inkbox", types.SimpleNamespace(Inkbox=FakeInkbox),
    )

    summary = doctor.run_doctor()

    assert summary["voiceStack"] == "inkbox_voice_ai"
    assert summary["voiceAiAuthorityMode"] == "yolo"
    finding_ids = {finding["id"] for finding in summary["findings"]}
    assert "inkbox/incoming-call-action-drift" in finding_ids
    assert "inkbox/voice-ai-authority-drift" in finding_ids


def test_doctor_requires_realtime_key_only_in_realtime_mode(monkeypatch):
    _clear_inkbox_env(monkeypatch)
    monkeypatch.setenv("INKBOX_VOICE_STACK", "openai_realtime")

    summary = doctor.run_doctor()

    finding_ids = {finding["id"] for finding in summary["findings"]}
    assert "inkbox/realtime-key-missing" in finding_ids

    monkeypatch.setenv("INKBOX_VOICE_STACK", "inkbox_tts_stt")
    summary = doctor.run_doctor()
    finding_ids = {finding["id"] for finding in summary["findings"]}
    assert "inkbox/realtime-key-missing" not in finding_ids


def test_doctor_uses_effective_yaml_voice_config(monkeypatch):
    _clear_inkbox_env(monkeypatch)
    monkeypatch.setenv("INKBOX_API_KEY", "ApiKey_fake")
    monkeypatch.setenv("INKBOX_IDENTITY", "fake-agent")
    set_runtime_config_extra({
        "voice_stack": "inkbox_voice_ai",
        "voice_ai_authority_mode": "yolo",
        "voicemail_detection": "disabled",
    })

    class Identity:
        id = "identity-1"
        mailbox = None
        phone_number = None
        imessage_enabled = False

        def get_incoming_call_action(self):
            return types.SimpleNamespace(incoming_call_action="hosted_agent")

        def get_hosted_agent_config(self):
            return types.SimpleNamespace(authority_mode="yolo")

    class FakeInkbox:
        def __init__(self, **_kwargs):
            pass

        def whoami(self):
            return {"scope": "agent"}

        def get_identity(self, _handle):
            return Identity()

    monkeypatch.setitem(
        sys.modules, "inkbox", types.SimpleNamespace(Inkbox=FakeInkbox),
    )
    try:
        summary = doctor.run_doctor()
    finally:
        set_runtime_config_extra({})

    assert summary["voiceStack"] == "inkbox_voice_ai"
    assert summary["voiceAiAuthorityMode"] == "yolo"
    assert summary["voicemailDetection"] == "disabled"
    finding_ids = {finding["id"] for finding in summary["findings"]}
    assert "inkbox/incoming-call-action-drift" not in finding_ids
    assert "inkbox/voice-ai-authority-drift" not in finding_ids


def test_doctor_ignores_inactive_voice_ai_authority(monkeypatch):
    _clear_inkbox_env(monkeypatch)
    set_runtime_config_extra({
        "voice_stack": "inkbox_tts_stt",
        "voice_ai_authority_mode": "invalid-but-inactive",
    })
    try:
        summary = doctor.run_doctor()
    finally:
        set_runtime_config_extra({})

    finding_ids = {finding["id"] for finding in summary["findings"]}
    assert "inkbox/config-invalid-voice-ai-authority" not in finding_ids


def test_doctor_reports_safe_a2a_delivery_and_local_lifecycle(
    monkeypatch,
    tmp_path,
):
    _clear_inkbox_env(monkeypatch)
    monkeypatch.setenv("INKBOX_API_KEY", "ApiKey_fake")
    monkeypatch.setenv("INKBOX_IDENTITY", "fake-agent")
    monkeypatch.setenv("INKBOX_SIGNING_KEY", "whsec_fake")
    state_path = tmp_path / "inkbox_identity_state.json"
    monkeypatch.setattr(doctor, "inkbox_state_path", lambda: state_path)
    state_path.with_name("inkbox_a2a_tasks.json").write_text(json.dumps({
        "task-1:message-1": {
            "task_id": "task-1",
            "context_id": "context-1",
            "message_id": "message-1",
            "state": "acknowledgement_failed",
            "updated_at": 123.0,
            "phases": {"received": 120.0, "enqueued": 122.0},
            "last_error": {
                "phase": "acknowledgement",
                "code": "receipt_delivery_failed",
                "at": 123.0,
            },
            "data": {"parts": [{"text": "private task body"}]},
        },
    }))

    class Identity:
        id = "identity-1"
        mailbox = None
        phone_number = None
        imessage_enabled = False

        def get_incoming_call_action(self):
            return types.SimpleNamespace(incoming_call_action="auto_accept")

    subscription = types.SimpleNamespace(
        id="subscription-1",
        event_types=[
            "a2a.task.created",
            "a2a.task.message",
            "a2a.task.canceled",
        ],
        status="active",
        url="https://agent.example/inkbox/webhook?key=secret-value",
        created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-02T00:00:00Z",
    )
    delivery = types.SimpleNamespace(
        id="delivery-1",
        event_id="event-1",
        event_type="a2a.task.created",
        response_status=401,
        response_body="sensitive response body",
        request_payload="sensitive request body",
        error_detail="sensitive transport detail",
        is_replay=False,
        created_at="2026-08-03T00:00:00Z",
    )

    class FakeInkbox:
        def __init__(self, **_kwargs):
            self.webhooks = types.SimpleNamespace(
                subscriptions=types.SimpleNamespace(
                    list=lambda **_kwargs: [subscription],
                ),
                deliveries=types.SimpleNamespace(
                    list=lambda **_kwargs: [delivery],
                ),
            )

        def whoami(self):
            return {"scope": "agent"}

        def get_identity(self, _handle):
            return Identity()

    monkeypatch.setitem(
        sys.modules, "inkbox", types.SimpleNamespace(Inkbox=FakeInkbox),
    )

    summary = doctor.run_doctor()

    assert summary["a2a"]["ready"] is True
    assert summary["a2a"]["subscriptions"][0]["receiverUrl"] == (
        "https://agent.example/inkbox/webhook"
    )
    assert summary["a2a"]["recentDeliveries"][0]["result"] == (
        "authentication"
    )
    assert summary["a2a"]["localTasks"][0]["state"] == (
        "acknowledgement_failed"
    )
    finding_ids = {finding["id"] for finding in summary["findings"]}
    assert "inkbox/a2a-latest-delivery-failed" in finding_ids
    rendered = json.dumps(summary)
    assert "secret-value" not in rendered
    assert "private task body" not in rendered
    assert "sensitive response body" not in rendered
    assert "sensitive request body" not in rendered
    assert "sensitive transport detail" not in rendered


def test_a2a_diagnostics_marks_drifted_subscription_not_ready(monkeypatch):
    monkeypatch.setattr(doctor, "_local_a2a_tasks", lambda: [])
    subscription = types.SimpleNamespace(
        id="subscription-1",
        event_types=["a2a.task.created"],
        status="active",
        url="https://agent.example/inkbox/webhook",
        created_at=None,
        updated_at=None,
    )
    client = types.SimpleNamespace(webhooks=types.SimpleNamespace(
        subscriptions=types.SimpleNamespace(
            list=lambda **_kwargs: [subscription],
        ),
        deliveries=types.SimpleNamespace(list=lambda **_kwargs: []),
    ))

    result = doctor._a2a_diagnostics(client, "identity-1")

    assert result["ready"] is False
    assert result["subscriptions"][0]["eventTypes"] == [
        "a2a.task.created",
    ]
