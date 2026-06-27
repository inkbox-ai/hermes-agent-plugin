"""Live voice-call driver: the peer on the other end of a real phone call.

Opens an Inkbox tunnel for the driver identity, serves the call-media WebSocket
behind it, and bridges audio in Inkbox STT/TTS mode (text frames only — no local
model). It speaks one scripted line so the agent under test gets a turn, and the
call transcript (read separately by the test) proves the agent replied.

Run as a standalone process alongside the gateway. On startup it writes a small
JSON state file (its public WS URL + phone-number id) that the test reads to place
or expect a call. Two call directions are supported by the same bridge:
  * the test places a call to the agent and passes this driver's WS URL, or
  * the agent calls this driver's number, which is set to auto-accept onto the
    same WS URL.

Env:
  REMOTE_INKBOX_API_KEY   driver identity key (identity-scoped)
  INKBOX_BASE_URL         API root (default https://inkbox.ai)
  VOICE_DRIVER_PORT       local port the tunnel forwards to (default 8090)
  VOICE_DRIVER_STATE      path to write the JSON state file
  VOICE_DRIVER_LINE       the one line the driver speaks (default below)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketState

from inkbox import Inkbox
from inkbox.tunnels.client import connect as tunnel_connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s driver %(message)s")
log = logging.getLogger("voice_driver")

API_KEY = os.environ["REMOTE_INKBOX_API_KEY"]
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
PORT = int(os.environ.get("VOICE_DRIVER_PORT", "8090"))
STATE_FILE = os.environ.get("VOICE_DRIVER_STATE", "/tmp/voice_driver_state.json")
LINE = os.environ.get(
    "VOICE_DRIVER_LINE",
    "Hi, this is a quick test call. Please reply out loud with one short sentence, then say goodbye.",
)
# Speak shortly after the pipeline is ready so the agent's greeting lands first.
SPEAK_AFTER_S = float(os.environ.get("VOICE_DRIVER_SPEAK_AFTER", "3"))
# Then give the agent a turn and hang up — a dropped WS does NOT end the call, so we
# must send an explicit stop or the leg lingers until the server max-duration cap.
LISTEN_S = float(os.environ.get("VOICE_DRIVER_LISTEN", "12"))

app = FastAPI()


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.websocket("/phone/media/ws")
async def phone_media_ws(ws: WebSocket) -> None:
    """Accept the call-media WS in Inkbox STT/TTS mode and run one scripted turn."""
    import asyncio

    # Opt into Inkbox-managed speech both ways → we exchange text, not audio.
    await ws.accept(headers=[
        (b"x-use-inkbox-text-to-speech", b"true"),
        (b"x-use-inkbox-speech-to-text", b"true"),
    ])
    log.info("call WS accepted")
    spoke = asyncio.Event()
    convo: asyncio.Task | None = None

    async def _speak(text: str) -> None:
        if spoke.is_set():
            return
        spoke.set()
        await ws.send_text(json.dumps({"event": "text", "delta": text}))
        await ws.send_text(json.dumps({"event": "text", "done": True}))
        log.info("spoke: %s", text)

    async def _run_turn() -> None:
        # Speak one line, give the agent a turn, then hang up so the call ends fast.
        await asyncio.sleep(SPEAK_AFTER_S)
        await _speak(LINE)
        await asyncio.sleep(LISTEN_S)
        try:
            await ws.send_text(json.dumps({"event": "stop"}))
            log.info("sent stop (hangup)")
        except Exception:
            pass

    try:
        while True:
            raw = await ws.receive_text()
            ev = json.loads(raw)
            kind = ev.get("event")
            if kind == "start":
                log.info("call start: %s", ev.get("stream_id"))
                convo = asyncio.create_task(_run_turn())
            elif kind == "transcript" and ev.get("is_final"):
                log.info("heard (final): %s", ev.get("text"))
                await _speak(LINE)  # speak now if the greeting beat our timer
            elif kind == "stop":
                log.info("call stop: %s", ev.get("reason"))
                break
    except Exception as exc:  # noqa: BLE001 — never let the bridge crash the process
        log.info("WS loop ended: %r", exc)
    finally:
        if convo:
            convo.cancel()
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except Exception:
                pass


def _run_uvicorn() -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, name="uvicorn", daemon=True).start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("uvicorn did not start")


def main() -> None:
    client = Inkbox(api_key=API_KEY, base_url=BASE_URL)
    handle = client.mailboxes.list()[0].email_address.split("@", 1)[0]   # tunnel name = handle
    num = client.phone_numbers.list()[0]
    log.info("driver identity %s number %s", handle, num.number)

    server = _run_uvicorn()

    listener = tunnel_connect(
        client, name=handle, forward_to=f"http://127.0.0.1:{PORT}",
        state_dir=f"/tmp/inkbox-tunnel-{handle}",
    )
    public_host = listener.tunnel.public_host
    ws_url = f"wss://{public_host}/phone/media/ws"
    log.info("tunnel ready: %s", ws_url)

    # Auto-accept inbound calls (agent → driver) straight onto this WS.
    prev_action = getattr(num, "incoming_call_action", None)
    client.phone_numbers.update(num.id, incoming_call_action="auto_accept", client_websocket_url=ws_url)

    Path(STATE_FILE).write_text(json.dumps({
        "ws_url": ws_url, "number": num.number, "number_id": str(num.id), "handle": handle,
    }))
    log.info("state written to %s", STATE_FILE)

    try:
        listener.wait()
    finally:
        # Leave the number as we found it so other suites aren't affected.
        try:
            client.phone_numbers.update(num.id, incoming_call_action=prev_action or "auto_reject")
        except Exception as exc:  # noqa: BLE001
            log.info("number revert failed: %r", exc)
        listener.close()
        server.should_exit = True


if __name__ == "__main__":
    main()
