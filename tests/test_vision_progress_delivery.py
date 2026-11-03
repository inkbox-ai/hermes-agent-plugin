"""Legacy vision progress stays local without swallowing trusted final replies."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from inkbox_plugin.adapter import InkboxAdapter, _host_final_reply, _is_hermes_admin_notice


PROGRESS = "👁️ Looking at the image Describe what is visibly in this image."


@pytest.mark.parametrize("body", [PROGRESS, "👁 Looking at the image Describe the image briefly to confirm..."])
@pytest.mark.parametrize("approval", [None, False, "true", 1, True])
def test_known_vision_producer_requires_boolean_approval(body, approval):
    assert _is_hermes_admin_notice(body, {"is_approval_prompt": approval}) is (approval is not True)


@pytest.mark.parametrize("body", ["👁️ A blue bird", "Looking at the image shows a bird", "👁️ Looking at the imagery"])
def test_other_eye_text_is_not_progress(body):
    assert not _is_hermes_admin_notice(body)


@pytest.mark.parametrize("final", [None, "answer", "rendered", "host", "other_turn"])
@pytest.mark.parametrize("notice", [None, "tool_progress"])
def test_vision_send_preserves_only_matching_final(final, notice):
    async def run():
        adapter = object.__new__(InkboxAdapter)
        chat = "whatsapp:synthetic"
        turn = {"id": "inbound-1", "route": {"mode": "whatsapp", "chat_id": chat}}
        if final == "answer":
            turn["answer"] = PROGRESS
        if final == "rendered":
            turn["rendered"] = {"text_content": PROGRESS}
        adapter._native_turns = NS(rows={"key": {"chat_id": chat, "turns": [turn]}},
                                  send=AsyncMock(return_value=NS(success=True, message_id="answer")))
        adapter._conversation_state = Mock(return_value=NS(row=lambda _: {"routes": {}}))
        adapter._stop_imessage_typing_for_chat = Mock()
        token = _host_final_reply.set((chat, "inbound-1" if final == "host" else "other"))
        try:
            result = await adapter.send(chat, PROGRESS, reply_to="inbound-1",
                                        metadata={"notice_type": notice} if notice else None)
        finally:
            _host_final_reply.reset(token)
        delivered = final in {"answer", "rendered", "host"} and notice is None
        assert adapter._native_turns.send.await_count == int(delivered)
        assert result.message_id == ("answer" if delivered else "suppressed-admin-notice")
    asyncio.run(run())
