import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _live_email_module():
    path = Path(__file__).parent / "live" / "test_email_intelligence.py"
    spec = importlib.util.spec_from_file_location("live_email_intelligence_helper", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Messages:
    def __init__(self):
        self.sent = False
        self.bodies = {
            "confirmation": "Sent — emailed you everything on file.",
            "details": "Ada Lovelace, ada@example.com, +1 555 111 2222",
        }

    def list(self, _mailbox, direction=None, start_datetime=None):
        if not self.sent:
            return []
        return [
            SimpleNamespace(
                id="confirmation",
                thread_id="request-thread",
                from_address="agent@example.com",
                subject="Re: request",
            ),
            SimpleNamespace(
                id="details",
                thread_id="tool-thread",
                from_address="agent@example.com",
                subject="Your details",
            ),
        ]

    def send(self, _mailbox, **kwargs):
        self.sent = True
        return SimpleNamespace(thread_id="request-thread")

    def get(self, _mailbox, message_id):
        return SimpleNamespace(body_text=self.bodies[message_id])


def test_ask_accepts_separate_tool_email_after_generic_confirmation(monkeypatch):
    live_email = _live_email_module()
    monkeypatch.setattr(live_email, "POLL_EVERY_S", 0)
    inkbox = ModuleType("inkbox")
    mail = ModuleType("inkbox.mail")
    mail_types = ModuleType("inkbox.mail.types")
    mail_types.MessageDirection = SimpleNamespace(INBOUND="inbound")
    monkeypatch.setitem(sys.modules, "inkbox", inkbox)
    monkeypatch.setitem(sys.modules, "inkbox.mail", mail)
    monkeypatch.setitem(sys.modules, "inkbox.mail.types", mail_types)
    remote = SimpleNamespace(messages=_Messages())

    body = live_email._ask(
        remote,
        "agent@example.com",
        "driver@example.com",
        "Who am I?",
        accept=lambda candidate: (
            "ada lovelace" in candidate and "+1 555 111 2222" in candidate
        ),
    )

    assert body == "ada lovelace, ada@example.com, +1 555 111 2222"


def test_ask_bounds_history_without_losing_pages_or_baseline_exclusions(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from inkbox.mail.resources.messages import MessagesResource

    live_email = _live_email_module()
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    boundary = (now - timedelta(minutes=5)).isoformat()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.current

    Clock.current = now
    monkeypatch.setattr(live_email, "datetime", Clock)
    monkeypatch.setattr(live_email, "POLL_EVERY_S", 0)

    def row(number, sender="other@example.com"):
        return {
            "id": str(UUID(int=number)), "mailbox_id": str(UUID(int=900)),
            "thread_id": str(UUID(int=901)), "message_id": f"message-{number}",
            "from_address": sender, "to_addresses": ["driver@example.com"],
            "subject": "reply", "direction": "inbound", "status": "received",
            "is_read": False, "is_starred": False, "has_attachments": False,
            "created_at": boundary,
        }

    baseline = [row(i) for i in range(1, 102)]
    baseline[-1] = row(101, "agent@example.com")
    answer = row(102, "agent@example.com")

    class Transport:
        sent = False

        def __init__(self):
            self.pages = []
            self.body_reads = []
            self.polls = 0

        def get(self, path, params=None):
            if path.endswith("/messages"):
                # Unfiltered reads would scan lifetime history. The actual SDK
                # must retain the inclusive bound on every cursor page.
                assert params["start_datetime"] == boundary
                offset = int(params["cursor"] or 0)
                self.pages.append((self.sent, offset))
                if self.sent and offset == 0:
                    self.polls += 1
                rows = baseline + ([answer] if self.polls > 1 else [])
                page = rows[offset:offset + 50]
                more = offset + 50 < len(rows)
                return {"items": page, "has_more": more,
                        "next_cursor": str(offset + 50) if more else None}
            message_id = path.rsplit("/", 1)[-1]
            self.body_reads.append(message_id)
            # The old baseline email also matches the content predicate. It
            # must remain excluded even though it shares the boundary time.
            if message_id == baseline[-1]["id"]:
                return {**baseline[-1], "body_text": "expected stale answer"}
            assert message_id == answer["id"]
            return {**answer, "body_text": "expected fresh answer"}

        def post(self, path, **kwargs):
            self.sent = True
            return row(103)

    transport = Transport()
    monkeypatch.setattr(live_email.time, "sleep",
                        lambda _seconds: setattr(Clock, "current", now + timedelta(minutes=10)))
    body = live_email._ask(
        SimpleNamespace(messages=MessagesResource(transport)),
        "agent@example.com", "driver@example.com", "Question",
        accept=lambda candidate: "expected" in candidate,
    )

    assert body == "expected fresh answer"
    assert transport.body_reads == [answer["id"]]
    assert transport.pages == [(False, 0), (False, 50), (False, 100),
                               (True, 0), (True, 50), (True, 100),
                               (True, 0), (True, 50), (True, 100)]
