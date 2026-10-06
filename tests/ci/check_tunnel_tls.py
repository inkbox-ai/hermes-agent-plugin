#!/usr/bin/env python3
"""Real native verifier + SDK tunnel h2 transport, using ephemeral local certificates."""
from __future__ import annotations

import asyncio
import datetime
import importlib.util
import os
from pathlib import Path
import ssl
import tempfile
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def certificates(directory: Path):
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Local test CA')])

    def builder(public, subject, is_ca):
        return (
            x509.CertificateBuilder().subject_name(subject).issuer_name(name)
            .public_key(public).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_encipherment=not is_ca, key_cert_sign=is_ca,
                crl_sign=is_ca, content_commitment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ), critical=True)
        )

    ca = builder(ca_key.public_key(), name, True).sign(ca_key, hashes.SHA256())
    leaf = (
        builder(key.public_key(), x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')]), False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    ca_path, cert_path, key_path = (directory / name for name in ('ca.pem', 'cert.pem', 'key.pem'))
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)
    return ca_path, cert_path, key_path


async def check(plugin: Path, directory: Path):
    from agent.ssl_verify import install_truststore
    import truststore
    from inkbox.tunnels.client import _runtime, _tls

    # Server fixture only: truststore is a client verifier, not a TLS server.
    from truststore._ssl_constants import _original_SSLContext
    ca, cert, key = certificates(directory / 'trusted')
    unrelated_ca, _, _ = certificates(directory / 'unrelated')
    server_ctx = _original_SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    server_ctx.set_alpn_protocols(['h2'])
    sni, received, contexts = [], [], []
    server_ctx.set_servername_callback(lambda _socket, name, _ctx: sni.append(name))
    assert install_truststore(), 'native platform verifier did not load'
    assert ssl.SSLContext is truststore.SSLContext

    spec = importlib.util.spec_from_file_location('inkbox_tls_smoke', plugin / 'tunnel_tls.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_factory = _tls.create_default_verify_context
    assert module.install_tunnel_tls_compatibility(), 'inspected SDK factory was not adapted'
    assert module.install_tunnel_tls_compatibility(), 'adapter is not idempotent'
    assert _runtime.create_default_verify_context is _tls.create_default_verify_context
    assert original_factory is not _tls.create_default_verify_context

    async def accept(reader, writer):
        try:
            received.append(await asyncio.wait_for(reader.readexactly(24), 5))
            writer.write(b'OK')
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    default_context = ssl.create_default_context
    open_connection = asyncio.open_connection

    def capture_context(*args, **kwargs):
        context = default_context(*args, **kwargs)
        contexts.append(context)
        return context

    server = await asyncio.start_server(accept, '127.0.0.1', 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]

    async def redirect(*, host, port: int, ssl, server_hostname):
        assert port == 443 and host == server_hostname
        assert ssl is contexts[-1], 'native context was replaced'
        assert ssl.check_hostname and ssl.verify_mode == __import__('ssl').CERT_REQUIRED
        return await open_connection('127.0.0.1', server.sockets[0].getsockname()[1], ssl=ssl, server_hostname=server_hostname)

    assert port > 0
    empty_store = directory / 'empty-store'
    empty_store.mkdir()
    try:
        async with server:
            for label, bundle, hostname, reject in (
                ('trusted', ca, 'localhost', False),
                ('wrong-host', ca, 'wrong.invalid', True),
                ('untrusted', unrelated_ca, 'localhost', True),
            ):
                runtime = _runtime.TunnelRuntime(
                    tunnel_id='local-fixture', api_key='synthetic-not-sent', zone=hostname,
                    public_host='localhost', pool_size=1, forward_to='http://127.0.0.1:1', tls_terminator=None,
                )
                conn = _runtime._Connection(1)
                with patch.dict(os.environ, {'SSL_CERT_FILE': str(bundle), 'SSL_CERT_DIR': str(empty_store)}), \
                        patch.object(ssl, 'create_default_context', capture_context), \
                        patch.object(_runtime.asyncio, 'open_connection', redirect):
                    try:
                        await asyncio.wait_for(runtime._open_connection(conn), 5)
                    except ssl.SSLCertVerificationError:
                        assert reject, f'{label} unexpectedly rejected'
                    else:
                        try:
                            assert not reject, f'{label} unexpectedly accepted'
                            assert conn.writer.get_extra_info('ssl_object').selected_alpn_protocol() == 'h2'
                            assert await asyncio.wait_for(conn.reader.readexactly(2), 5) == b'OK'
                        finally:
                            conn.writer.close()
                            await conn.writer.wait_closed()
                print(f'native SDK tunnel TLS: {label} {"rejected" if reject else "h2 accepted"}')
            assert received == [b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n']
            assert sni == ['localhost', 'wrong.invalid', 'localhost']
            assert ssl.SSLContext is truststore.SSLContext
            assert all(c.check_hostname and c.verify_mode == ssl.CERT_REQUIRED for c in contexts)
            print('native SDK tunnel TLS: original contexts, custom roots, SNI and h2 preserved')
    finally:
        # Relevant when invoked in a larger contract process, not only as a script.
        _tls.create_default_verify_context = original_factory
        _runtime.create_default_verify_context = original_factory


def main():
    plugin = Path(os.environ.get('INKBOX_TLS_TEST_PLUGIN', str(Path(__file__).resolve().parents[2])))
    with tempfile.TemporaryDirectory(prefix='inkbox-tls-') as directory:
        asyncio.run(check(plugin, Path(directory)))


if __name__ == '__main__':
    main()
