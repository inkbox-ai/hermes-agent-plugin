import asyncio
import hashlib
import hmac
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter as adapter_mod
from inkbox_plugin import webhook_providers as wp
from inkbox_plugin.webhook_providers import inkbox as inkbox_provider_mod
from inkbox_plugin.adapter import InkboxAdapter


class _FakeRequest:
    def __init__(self, body, headers=None, *, request_id="req-wp-1"):
        self._body = body
        self.headers = {"X-Inkbox-Request-Id": request_id, **(headers or {})}
        self.url = "https://agent.example/webhook"

    async def read(self):
        return self._body


def _sign(body, secret, *, request_id="rid-1", timestamp="1700000000"):
    """Build real Inkbox signature headers for ``body`` (matches the SDK scheme)."""
    key = secret.removeprefix("whsec_")
    message = f"{request_id}.{timestamp}.".encode() + body
    digest = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return {
        "X-Inkbox-Signature": "sha256=" + digest,
        "X-Inkbox-Request-Id": request_id,
        "X-Inkbox-Timestamp": timestamp,
    }


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "web",
        types.SimpleNamespace(Response=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    )


def _adapter(*, require_signature, external_events_enabled):
    # Build an adapter without __init__ (no network/SDK), wiring only the state
    # the webhook auth path + external handler touch.
    adapter = object.__new__(InkboxAdapter)
    adapter._require_signature = require_signature
    adapter._external_events_enabled = external_events_enabled
    adapter._signing_key = "whsec_test"
    adapter._seen_request_ids = {}
    adapter._inflight_request_ids = {}
    adapter.platform = "inkbox"
    adapter._enqueued = []

    async def _capture(event):
        adapter._enqueued.append(event)

    adapter._enqueue = _capture
    adapter._resolve_channel_overrides = lambda *a, **k: (None, None)
    return adapter


# --- registry ------------------------------------------------------------

def test_providers_are_auto_discovered():
    # Importing the package alone registers every provider module (the drop-in
    # contract): the Inkbox provider is present without being imported by hand.
    assert "inkbox" in {p.name for p in wp.base._REGISTRY}


def test_match_provider_identifies_inkbox_by_header():
    provider = wp.match_provider({"X-Inkbox-Signature": "sha256=abc"})
    assert provider is not None and provider.name == "inkbox"


def test_match_provider_is_case_insensitive():
    provider = wp.match_provider({"x-inkbox-signature": "sha256=abc"})
    assert provider is not None and provider.name == "inkbox"


def test_match_provider_returns_none_for_unknown_source():
    # A third-party source we have not onboarded a verifier for.
    assert wp.match_provider({"X-Stripe-Signature": "t=1,v1=abc"}) is None


def test_github_provider_registered_and_matches():
    provider = wp.match_provider({"X-Hub-Signature-256": "sha256=abc"})
    assert provider is not None and provider.name == "github"


def test_github_provider_verifies_real_hmac():
    from inkbox_plugin.webhook_providers.github import GithubProvider

    provider = GithubProvider()
    body = b'{"action":"completed","conclusion":"failure"}'
    secret = "gh_webhook_secret"
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    hdr = {"X-Hub-Signature-256": good}
    assert provider.verify(body=body, headers=hdr, url="u", secret=secret) is True
    # Tamper / wrong secret / no secret → all reject.
    assert provider.verify(body=body + b"x", headers=hdr, url="u", secret=secret) is False
    assert provider.verify(body=body, headers=hdr, url="u", secret="wrong") is False
    assert provider.verify(body=body, headers=hdr, url="u", secret="") is False
    assert provider.verify(
        body=body, headers={"X-Hub-Signature-256": "nope"}, url="u", secret=secret
    ) is False


def test_inkbox_provider_delegates_to_sdk(monkeypatch):
    seen = {}

    def _fake_verify(*, payload, headers, secret):
        seen.update(payload=payload, secret=secret)
        return True

    monkeypatch.setattr(inkbox_provider_mod, "verify_webhook", _fake_verify)
    provider = inkbox_provider_mod.InkboxProvider()
    ok = provider.verify(body=b"raw", headers={}, url="u", secret="whsec_test")
    assert ok is True
    assert seen == {"payload": b"raw", "secret": "whsec_test"}


# --- adapter integration -------------------------------------------------

