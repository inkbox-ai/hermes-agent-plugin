"""Remote tool registrations bound to native turn lifetimes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import logging
import threading
import time

from .config import inkbox_client_kwargs, inkbox_state_path, read_runtime_config
from .remote_compat import COMPATIBILITY_NAMES, compatibility_arguments

logger = logging.getLogger(__name__)


class RemoteTools:
    def __init__(self, ctx):
        self.ctx = ctx
        self.handles = {}
        self.compatibility = {}
        self.remote = None
        self.catalog = []
        self.turns = {}
        self.refreshed_at = 0.0
        self.lock = threading.RLock()

    async def discover(self):
        from inkbox.remote_mcp import RemoteMcp
        from inkbox import Inkbox

        cfg = read_runtime_config()
        if not cfg.api_key or not cfg.identity:
            return None, []
        client = Inkbox(**inkbox_client_kwargs(cfg.api_key, cfg.base_url))
        try:
            account = await asyncio.wait_for(asyncio.to_thread(client.whoami), 5)
            if account.auth_subtype in {"api_key.admin_scoped", "api_key.agent_scoped.unclaimed"}:
                return None, []
            if account.auth_subtype != "api_key.agent_scoped.claimed":
                raise RuntimeError("This credential type does not support remote tools")
            identity = await asyncio.wait_for(asyncio.to_thread(client.get_identity, cfg.identity), 5)
            remote = RemoteMcp(
                api_key=cfg.api_key,
                base_url=client._base_url,
                identity=str(identity.id),
                journal_path=inkbox_state_path().parent / "remote_calls" / "catalog.sqlite",
            )
            return remote, await remote.refresh()
        finally:
            client.close()

    def publish(self, remote, catalog):
        from . import terminal_reply
        from tools.registry import registry

        if not self.compatibility:
            self.compatibility = {
                name: registry.get_entry(name) for name in COMPATIBILITY_NAMES if registry.get_entry(name) is not None
            }
        names = {tool["name"] for tool in catalog}
        if terminal_reply.NAME in names:
            raise RuntimeError("A remote tool conflicts with a delivery helper")
        if names & {"inkbox_email_reply", "inkbox_text_send"}:
            catalog = [*catalog, terminal_reply.DESCRIPTOR]
        for name in {tool["name"] for tool in catalog}:
            if name not in self.handles and registry.get_entry(name) is not None:
                raise RuntimeError("A remote tool conflicts with an existing tool")
        staged = {}
        try:
            for tool in catalog:
                name = tool["name"]
                schema = {"name": name, "description": tool.get("description", ""), "parameters": tool["inputSchema"]}
                if "annotations" in tool:
                    schema["annotations"] = tool["annotations"]
                if "outputSchema" in tool:
                    schema["outputSchema"] = tool["outputSchema"]
                handle = self.ctx.register_tool(name, "inkbox", schema, self.handler(name))
                if handle is None:
                    raise RuntimeError("The native host could not register a remote tool")
                staged[name] = handle
            if remote is not None:
                for name, entry in self.compatibility.items():
                    if COMPATIBILITY_NAMES[name] not in names:
                        continue
                    schema = {
                        **entry.schema,
                        "description": entry.schema.get("description", "")
                        + f" Compatibility name for {COMPATIBILITY_NAMES[name]}.",
                    }
                    handle = self.ctx.register_tool(name, "inkbox", schema, self.handler(name))
                    if handle is None:
                        raise RuntimeError("The native host could not register a compatibility tool")
                    staged[name] = handle
        except BaseException:
            for handle in reversed(list(staged.values())):
                handle.dispose()
            raise
        previous = self.handles
        self.handles, self.remote, self.catalog = staged, remote, catalog
        for handle in reversed(list(previous.values())):
            handle.dispose()
        self.refreshed_at = time.monotonic()

    def begin(self, session_id="", turn_id="", platform="", **kwargs):
        if not session_id or not turn_id:
            return {"context": "Inkbox remote tools require a native host with turn identifiers."}
        key = (str(session_id), str(turn_id))
        with self.lock:
            if key in self.turns:
                return None
            if len(self.turns) >= 512:
                return {"context": "Inkbox remote tools are unavailable until active turns finish."}
            warning = None
            if time.monotonic() - self.refreshed_at >= 300:
                try:
                    self.publish(*asyncio.run(self.discover()))
                except Exception:
                    logger.warning("Inkbox remote tool discovery unavailable")
                    warning = {
                        "context": "Inkbox remote tool discovery is unavailable. Existing remote calls still require current authorization."
                    }
            names = {tool["name"] for tool in self.catalog}
            if self.remote is not None:
                names.update(name for name in self.compatibility if COMPATIBILITY_NAMES[name] in names)
            from .conversation import reply_route

            self.turns[key] = (self.remote, frozenset(names), str(platform), deepcopy(reply_route.get() or {}))
        try:
            from .capabilities import refresh_host_tools

            refresh_host_tools(session_id=str(session_id), current_session_only=True)
        except Exception:
            logger.warning("Inkbox native tool catalog refresh unavailable")
            warning = {"context": "Inkbox tools changed, but this session could not refresh its tool catalog."}
        return warning

    def end(self, session_id="", turn_id="", **kwargs):
        with self.lock:
            self.turns.pop((str(session_id), str(turn_id)), None)

    def approve(self, tool_name="", session_id="", turn_id="", **kwargs):
        turn = self.turns.get((str(session_id), str(turn_id)))
        if turn is None or tool_name not in turn[1]:
            return None
        if turn[2] == "inkbox":
            return None
        return {"action": "approve", "message": "Run this Inkbox tool?"}

    def handler(self, name):
        from . import terminal_reply
        def call(arguments, **kwargs):
            try:
                from tools.approval_context import _approval_session_id, _approval_turn_id, _approval_tool_call_id

                values = [_approval_session_id.get(), _approval_turn_id.get(), _approval_tool_call_id.get()]
                if not all(isinstance(value, str) and 1 <= len(value) <= 256 for value in values):
                    raise RuntimeError("The native host did not supply a stable tool-call identity")
                key = tuple(values[:2])
                turn = self.turns.get(key)
                if turn is None or turn[0] is None or name not in turn[1]:
                    raise RuntimeError("The tool is unavailable for this native turn")

                def validate():
                    if self.turns.get(key) is not turn:
                        raise RuntimeError("The native turn ended before this tool operation")

                operation = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
                journal = (
                    inkbox_state_path().parent
                    / "remote_calls"
                    / (hashlib.sha256(values[0].encode()).hexdigest() + ".sqlite")
                )
                from model_tools import _run_async

                if name == terminal_reply.NAME:

                    async def reply():
                        from inkbox import Inkbox

                        cfg = read_runtime_config()
                        client = Inkbox(**inkbox_client_kwargs(cfg.api_key, cfg.base_url))
                        try:
                            return await terminal_reply.send(
                                turn[0],
                                client,
                                cfg.identity,
                                terminal_reply.route_source(turn[3]),
                                arguments,
                                terminal_reply.journal_path(turn[3].get("chat_id", "")),
                                validate,
                            )
                        finally:
                            client.close()

                    return json.dumps(_run_async(reply()), separators=(",", ":"))
                result = _run_async(
                    turn[0].call(
                        COMPATIBILITY_NAMES.get(name, name),
                        compatibility_arguments(name, arguments, operation),
                        call_identity=operation,
                        before_execute=validate,
                        journal_path=journal,
                    )
                )
                return json.dumps(result, separators=(",", ":"))
            except Exception:
                return json.dumps(
                    {
                        "isError": True,
                        "content": [{"type": "text", "text": "Inkbox tool unavailable or no longer authorized"}],
                    }
                )

        return call


def register(ctx):
    remote = RemoteTools(ctx)
    ctx.register_hook("pre_llm_call", remote.begin)
    ctx.register_hook("post_llm_call", remote.end)
    ctx.register_hook("pre_tool_call", remote.approve)
