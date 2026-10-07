#!/usr/bin/env python3
"""Credential-free boot proof after the real installer and native plugin enable.

Only the remote boundary is a loopback HTTP fixture. The published launcher,
plugin loader/admission, SDK, gateway adapter, runtime status and readiness
helper are real. This is not a replacement for live tunnel/model/channel E2E.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class LocalAPI(BaseHTTPRequestHandler):
    requests: list[str] = []
    unexpected: list[str] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        self.requests.append(path)
        if path == '/api/v1/identities/offline-smoke':
            data = {
                'id': '11111111-1111-4111-8111-111111111111', 'organization_id': 'offline',
                'agent_handle': 'offline-smoke', 'created_at': '2026-01-01T00:00:00+00:00',
                'updated_at': '2026-01-01T00:00:00+00:00',
            }
        elif path == '/api/v1/identities/offline-smoke/a2a/tasks':
            data = {'items': [], 'next_cursor': None}
        elif path in {'/api/v1/models', '/v1/models', '/models', '/api/tags', '/v1/props', '/props', '/version'}:
            # Native provider detection probes these optional metadata routes.
            # There is deliberately no model server in this startup-only fixture.
            self.send_error(404)
            return
        else:
            self.unexpected.append(path)
            self.send_error(404)
            return
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        # Hermes also uses POST for Ollama's read-only model-metadata probe.
        if urlparse(self.path).path == '/api/show':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length)) if 0 < length <= 1024 else None
            except (ValueError, UnicodeError):
                body = None
            if body == {'name': 'mock-model'}:
                self.send_error(404)
                return
        self.reject_mutation()

    def reject_mutation(self):
        self.unexpected.append('mutation')
        self.send_error(405)

    do_PUT = reject_mutation
    do_PATCH = reject_mutation
    do_DELETE = reject_mutation


def main() -> None:
    if any(os.environ.get(name) for name in ('INKBOX_API_KEY', 'HERMES_INKBOX_API_KEY', 'REMOTE_INKBOX_API_KEY', 'OPENAI_API_KEY')):
        raise RuntimeError('native install smoke must run without real credentials')
    home = Path(os.environ['HERMES_HOME'])
    launcher = home / 'hermes-agent/.hermes/bin/hermes'
    scripts = Path(__file__).resolve().parent
    subprocess.run(
        [sys.executable, str(scripts / 'hermes_python.py'), str(scripts / 'check_tunnel_tls.py')],
        env={**os.environ, 'INKBOX_TLS_TEST_PLUGIN': str(home / 'plugins/inkbox')}, check=True, timeout=45,
    )
    server = ThreadingHTTPServer(('127.0.0.1', 0), LocalAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    env = {
        **os.environ, 'INKBOX_API_KEY': 'offline-fixture-only', 'INKBOX_IDENTITY': 'offline-smoke',
        'INKBOX_SIGNING_KEY': 'offline-signature-fixture', 'INKBOX_BASE_URL': base,
        'INKBOX_PUBLIC_URL': 'http://127.0.0.1:8765', 'INKBOX_SKIP_WEBHOOK_RECONCILE': 'true',
        'INKBOX_REALTIME_ENABLED': 'false', 'OPENAI_API_KEY': 'offline-no-model-request',
        'OPENAI_BASE_URL': base + '/v1',
    }
    # Native config commands are part of the same setup path as live jobs.
    for key, value in [('model.provider', 'custom'), ('model.base_url', base + '/v1'), ('model.default', 'mock-model')]:
        subprocess.run([str(launcher), 'config', 'set', key, value], env=env, check=True, capture_output=True, timeout=30)
    gateway = None
    try:
        with (home / 'native-install-smoke.log').open('w') as log:
            gateway = subprocess.Popen([str(launcher), 'gateway', 'run'], env=env, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if gateway.poll() is not None:
                    raise RuntimeError('native gateway exited before readiness; inspect local smoke log')
                result = subprocess.run(
                    [sys.executable, str(scripts / 'hermes_python.py'), str(scripts / 'check_gateway_ready.py'), str(gateway.pid)],
                    env=env, capture_output=True, timeout=20,
                )
                if result.returncode == 0:
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('native gateway never published matching connected status')
            # Native process-identity recognition is stronger than PID liveness.
            probe = home / 'native-process-proof.py'
            probe.write_text('from gateway.status import _looks_like_gateway_process\n'
                             f'assert _looks_like_gateway_process({gateway.pid})\n')
            subprocess.run([sys.executable, str(scripts / 'hermes_python.py'), str(probe)], env=env, check=True, timeout=20)
    finally:
        if gateway is not None and gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=20)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    if (LocalAPI.unexpected or '/api/v1/identities/offline-smoke' not in LocalAPI.requests
            or LocalAPI.requests.count('/api/v1/identities/offline-smoke/a2a/tasks') < 2):
        raise RuntimeError('native SDK fixture did not complete expected reads, or attempted an unexpected request')
    print('native installer smoke: real gateway ready; native process identity verified; loopback SDK reads verified')


if __name__ == '__main__':
    main()