def test_unsigned_inkbox_typed_event_is_not_trusted_as_inkbox(monkeypatch):
    # We route on the authenticated source, not the body's claim. An unsigned
    # payload claiming "message.received" must NOT reach the Inkbox mail handler
    # — with pass-through off it is simply ignored.
    hit = {"mail": 0}

    async def _mail(_envelope):
        hit["mail"] += 1

    adapter = _adapter(require_signature=True, external_events_enabled=False)
    monkeypatch.setattr(adapter, "_on_mail_received", _mail)
    resp = asyncio.run(
        adapter._handle_webhook(_FakeRequest(b'{"event_type":"message.received"}'))
    )
    assert resp.status == 200 and resp.text == "ignored"
    assert hit["mail"] == 0
    assert adapter._enqueued == []


def test_inkbox_event_with_valid_signature_passes(monkeypatch):
    monkeypatch.setattr(inkbox_provider_mod, "verify_webhook", lambda **k: True)
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(
                b'{"event_type":"message.delivered"}',
                headers={"X-Inkbox-Signature": "sha256=good"},
            )
        )
    )
    # message.* lifecycle is a log-only 200 "ok" — proves it passed auth.
    assert resp.status == 200 and resp.text == "ok"


def test_inkbox_event_with_bad_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(inkbox_provider_mod, "verify_webhook", lambda **k: False)
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(
                b'{"event_type":"message.delivered"}',
                headers={"X-Inkbox-Signature": "sha256=bad"},
            )
        )
    )
    assert resp.status == 401


def test_unknown_source_passthrough_is_unverified_when_enabled():
    # No registered verifier + pass-through on → wake the agent even with
    # require_signature True (we cannot verify an unknown source).
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    resp = asyncio.run(
        adapter._handle_webhook(_FakeRequest(b'{"event":"prod_on_fire"}'))
    )
    assert resp.status == 200
    assert len(adapter._enqueued) == 1


def test_unknown_source_dropped_when_passthrough_disabled():
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(_FakeRequest(b'{"event":"prod_on_fire"}'))
    )
    assert resp.status == 200 and resp.text == "ignored"
    assert adapter._enqueued == []


def test_registered_third_party_is_verified(monkeypatch):
    # Simulate a future onboarded third-party verifier that rejects the request.
    fake = types.SimpleNamespace(name="acme", verify=lambda **k: False)
    monkeypatch.setattr(adapter_mod, "match_provider", lambda headers: fake)
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_ACME", "s3cret")
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(b'{"event":"charge"}', headers={"X-Acme-Signature": "bad"})
        )
    )
    assert resp.status == 401


def test_third_party_valid_signature_proceeds(monkeypatch):
    # Matched third-party + good signature → the event reaches the agent, and
    # the raw body, url, and env-resolved secret are all passed to verify().
    captured = {}

    def _verify(**kwargs):
        captured.update(kwargs)
        return True

    fake = types.SimpleNamespace(name="acme", verify=_verify)
    monkeypatch.setattr(adapter_mod, "match_provider", lambda headers: fake)
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_ACME", "s3cret")
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(b'{"event":"charge"}', headers={"X-Acme-Signature": "good"})
        )
    )
    assert resp.status == 200
    assert len(adapter._enqueued) == 1
    assert captured["secret"] == "s3cret"          # env secret reached the verifier
    assert captured["body"] == b'{"event":"charge"}'  # raw body, unparsed
    assert captured["url"] == "https://agent.example/webhook"


def test_provider_event_key_deduplicates_labels_source_and_loads_skill(monkeypatch):
    fake = types.SimpleNamespace(
        name="whoop",
        skill="whoop:whoop-coach",
        verify=lambda **kwargs: True,
        event_key=lambda **kwargs: kwargs["envelope"]["trace_id"],
    )
    monkeypatch.setattr(adapter_mod, "match_provider", lambda headers: fake)
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_WHOOP", "whoop-secret")
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    adapter._resolve_channel_overrides = lambda _modality, _chat_id, default: (None, default)

    def request():
        return _FakeRequest(
            b'{"id":"sleep-1","type":"recovery.updated","trace_id":"trace-1"}',
            headers={"X-WHOOP-Signature": "good"},
            request_id="",
        )

    first = asyncio.run(adapter._handle_webhook(request()))
    second = asyncio.run(adapter._handle_webhook(request()))

    assert first.status == 200 and first.text == "ok"
    assert second.status == 200 and second.text == "duplicate"
    assert len(adapter._enqueued) == 1
    event = adapter._enqueued[0]
    assert event.source.chat_id == "external:whoop"
    assert event.source.thread_id == "external:whoop:trace-1"
    assert event.auto_skill == "whoop:whoop-coach"
    assert "event=recovery.updated" in event.text


