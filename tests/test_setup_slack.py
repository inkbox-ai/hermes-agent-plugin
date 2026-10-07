"""Slack onboarding, preparation, installation, and credential boundaries."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from inkbox_plugin import setup_wizard as wizard


def workspace(id="saved-workspace-1", status="ready"):
    return NS(id=id, workspace_id="TEXAMPLE", workspace_name="Example workspace", status=status)


def snapshot(status="ready", connections=(), *, bound=True, error=None, available=None):
    saved = workspace() if bound else None
    return NS(
        setup=NS(status=status, error_code=error, provisioning_workspace_id=saved.id if saved else None),
        connections=list(connections), installation_available=status == "ready" if available is None else available,
        provisioning_workspace=saved, application_created=status == "ready",
    )


def connection(id="connection-1", status="connected", workspace_id="TEXAMPLE"):
    return NS(id=id, status=status, workspace_id=workspace_id, workspace_name="Example workspace")


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    def save(name, value):
        import os
        path = tmp_path / ".env"
        entries = dict(line.split("=", 1) for line in path.read_text().splitlines()) if path.exists() else {}
        entries[name] = value
        path.write_text("".join(f"{key}={item}\n" for key, item in entries.items()))
        os.environ[name] = value
    monkeypatch.setattr(wizard, "_save", save)
    # _save updates os.environ directly; register even an initially absent key
    # so teardown restores it instead of enabling Slack for subsequent tests.
    monkeypatch.setenv("INKBOX_SLACK_ENABLED", "false")
    # No remote slack_enabled field: opting in is a bridge-local setting.
    identity = NS(id="identity-1", agent_handle="agent", update=Mock())
    resource = Mock(spec=["list_connections", "list_provisioning_workspaces", "save_provisioning_workspace",
                          "start_setup", "start_installation"])
    resource.list_connections.return_value = snapshot()
    resource.list_provisioning_workspaces.return_value = [workspace()]
    resource.save_provisioning_workspace.return_value = workspace()
    resource.start_installation.return_value = NS(
        authorization_url="https://example.com/install?state=one-time",
        expires_at="2030-01-01T00:00:00Z",
    )
    info = NS(auth_subtype="api_key.agent_scoped.claimed", scope="agent_identity:identity-1",
              organization_id="org-1")

    class Client:
        def __init__(self, who):
            self.who = who
            self.slack = resource
            self.closed = False

        def whoami(self):
            return self.who

        def get_identity(self, handle):
            assert handle == "agent"
            return identity

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    runtime = Client(info)
    clients = {"runtime-key": runtime}
    factory = Mock(side_effect=lambda **kw: clients[kw["api_key"]])
    answers = iter([True, True])
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda *a: next(answers))
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: pytest.fail("unexpected credential prompt"))
    monkeypatch.setattr(wizard, "prompt_choice", lambda *a: 0)
    now = [0.0]
    monkeypatch.setattr(wizard.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(wizard.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return NS(identity=identity, resource=resource, runtime=runtime, factory=factory,
              env=tmp_path / ".env", now=now,
              run=lambda: wizard._configure_slack("runtime-key", "https://example.com", "agent", factory))


def test_install_prepares_then_links_then_polls_real_connection(setup, capsys):
    setup.resource.list_connections.side_effect = [
        snapshot("needs_credentials", bound=False), snapshot("pending"), snapshot(),
        snapshot(), snapshot(connections=[connection()]),
    ]
    assert setup.run() is True
    setup.identity.update.assert_not_called()
    setup.resource.start_setup.assert_called_once_with("identity-1", "saved-workspace-1")
    setup.resource.start_installation.assert_called_once_with("identity-1", workspace_id="TEXAMPLE")
    assert setup.now[0] == 8
    out = capsys.readouterr().out
    assert "https://example.com/install?state=one-time" in out
    assert "Slack connected: Example workspace" in out
    assert "runtime-key" not in out
    assert setup.env.read_text() == "INKBOX_SLACK_ENABLED=true\n"
    assert setup.runtime.closed
    assert setup.factory.call_count == 1


def test_decline_only_disables_local_bridge(setup, monkeypatch):
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda *a: False)
    assert setup.run() is False
    setup.factory.assert_not_called()
    assert setup.env.read_text() == "INKBOX_SLACK_ENABLED=false\n"


def test_enable_later_does_not_mutate_identity_or_create_installation(setup, monkeypatch):
    answers = iter([True, False])
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda *a: next(answers))
    assert setup.run() is True
    setup.identity.update.assert_not_called()
    setup.resource.start_installation.assert_not_called()
    assert "true" in setup.env.read_text()


def test_existing_connection_needs_no_admin_and_preserves_saved_default(setup, monkeypatch, capsys):
    monkeypatch.setenv("INKBOX_SLACK_ENABLED", "true")
    setup.resource.list_connections.return_value = snapshot(connections=[connection()])
    questions = []

    def answer(question, default):
        questions.append((question, default))
        return default

    monkeypatch.setattr(wizard, "prompt_yes_no", answer)
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: pytest.fail("unexpected credential prompt"))
    assert setup.run() is True
    assert [default for _, default in questions] == [True]
    setup.resource.start_setup.assert_not_called()
    setup.resource.start_installation.assert_not_called()
    out = capsys.readouterr().out
    assert "Already connected" in out and "Slack connected" not in out


def test_connection_poll_requires_selected_workspace(setup, capsys):
    setup.resource.list_connections.side_effect = [
        snapshot(), snapshot(), snapshot(connections=[connection(workspace_id="TOTHER")]),
        snapshot(connections=[connection()]),
    ]
    assert setup.run() is True
    assert setup.resource.list_connections.call_count == 4
    assert setup.now[0] == 4
    assert "Slack connected" in capsys.readouterr().out


def test_reauthorization_counts_as_connected_when_status_changes(setup, capsys):
    setup.resource.list_connections.side_effect = [
        snapshot(connections=[connection(status="reauthorization_required")]),
        snapshot(), snapshot(connections=[connection()]),
    ]
    assert setup.run() is True
    setup.resource.start_setup.assert_not_called()
    assert "Slack connected" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["failed", "unavailable"])
def test_failed_preparation_never_creates_link_or_claims_connection(setup, capsys, status):
    setup.resource.list_connections.return_value = snapshot(status)
    assert setup.run() is True
    setup.resource.start_installation.assert_not_called()
    out = capsys.readouterr().out
    assert ("needs attention" in out or "unavailable" in out) and "Slack connected" not in out


@pytest.mark.parametrize("phase", ["preparation", "connection"])
def test_timeout_is_bounded_and_preserves_enabled_config(setup, phase, capsys):
    setup.resource.list_connections.return_value = snapshot("pending" if phase == "preparation" else "ready")
    assert setup.run() is True
    assert setup.now[0] == 300
    assert "Still waiting" in capsys.readouterr().out
    assert "true" in setup.env.read_text()
    setup.resource.start_setup.assert_not_called()


def test_interrupt_wait_keeps_other_setup_steps_available(setup, monkeypatch, capsys):
    setup.resource.list_connections.return_value = snapshot("pending")
    monkeypatch.setattr(wizard.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    assert setup.run() is True
    assert "waiting skipped" in capsys.readouterr().out
    setup.resource.start_installation.assert_not_called()


@pytest.mark.parametrize("claimed", [True, False])
def test_setup_uses_configured_key_without_switching_to_ambient_admin(setup, monkeypatch, claimed):
    if not claimed:
        setup.runtime.who.auth_subtype = "api_key.admin_scoped"
        setup.runtime.who.scope = "organization"
    monkeypatch.setenv("INKBOX_ADMIN_API_KEY", "unrelated-synthetic-admin-key")
    setup.resource.list_connections.side_effect = [
        snapshot("needs_credentials", bound=False), snapshot(), snapshot(connections=[connection()]),
    ]
    assert setup.run() is True
    setup.resource.start_setup.assert_called_once_with("identity-1", "saved-workspace-1")
    setup.resource.start_installation.assert_called_once_with("identity-1", workspace_id="TEXAMPLE")
    assert setup.factory.call_count == 1
    assert setup.factory.call_args.kwargs["api_key"] == "runtime-key"
    assert setup.env.read_text() == "INKBOX_SLACK_ENABLED=true\n"


def test_mismatched_runtime_key_cannot_configure_another_identity(setup):
    setup.runtime.who.scope = "agent_identity:other"
    assert setup.run() is False
    setup.resource.list_connections.assert_not_called()
    setup.identity.update.assert_not_called()


def test_unclaimed_identity_gets_actionable_claim_instruction(setup, capsys):
    setup.runtime.who.auth_subtype = "api_key.agent_scoped.unclaimed"
    assert setup.run() is False
    assert "Claim it" in capsys.readouterr().out
    setup.resource.start_setup.assert_not_called()


def test_setup_auth_failure_never_requests_a_different_key(setup, capsys):
    setup.resource.list_connections.return_value = snapshot("needs_credentials", bound=False)
    error = RuntimeError("credential=do-not-print")
    error.status_code = 403
    setup.resource.list_provisioning_workspaces.side_effect = error
    assert setup.run() is True
    setup.identity.update.assert_not_called()
    setup.resource.start_setup.assert_not_called()
    setup.resource.start_installation.assert_not_called()
    assert setup.factory.call_count == 1
    assert setup.env.read_text() == "INKBOX_SLACK_ENABLED=true\n"
    out = capsys.readouterr().out
    assert "Could not finish Slack setup" in out and "credential=do-not-print" not in out


def test_old_sdk_is_optional_and_does_not_break_setup(setup, capsys):
    setup.runtime.slack = NS(list_connections=Mock())
    assert setup.run() is False
    assert "requires an Inkbox SDK" in capsys.readouterr().out
    assert not setup.env.exists()


def test_missing_server_setup_status_does_not_claim_success(setup, capsys):
    setup.resource.list_connections.return_value.setup = None
    assert setup.run() is True
    setup.resource.start_installation.assert_not_called()
    assert "setup is unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("pending", [False, True])
def test_installation_retry_only_for_explicit_setup_pending(setup, capsys, pending):
    error = RuntimeError("credential=do-not-print")
    error.status_code = 409 if pending else 503
    error.detail = {"code": "slack_setup_pending"} if pending else None
    installation = setup.resource.start_installation.return_value
    setup.resource.start_installation.side_effect = [error, installation]
    setup.resource.list_connections.side_effect = [
        snapshot(), snapshot(), snapshot(), snapshot(connections=[connection()]),
    ]
    assert setup.run() is True
    assert setup.resource.start_installation.call_count == (2 if pending else 1)
    out = capsys.readouterr().out
    assert "credential=do-not-print" not in out
    assert ("Slack connected" in out) is pending


def test_missing_installation_link_does_not_poll_or_claim_connection(setup, capsys):
    setup.resource.start_installation.return_value.authorization_url = None
    assert setup.run() is True
    assert "No Slack installation" in capsys.readouterr().out
    assert setup.resource.list_connections.call_count == 2


def test_unknown_creation_outcome_never_retries_or_requests_credentials(setup, monkeypatch, capsys):
    setup.resource.list_connections.return_value = snapshot("failed", error="outcome_unknown")
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: pytest.fail("unexpected key prompt"))
    assert setup.run() is True
    setup.resource.start_setup.assert_not_called()
    setup.resource.start_installation.assert_not_called()
    assert "Contact support" in capsys.readouterr().out


@pytest.mark.parametrize("status,available", [("needs_credentials", False), ("ready", False)])
def test_preparation_stops_when_credentials_or_installation_unavailable(setup, monkeypatch, status, available, capsys):
    setup.resource.list_connections.side_effect = [snapshot("pending"), snapshot(status, available=available)]
    answers = iter([True, True, False])
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda *a: next(answers))
    assert setup.run() is True
    setup.resource.start_setup.assert_not_called()
    setup.resource.start_installation.assert_not_called()
    assert setup.now[0] == 0
    out = capsys.readouterr().out
    assert "Slack connected" not in out and "Could not finish Slack setup" not in out


@pytest.mark.parametrize("status,detail", [
    (422, "do-not-print-response-or-tokens"),
    (409, {"code": "credentials_required", "message": "do-not-print-response-or-tokens"}),
    (409, "Generate a separate app-configuration token pair for this organization."),
    (409, "Use app-configuration tokens from the Slack user who created these apps."),
])
def test_rejected_credentials_offer_another_pair_and_finish_setup(setup, monkeypatch, capsys, status, detail):
    setup.resource.list_provisioning_workspaces.return_value = []
    setup.resource.list_connections.side_effect = [
        snapshot("needs_credentials", bound=False), snapshot(), snapshot(connections=[connection()]),
    ]
    error = RuntimeError("do-not-print-response-or-tokens")
    error.status_code, error.detail = status, detail
    setup.resource.save_provisioning_workspace.side_effect = [error, workspace()]
    credentials = iter(["access-first", "refresh-first", "access-second", "refresh-second"])
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: next(credentials))
    questions = []
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda question, default: questions.append(question) or True)

    assert setup.run() is True
    assert setup.resource.save_provisioning_workspace.call_count == 2
    assert setup.resource.save_provisioning_workspace.call_args.kwargs == {
        "access_token": "access-second", "refresh_token": "refresh-second",
    }
    assert any("another app-configuration token pair" in q for q in questions)
    setup.resource.start_setup.assert_called_once_with("identity-1", "saved-workspace-1")
    out = capsys.readouterr().out
    assert "No saved Slack workspaces" in out
    assert "Slack connected" in out
    assert all(secret not in out for secret in (
        "do-not-print-response-or-tokens", "access-first", "refresh-first", "access-second", "refresh-second",
    ))
    assert setup.env.read_text() == "INKBOX_SLACK_ENABLED=true\n"


def test_declining_credential_retry_does_not_create_app(setup, monkeypatch, capsys):
    setup.resource.list_provisioning_workspaces.return_value = []
    setup.resource.list_connections.return_value = snapshot("needs_credentials", bound=False)
    error = RuntimeError("secret-response")
    error.status_code = 422
    setup.resource.save_provisioning_workspace.side_effect = error
    tokens = iter(["access", "refresh"])
    answers = iter([True, True, False])
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: next(tokens))
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda *a: next(answers))

    assert setup.run() is True
    setup.resource.save_provisioning_workspace.assert_called_once()
    setup.resource.start_setup.assert_not_called()
    setup.resource.start_installation.assert_not_called()
    out = capsys.readouterr().out
    assert "Slack rejected" in out and "secret-response" not in out


@pytest.mark.parametrize("status", [401, 403, 409, 503, None])
def test_noncredential_failure_reports_the_step_without_replaying_save(setup, monkeypatch, capsys, status):
    setup.resource.list_provisioning_workspaces.return_value = []
    setup.resource.list_connections.return_value = snapshot("needs_credentials", bound=False)
    error = RuntimeError("secret-response")
    error.status_code = status
    error.detail = {"code": "unexpected", "input": "secret-response"}
    setup.resource.save_provisioning_workspace.side_effect = error
    tokens = iter(["access", "refresh"])
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: next(tokens))

    assert setup.run() is True
    setup.resource.save_provisioning_workspace.assert_called_once()
    setup.resource.start_setup.assert_not_called()
    out = capsys.readouterr().out
    assert "saving workspace credentials" in out and "secret-response" not in out
    if status:
        assert f"HTTP {status}" in out


def test_preparation_can_renew_credentials_and_resume_without_leaving_wizard(setup, monkeypatch, capsys):
    setup.resource.list_connections.side_effect = [
        snapshot("pending"), snapshot("needs_credentials"), snapshot(), snapshot(connections=[connection()]),
    ]
    tokens = iter(["renewed-access", "renewed-refresh"])
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: next(tokens))
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda *a: True)

    assert setup.run() is True
    setup.resource.save_provisioning_workspace.assert_called_once_with(
        access_token="renewed-access", refresh_token="renewed-refresh")
    setup.resource.start_setup.assert_called_once_with("identity-1", "saved-workspace-1")
    setup.resource.start_installation.assert_called_once()
    assert "Slack connected" in capsys.readouterr().out


def test_new_workspace_uses_masked_pair_without_local_persistence(setup, monkeypatch, capsys):
    setup.resource.list_provisioning_workspaces.return_value = []
    setup.resource.list_connections.side_effect = [
        snapshot("needs_credentials", bound=False), snapshot(), snapshot(connections=[connection()]),
    ]
    answers = iter(["xoxe.xoxp-synthetic-access", "xoxe-synthetic-refresh"])
    prompts = []

    def ask(question, **kwargs):
        prompts.append(kwargs)
        return next(answers)

    monkeypatch.setattr(wizard, "prompt", ask)
    assert setup.run() is True
    assert all(p["password"] for p in prompts)
    setup.resource.save_provisioning_workspace.assert_called_once_with(
        access_token="xoxe.xoxp-synthetic-access", refresh_token="xoxe-synthetic-refresh")
    setup.resource.start_setup.assert_called_once_with("identity-1", "saved-workspace-1")
    assert setup.env.read_text() == "INKBOX_SLACK_ENABLED=true\n"
    out = capsys.readouterr().out
    assert "xoxe.xoxp-synthetic-access" not in out and "xoxe-synthetic-refresh" not in out


@pytest.mark.parametrize("tokens", [["", ""], ["xoxe.xoxp-synthetic-access", ""]])
def test_incomplete_token_pair_does_not_save_or_start_setup(setup, monkeypatch, tokens):
    setup.resource.list_provisioning_workspaces.return_value = []
    setup.resource.list_connections.return_value = snapshot("needs_credentials", bound=False)
    answers = iter(tokens)
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: next(answers))
    assert setup.run() is True
    setup.resource.save_provisioning_workspace.assert_not_called()
    setup.resource.start_setup.assert_not_called()


@pytest.mark.parametrize("matching", [False, True])
def test_bound_workspace_credential_renewal_cannot_move_app(setup, monkeypatch, capsys, matching):
    initial = snapshot("needs_credentials")
    initial.provisioning_workspace.status = "reauthorization_required"
    setup.resource.list_connections.side_effect = [initial, snapshot(), snapshot(connections=[connection()])]
    setup.resource.save_provisioning_workspace.return_value = workspace(
        id="saved-workspace-1" if matching else "saved-workspace-other")
    answers = iter(["xoxe.xoxp-synthetic-access", "xoxe-synthetic-refresh"])
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: next(answers))
    retries = iter([True, True, False])
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda *a: next(retries))
    monkeypatch.setattr(wizard, "prompt_choice", lambda *a: pytest.fail("bound app cannot choose workspace"))
    assert setup.run() is True
    setup.resource.list_provisioning_workspaces.assert_not_called()
    if matching:
        setup.resource.start_setup.assert_called_once_with("identity-1", "saved-workspace-1")
    else:
        setup.resource.start_setup.assert_not_called()
        setup.resource.start_installation.assert_not_called()
        assert "cannot move" in capsys.readouterr().out


def test_saved_workspace_selection_passes_saved_id_not_slack_team_id(setup, monkeypatch):
    setup.resource.list_provisioning_workspaces.return_value = [workspace(), workspace(id="saved-workspace-2")]
    monkeypatch.setattr(wizard, "prompt_choice", lambda *a: 1)
    setup.resource.list_connections.side_effect = [
        snapshot("not_started", bound=False), snapshot(), snapshot(connections=[connection()]),
    ]
    assert setup.run() is True
    setup.resource.start_setup.assert_called_once_with("identity-1", "saved-workspace-2")
    setup.resource.save_provisioning_workspace.assert_not_called()


def test_app_without_binding_is_not_recreated(setup, capsys):
    initial = snapshot("failed", bound=False)
    initial.application_created = True
    setup.resource.list_connections.return_value = initial
    assert setup.run() is True
    setup.resource.start_setup.assert_not_called()
    setup.resource.list_provisioning_workspaces.assert_not_called()
    assert "no available workspace binding" in capsys.readouterr().out


@pytest.mark.parametrize("new_workspace,reject_first", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("claimed", [False, True])
def test_onboarding_through_slack_sdk_wire_contract(setup, monkeypatch, capsys, new_workspace, reject_first, claimed):
    """Run actual SDK parsers/builders; removed identity mutations and invitations fail."""
    import json

    import httpx
    from inkbox import Inkbox

    slack = pytest.importorskip("inkbox.slack")
    if not hasattr(slack.SlackResource, "list_provisioning_workspaces"):
        pytest.skip("installed SDK predates Slack provisioning workspaces")

    identity_id = "00000000-0000-4000-8000-000000000001"
    workspace_id = "00000000-0000-4000-8000-000000000002"
    connection_id = "00000000-0000-4000-8000-000000000003"
    saved = {
        "id": workspace_id, "workspace_id": "TEXAMPLE", "workspace_name": "Example workspace",
        "user_id": "UADMIN", "status": "ready", "token_expires_at": "2030-01-01T12:00:00Z",
        "created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-01T00:00:00Z",
    }
    state = {"started": False, "polls": 0, "installed": False, "saves": 0}
    requests = []
    answers = iter((["invalid-access", "invalid-refresh"] if reject_first else [])
                   + ["xoxe.xoxp-synthetic-access", "xoxe-synthetic-refresh"])
    monkeypatch.setattr(wizard, "prompt", lambda *a, **kw: next(answers))
    monkeypatch.setattr(wizard, "prompt_yes_no", lambda *a: True)

    def handle(request):
        assert request.headers["X-API-Key"] == "runtime-key"
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        requests.append((request.method, path))
        if path == "/api/whoami":
            return httpx.Response(200, json={
                "auth_type": "api_key", "organization_id": "org-1",
                "auth_subtype": "api_key.agent_scoped.claimed" if claimed else "api_key.admin_scoped",
                "scope": f"agent_identity:{identity_id}" if claimed else "organization",
            })
        if path == "/api/v1/identities/agent":
            assert request.method == "GET"
            return httpx.Response(200, json={
                "id": identity_id, "organization_id": "org-1", "agent_handle": "agent",
                "created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-01T00:00:00Z",
            })
        if path == "/api/v1/slack/provisioning-workspaces":
            if request.method == "GET":
                return httpx.Response(200, json={"workspaces": [] if new_workspace else [saved]})
            assert request.method == "POST" and new_workspace
            state["saves"] += 1
            if reject_first and state["saves"] == 1:
                assert body == {"access_token": "invalid-access", "refresh_token": "invalid-refresh"}
                return httpx.Response(422, json={"detail": [{"msg": "Invalid token", "input": "invalid-access"}]})
            assert body == {"access_token": "xoxe.xoxp-synthetic-access", "refresh_token": "xoxe-synthetic-refresh"}
            return httpx.Response(200, json=saved)
        if path == "/api/v1/slack/applications/setup":
            assert request.method == "POST"
            assert body == {"identity_id": identity_id, "provisioning_workspace_id": workspace_id}
            state["started"] = True
            return httpx.Response(202, json={"status": "pending", "provisioning_workspace_id": workspace_id})
        if path == "/api/v1/slack/connections":
            assert request.url.params["identity_id"] == identity_id
            state["polls"] += 1
            status = "needs_credentials" if not state["started"] else "pending" if state["polls"] < 3 else "ready"
            connections = []
            if state["installed"]:
                connections.append({
                    "id": connection_id, "identity_id": identity_id, "workspace_id": "TEXAMPLE",
                    "workspace_name": "Example workspace", "bot_user_id": "UAGENT",
                    "status": "connected", "scopes": [], "created_at": "2030-01-01T00:00:00Z",
                })
            return httpx.Response(200, json={
                "connections": connections, "installation_available": status == "ready",
                "setup": {"status": status, "provisioning_workspace_id": workspace_id if state["started"] else None},
                "application_created": status == "ready", "provisioning_workspace": saved if state["started"] else None,
            })
        if path == "/api/v1/slack/installations":
            assert request.method == "POST"
            assert body == {"identity_id": identity_id, "workspace_id": "TEXAMPLE"}
            state["installed"] = True
            return httpx.Response(201, json={
                "expires_at": "2030-01-01T00:15:00Z", "authorization_url": "https://example.com/install?state=example",
            })
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(handle))
    assert wizard._configure_slack("runtime-key", "https://example.com", "agent", Inkbox) is True
    assert state["started"] and state["installed"]
    assert state["saves"] == (2 if reject_first else 1 if new_workspace else 0)
    assert setup.env.read_text() == "INKBOX_SLACK_ENABLED=true\n"
    assert sum(path.endswith("/installations") for _, path in requests) == 1
    out = capsys.readouterr().out
    assert "Slack connected: Example workspace" in out
    assert all(secret not in out for secret in ["runtime-key", "xoxe.xoxp-synthetic-access", "xoxe-synthetic-refresh",
                                               "invalid-access", "invalid-refresh"])
