"""Exact SDK compatibility boundary without changing global verifier policy."""
import concurrent.futures
import asyncio
import builtins
import logging
import ssl
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from inkbox.tunnels.client import _runtime, _tls
from inkbox_plugin import tunnel_tls


@pytest.fixture(autouse=True)
def restore_factories(monkeypatch):
    # Record real originals so monkeypatch restores even direct adapter assignments.
    monkeypatch.setattr(_tls, 'create_default_verify_context', _tls.create_default_verify_context)
    monkeypatch.setattr(_runtime, 'create_default_verify_context', _runtime.create_default_verify_context)


def test_sdk_factory_is_adapted_only_when_needed_under_concurrency():
    context_class, default_factory = ssl.SSLContext, ssl.create_default_context
    original = _tls.create_default_verify_context
    needs_adapter = version('inkbox') in tunnel_tls._VERSIONS
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: tunnel_tls.install_tunnel_tls_compatibility(), range(24)))
    assert results == [needs_adapter] * 24
    expected = tunnel_tls._verify_context if needs_adapter else original
    assert _tls.create_default_verify_context is expected
    assert _runtime.create_default_verify_context is expected
    assert ssl.SSLContext is context_class and ssl.create_default_context is default_factory


@pytest.mark.parametrize('which', ['runtime', 'tls', 'both', 'future', 'fingerprint'])
def test_unknown_factories_or_versions_remain_untouched(monkeypatch, caplog, which):
    foreign = lambda: None  # noqa: E731
    if which in {'runtime', 'both'}:
        monkeypatch.setattr(_runtime, 'create_default_verify_context', foreign)
    if which in {'tls', 'both'}:
        monkeypatch.setattr(_tls, 'create_default_verify_context', foreign)
    if which == 'future':
        monkeypatch.setattr(tunnel_tls, 'version', lambda _: '0.8.0')
    if which == 'fingerprint':
        monkeypatch.setattr(tunnel_tls, '_FACTORY_SHA256', 'unknown')
    before = (_tls.create_default_verify_context, _runtime.create_default_verify_context)
    with caplog.at_level(logging.INFO):
        assert not tunnel_tls.install_tunnel_tls_compatibility()
    assert before == (_tls.create_default_verify_context, _runtime.create_default_verify_context)
    assert 'SDK TLS behavior unchanged' in caplog.text


@pytest.mark.parametrize('stats', [NotImplementedError(), {'x509_ca': 1}, {'x509_ca': 0}])
def test_retains_original_context_and_only_known_empty_loads_bundle(monkeypatch, stats):
    loaded = []
    def inspect_store():
        if isinstance(stats, Exception):
            raise stats
        return stats
    context = SimpleNamespace(cert_store_stats=inspect_store, load_verify_locations=lambda **kw: loaded.append(kw))
    monkeypatch.setattr(ssl, 'create_default_context', lambda: context)
    assert tunnel_tls._verify_context() is context
    assert bool(loaded) == (stats == {'x509_ca': 0})
    if loaded:
        import certifi
        assert loaded == [{'cafile': certifi.where()}]


@pytest.mark.parametrize('error', [ValueError('bad store'), AttributeError('bad API'), OSError('bad roots')])
def test_other_introspection_errors_propagate(monkeypatch, error):
    def inspect_store():
        raise error
    monkeypatch.setattr(ssl, 'create_default_context', lambda: SimpleNamespace(cert_store_stats=inspect_store))
    with pytest.raises(type(error), match=str(error)):
        tunnel_tls._verify_context()


def test_known_empty_bundle_failure_retains_sdk_behavior(monkeypatch):
    def load(**_kw):
        raise OSError('bundle unavailable')
    context = SimpleNamespace(cert_store_stats=lambda: {'x509_ca': 0}, load_verify_locations=load)
    monkeypatch.setattr(ssl, 'create_default_context', lambda: context)
    assert tunnel_tls._verify_context() is context


@pytest.mark.parametrize('sdk_version', ['0.8.0', '0.7.14'])
def test_missing_private_boundary_is_untouched(monkeypatch, sdk_version):
    original_import = builtins.__import__
    attempted = []
    def no_private_import(name, *args, **kwargs):
        if name == 'inkbox.tunnels.client':
            attempted.append(name)
            raise ImportError('private boundary moved')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(tunnel_tls, 'version', lambda _: sdk_version)
    monkeypatch.setattr(builtins, '__import__', no_private_import)
    before = (_tls.create_default_verify_context, _runtime.create_default_verify_context)
    assert not tunnel_tls.install_tunnel_tls_compatibility()
    assert attempted == ([] if sdk_version == '0.8.0' else ['inkbox.tunnels.client'])
    assert before == (_tls.create_default_verify_context, _runtime.create_default_verify_context)


def test_missing_factory_leaves_other_binding_untouched(monkeypatch):
    before = _runtime.create_default_verify_context
    monkeypatch.delattr(_tls, 'create_default_verify_context')
    assert not tunnel_tls.install_tunnel_tls_compatibility()
    assert _runtime.create_default_verify_context is before
    assert not hasattr(_tls, 'create_default_verify_context')


def test_gateway_supports_native_verifier_before_connecting_tunnel(monkeypatch, tmp_path):
    from inkbox_plugin import adapter as adapter_module
    gateway = adapter_module.InkboxAdapter.__new__(adapter_module.InkboxAdapter)
    gateway._identity_handle = 'local-fixture'
    gateway._tunnel_name_override = ''
    gateway._port = 8765
    gateway._inkbox = object()
    monkeypatch.setattr(adapter_module, '_inkbox_tunnel_state_dir', lambda: tmp_path / 'tunnel')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(context, 'cert_store_stats', Mock(side_effect=NotImplementedError))
    monkeypatch.setattr(ssl, 'create_default_context', lambda: context)
    calls = []
    def connect(client, **kwargs):
        assert client is gateway._inkbox
        assert _runtime.create_default_verify_context() is context
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        calls.append(kwargs)
        return SimpleNamespace(wait=lambda: None, public_url='https://localhost', tunnel=SimpleNamespace(public_host='localhost'))
    monkeypatch.setattr(adapter_module, 'inkbox_tunnel_connect', connect)
    assert asyncio.run(gateway._provision_inkbox_tunnel())
    gateway._tunnel_runtime_thread.join(timeout=2)
    assert calls == [{'name': 'local-fixture', 'forward_to': 'http://127.0.0.1:8765', 'state_dir': tmp_path / 'tunnel'}]