def test_inkbox_signed_external_shaped_event_routes_external(monkeypatch):
    # An Inkbox *signature* only means Inkbox vouched for delivery — a forwarded
    # external event (e.g. a CI escalation) is Inkbox-signed but is NOT a known
    # Inkbox event shape. It must reach the agent via the external path, not get
    # swallowed by the Inkbox handler branch. (Regression: the live
    # external-events suite signs its escalation with the Inkbox key.)
    hit = {"mail": 0, "call": 0}

    async def _mail(_e):
        hit["mail"] += 1

    async def _call(_e):
        hit["call"] += 1

    monkeypatch.setattr(inkbox_provider_mod, "verify_webhook", lambda **k: True)
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    monkeypatch.setattr(adapter, "_on_mail_received", _mail)
    monkeypatch.setattr(adapter, "_on_incoming_call", _call)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(
                b'{"event":"agent_escalation_demo","title":"prod down"}',
                headers={"X-Inkbox-Signature": "sha256=good"},
            )
        )
    )
    assert resp.status == 200
    assert hit == {"mail": 0, "call": 0}   # not routed to any Inkbox handler
    assert len(adapter._enqueued) == 1      # woke the agent as an external event


def test_inkbox_signed_unknown_dropped_when_external_events_off(monkeypatch):
    # An Inkbox-signed payload with no handler (e.g. a future Inkbox event
    # family) must NOT wake a session when external events are off — it's gated
    # by the flag, same as an unknown source. Only registered third parties
    # bypass the flag.
    monkeypatch.setattr(inkbox_provider_mod, "verify_webhook", lambda **k: True)
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(
                b'{"event_type":"contact.updated","data":{}}',
                headers={"X-Inkbox-Signature": "sha256=good"},
            )
        )
    )
    assert resp.status == 200 and resp.text == "ignored"
    assert adapter._enqueued == []


def test_unknown_source_event_carries_unverified_directive():
    # Unsigned unknown source, passed through → the enqueued event must carry
    # the cautious (do-not-act) directive as its channel_prompt.
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    asyncio.run(adapter._handle_webhook(_FakeRequest(b'{"event":"maybe_prod_fire"}')))
    assert adapter._enqueued[0].channel_prompt == adapter_mod.EXTERNAL_EVENT_UNVERIFIED_DIRECTIVE


def test_verified_thirdparty_event_carries_action_directive(monkeypatch):
    # A verified third-party event → action directive (may act on it).
    fake = types.SimpleNamespace(name="acme", verify=lambda **k: True)
    monkeypatch.setattr(adapter_mod, "match_provider", lambda headers: fake)
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_ACME", "s3cret")
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    asyncio.run(
        adapter._handle_webhook(_FakeRequest(b'{"event":"charge"}', headers={"X-Acme-Signature": "good"}))
    )
    assert adapter._enqueued[0].channel_prompt == adapter_mod.EXTERNAL_EVENT_DIRECTIVE


def test_github_valid_signature_reaches_agent(monkeypatch):
    # A GitHub-signed escalation with a VALID signature is verified and handed
    # to the agent as an external event (source=github, not a known Inkbox shape).
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_GITHUB", "gh_secret")
    body = b'{"event":"workflow_run","conclusion":"failure","summary":"call Jane Doe now"}'
    sig = "sha256=" + hmac.new(b"gh_secret", body, hashlib.sha256).hexdigest()
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    resp = asyncio.run(
        adapter._handle_webhook(_FakeRequest(body, headers={"X-Hub-Signature-256": sig}))
    )
    assert resp.status == 200
    assert len(adapter._enqueued) == 1  # verified → agent woken


def test_github_forged_signature_is_dropped(monkeypatch):
    # Same event, a FORGED signature → rejected before the agent sees anything.
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_GITHUB", "gh_secret")
    body = b'{"event":"workflow_run","conclusion":"failure","summary":"call Jane Doe now"}'
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        )
    )
    assert resp.status == 401
    assert adapter._enqueued == []  # forged → agent never woken


def test_verified_third_party_bypasses_passthrough_flag(monkeypatch):
    # A source we deliberately onboarded (provider + secret) is trusted, so its
    # events reach the agent even with external pass-through OFF — the flag only
    # gates *unverified* unknown sources.
    fake = types.SimpleNamespace(name="acme", verify=lambda **k: True)
    monkeypatch.setattr(adapter_mod, "match_provider", lambda headers: fake)
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_ACME", "s3cret")
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(b'{"event":"charge"}', headers={"X-Acme-Signature": "good"})
        )
    )
    assert resp.status == 200
    assert len(adapter._enqueued) == 1


