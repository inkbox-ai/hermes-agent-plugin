"""Deterministic OpenAI-compatible mock for live agent tests.

Hermes' ``openai-api`` provider honours ``OPENAI_BASE_URL``, so pointing it at
this server makes the agent "think" here instead of against real OpenAI: no real
key, no tokens, no flakiness, fully deterministic. We still exercise the entire
real pipeline (gateway, plugin, inbound routing, agent loop, Inkbox send +
delivery) — only the LLM brain is faked.

Every reply contains ``REPLY_OK`` plus, when present, the inbound's smoke nonce,
so a live test can assert the canned content travelled inbound → model → reply →
delivery end to end (and that the agent did NOT fall back to an error message).

Run: ``python mock_openai.py [port]`` (default 8088). Stdlib only.
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_NONCE = re.compile(r"smoke-[0-9a-f]{6,}")


def _reply_text(req: dict) -> str:
    m = _NONCE.search(json.dumps(req))
    tag = m.group(0) if m else "no-nonce"
    return f"REPLY_OK {tag} — automated reachability reply from the agent."


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):  # quiet
        pass

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802  (model-probe + health)
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(200, {"object": "list", "data": [
                {"id": "mock-model", "object": "model", "owned_by": "mock"},
            ]})
        else:
            self._send_json(200, {"ok": True})

    def do_POST(self):  # noqa: N802  (chat completions)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            req = {}
        text = _reply_text(req)
        model = req.get("model", "mock-model")
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self._send_json(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
