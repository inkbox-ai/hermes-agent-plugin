import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin.config import InkboxPluginConfig, public_call_ws_url
from inkbox_plugin.tools import _append_query_param


def test_public_call_ws_url_from_public_https_url():
    cfg = InkboxPluginConfig(public_url="https://agent.example.com")

    assert public_call_ws_url(cfg) == "wss://agent.example.com/phone/media/ws"


def test_public_call_ws_url_falls_back_to_identity_tunnel_name():
    cfg = InkboxPluginConfig(identity="demo-agent")

    assert public_call_ws_url(cfg) == "wss://demo-agent.inkboxwire.com/phone/media/ws"


def test_append_query_param_preserves_existing_query():
    out = _append_query_param("wss://agent.example.com/ws?x=1", "context_token", "abc")

    assert out == "wss://agent.example.com/ws?x=1&context_token=abc"
