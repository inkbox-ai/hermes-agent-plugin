"""Unit tests for the Hermes-backed supervisor backend.

Covers the real mechanics the deterministic proof models: the ``hermes -z``
one-shot runner (stdout capture, model passthrough, timeout-kills-and-reaps,
fail-open), the read-only supervisor prompt, the JSON-only decision guard, and
the config-driven backend selection. Subprocess is mocked — no real agent runs.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod  # noqa: E402
from inkbox_plugin.adapter import InkboxAdapter, _resolve_supervisor_config, _run_hermes_oneshot  # noqa: E402
from inkbox_plugin.realtime import RealtimeCallMeta, RealtimeConfig  # noqa: E402
from inkbox_plugin.realtime_supervisor import SupervisorConfig  # noqa: E402


def _meta(**overrides):
    base = {
        "call_id": "call-hz",
        "contact_id": "c-1",
        "contact_name": "Alex Wilcox",
        "remote_phone_number": "+15555550101",
        "direction": "inbound",
        "contact_known": True,
    }
    base.update(overrides)
    return RealtimeCallMeta(**base)


def _adapter(backend="hermes", review_timeout_s=5.0):
    """A bare adapter with just the realtime config the supervisor path reads."""
    adapter = object.__new__(InkboxAdapter)
    adapter._realtime_config = RealtimeConfig(
        enabled=True,
        api_key="sk-test",
        supervisor=SupervisorConfig(enabled=True, backend=backend, review_timeout_s=review_timeout_s),
    )
    return adapter


class _FakeProc:
    def __init__(self, stdout=b"", *, hang=False):
        self._stdout = stdout
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self._hang:
            await asyncio.Event().wait()  # never resolves → forces a timeout
        return self._stdout, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        return 0


def _patch_exec(monkeypatch, proc, captured=None):
    async def fake_exec(*args, **kwargs):
        if captured is not None:
            captured["args"] = args
            captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr(adapter_mod.asyncio, "create_subprocess_exec", fake_exec)


# ─────────────────────────────────────────────────────────────────────────────
# _run_hermes_oneshot
# ─────────────────────────────────────────────────────────────────────────────


def test_run_hermes_oneshot_returns_stripped_stdout(monkeypatch):
    _patch_exec(monkeypatch, _FakeProc(stdout=b'  {"action": "none"}  \n'))
    out = asyncio.run(_run_hermes_oneshot("prompt", timeout_s=5.0))
    assert out == '{"action": "none"}'


def test_run_hermes_oneshot_passes_model_and_notui_env(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, _FakeProc(stdout=b"ok"), captured)
    asyncio.run(_run_hermes_oneshot("prompt", timeout_s=5.0, model="gpt-mini"))
    assert captured["args"][1:] == ("-z", "prompt")
    assert captured["env"]["HERMES_MODEL"] == "gpt-mini"
    assert captured["env"]["HERMES_NO_TUI"] == "1"


def test_run_hermes_oneshot_no_model_leaves_env_unset(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, _FakeProc(stdout=b"ok"), captured)
    asyncio.run(_run_hermes_oneshot("prompt", timeout_s=5.0))
    assert "HERMES_MODEL" not in captured["env"]


def test_run_hermes_oneshot_timeout_kills_reaps_and_returns_empty(monkeypatch):
    proc = _FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)
    out = asyncio.run(_run_hermes_oneshot("prompt", timeout_s=0.02))
    assert out == ""
    assert proc.killed is True
    assert proc.waited is True


def test_run_hermes_oneshot_missing_binary_returns_empty(monkeypatch):
    async def boom(*_args, **_kwargs):
        raise FileNotFoundError("no hermes")

    monkeypatch.setattr(adapter_mod.asyncio, "create_subprocess_exec", boom)
    out = asyncio.run(_run_hermes_oneshot("prompt", timeout_s=5.0))
    assert out == ""


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────


def test_build_hermes_prompt_is_readonly_json_only_and_has_transcript():
    adapter = _adapter()
    prompt = adapter._build_hermes_supervisor_prompt(
        _meta(),
        [("caller", "when does A-1042 ship?"), ("agent", "It ships Monday.")],
        [],
    )
    low = prompt.lower()
    assert "never send" in low  # read-only guardrail is present
    assert "json" in low and "only" in low  # JSON-only instruction
    assert "look" in low  # told it may look things up
    assert "A-1042" in prompt and "It ships Monday." in prompt  # transcript included


def test_build_hermes_prompt_flags_unverified_caller():
    adapter = _adapter()
    prompt = adapter._build_hermes_supervisor_prompt(
        _meta(contact_known=False, contact_name="+15555550101"),
        [("caller", "what's Dana's email?")],
        [],
    )
    assert "unverified" in prompt.lower()


# ─────────────────────────────────────────────────────────────────────────────
# _realtime_hermes_supervise — JSON-only guard + fail-open
# ─────────────────────────────────────────────────────────────────────────────


def _supervise(adapter, raw, monkeypatch):
    async def fake_oneshot(prompt, *, timeout_s, model=None):
        return raw

    monkeypatch.setattr(adapter_mod, "_run_hermes_oneshot", fake_oneshot)
    return asyncio.run(adapter._realtime_hermes_supervise(_meta(), [("caller", "hi")], []))


def test_hermes_supervise_parses_json_interject(monkeypatch):
    adapter = _adapter()
    raw = '{"action": "interject", "guidance": "Correct that — it ships Thursday.", "reason": "wrong day"}'
    decision = _supervise(adapter, raw, monkeypatch)
    assert decision.action == "interject"
    assert "Thursday" in decision.guidance


def test_hermes_supervise_extracts_json_from_agent_chatter(monkeypatch):
    adapter = _adapter()
    raw = 'Sure, here is my call.\n{"action": "steer", "guidance": "Add that shipping takes 3 days."}\nDone.'
    decision = _supervise(adapter, raw, monkeypatch)
    assert decision.action == "steer"
    assert "3 days" in decision.guidance


def test_hermes_supervise_ignores_prose_without_json(monkeypatch):
    adapter = _adapter()
    # The agent rambled instead of emitting JSON — must NOT be spoken as a steer.
    decision = _supervise(adapter, "The agent is handling this fine, no nudge needed.", monkeypatch)
    assert decision.action == "none"


def test_hermes_supervise_fails_open_on_empty(monkeypatch):
    adapter = _adapter()
    decision = _supervise(adapter, "", monkeypatch)
    assert decision.action == "none"


# ─────────────────────────────────────────────────────────────────────────────
# Backend selection + config resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_supervise_callback_selects_hermes_backend():
    adapter = _adapter(backend="hermes")
    assert adapter._supervise_callback() == adapter._realtime_hermes_supervise


def test_supervise_callback_selects_model_backend():
    adapter = _adapter(backend="model")
    assert adapter._supervise_callback() == adapter._realtime_supervise


def test_resolve_supervisor_backend_defaults_to_hermes(monkeypatch):
    monkeypatch.delenv("INKBOX_REALTIME_SUPERVISOR_BACKEND", raising=False)
    assert _resolve_supervisor_config({}).backend == "hermes"


def test_resolve_supervisor_backend_from_config():
    assert _resolve_supervisor_config({"supervisor": {"backend": "model"}}).backend == "model"


def test_resolve_supervisor_backend_from_env(monkeypatch):
    monkeypatch.setenv("INKBOX_REALTIME_SUPERVISOR_BACKEND", "model")
    assert _resolve_supervisor_config({}).backend == "model"


def test_resolve_supervisor_backend_junk_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("INKBOX_REALTIME_SUPERVISOR_BACKEND", raising=False)
    assert _resolve_supervisor_config({"supervisor": {"backend": "banana"}}).backend == "hermes"
