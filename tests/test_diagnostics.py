import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import doctor
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