def test_other_provider_claiming_inkbox_type_routes_external_not_mail(monkeypatch):
    # A non-Inkbox source signs a payload that *claims* "message.received".
    # Routing on the authenticated source means it goes to the external path
    # (source=github), never to the Inkbox mail handler — no spoof possible.
    hit = {"mail": 0}

    async def _mail(_envelope):
        hit["mail"] += 1

    other = types.SimpleNamespace(name="github", verify=lambda **k: True)
    monkeypatch.setattr(adapter_mod, "match_provider", lambda headers: other)
    adapter = _adapter(require_signature=True, external_events_enabled=True)
    monkeypatch.setattr(adapter, "_on_mail_received", _mail)
    resp = asyncio.run(
        adapter._handle_webhook(_FakeRequest(b'{"event_type":"message.received"}'))
    )
    assert resp.status == 200
    assert hit["mail"] == 0            # never reached the Inkbox mail handler
    assert len(adapter._enqueued) == 1  # handled as a verified external event


def test_require_signature_false_bypasses_verify():
    # Local-testing escape hatch: the source is still identified by its header
    # (real Inkbox traffic always carries it), but the signature is not checked.
    adapter = _adapter(require_signature=False, external_events_enabled=False)
    resp = asyncio.run(
        adapter._handle_webhook(
            _FakeRequest(
                b'{"event_type":"message.delivered"}',
                headers={"X-Inkbox-Signature": "sha256=unchecked"},
            )
        )
    )
    assert resp.status == 200 and resp.text == "ok"


# --- provider unit edges -------------------------------------------------

def test_register_provider_returns_class_and_registers(monkeypatch):
    monkeypatch.setattr(wp.base, "_REGISTRY", [])

    @wp.register_provider
    class _Tmp(wp.WebhookProvider):
        name = "tmp"
        provider_header = "X-Tmp"

    assert _Tmp.__name__ == "_Tmp"  # decorator is transparent
    assert [p.name for p in wp.base._REGISTRY] == ["tmp"]


def test_match_provider_first_match_wins(monkeypatch):
    a = types.SimpleNamespace(name="a", matches=lambda h: True)
    b = types.SimpleNamespace(name="b", matches=lambda h: True)
    monkeypatch.setattr(wp.base, "_REGISTRY", [a, b])
    assert wp.match_provider({}).name == "a"


def test_base_matches_false_without_provider_header():
    assert wp.WebhookProvider().matches({"X-Anything": "1"}) is False


def test_base_verify_is_abstract():
    with pytest.raises(NotImplementedError):
        wp.WebhookProvider().verify(body=b"", headers={}, url="", secret="")


def test_inkbox_provider_fails_closed_without_sdk(monkeypatch):
    # SDK absent → cannot verify → must reject, never accept.
    monkeypatch.setattr(inkbox_provider_mod, "verify_webhook", None)
    provider = inkbox_provider_mod.InkboxProvider()
    ok = provider.verify(
        body=b"x", headers={"X-Inkbox-Signature": "sha256=abc"}, url="u", secret="s"
    )
    assert ok is False


def test_inkbox_provider_real_signature_roundtrip():
    # Exercise the real SDK HMAC path (not mocked): good sig verifies, and any
    # tamper — body, secret, or dropped prefix — fails.
    if inkbox_provider_mod.verify_webhook is None:
        pytest.skip("inkbox SDK not installed")
    provider = inkbox_provider_mod.InkboxProvider()
    body = b'{"event_type":"message.received","data":{"id":"abc"}}'
    headers = _sign(body, "whsec_secret")

    assert provider.verify(body=body, headers=headers, url="u", secret="whsec_secret") is True
    assert provider.verify(body=body + b" ", headers=headers, url="u", secret="whsec_secret") is False
    assert provider.verify(body=body, headers=headers, url="u", secret="whsec_wrong") is False


# --- secret resolution ---------------------------------------------------

def test_provider_secret_inkbox_uses_signing_key():
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    assert adapter._provider_secret("inkbox") == "whsec_test"


def test_provider_secret_third_party_reads_env(monkeypatch):
    monkeypatch.setenv("INKBOX_WEBHOOK_SECRET_ACME", "from-env")
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    assert adapter._provider_secret("acme") == "from-env"


def test_provider_secret_missing_env_is_empty(monkeypatch):
    monkeypatch.delenv("INKBOX_WEBHOOK_SECRET_NOPE", raising=False)
    adapter = _adapter(require_signature=True, external_events_enabled=False)
    assert adapter._provider_secret("nope") == ""
