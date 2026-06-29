import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import tools  # noqa: E402


class FakeContacts:
    def __init__(self):
        self.lookup_calls = []
        self.list_calls = []
        self.get_calls = []

    def lookup(self, **kwargs):
        self.lookup_calls.append(kwargs)
        return [{"id": "contact-1", "preferred_name": "Alex Example"}]

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return [{"id": "contact-2", "preferred_name": "Alex Mercer"}]

    def get(self, contact_id):
        self.get_calls.append(contact_id)
        return {
            "id": contact_id,
            "preferred_name": "Alex Example",
            "emails": [{"value": "alex@example.com", "is_primary": True}],
            "phones": [{"value": "+15555550123", "is_primary": True}],
        }


def test_contact_read_tools(monkeypatch):
    contacts = FakeContacts()
    client = types.SimpleNamespace(contacts=contacts)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, client, None))

    lookup = json.loads(tools.inkbox_lookup_contact({"emailDomain": "example.com"}))
    listed = json.loads(tools.inkbox_list_contacts({"q": "Alex", "order": "name", "limit": 5, "offset": 1}))
    fetched = json.loads(tools.inkbox_get_contact({"contactId": "contact-1"}))

    assert lookup["ok"] is True
    assert lookup["count"] == 1
    assert contacts.lookup_calls == [{"email_domain": "example.com"}]

    assert listed["ok"] is True
    assert listed["contacts"][0]["preferred_name"] == "Alex Mercer"
    assert contacts.list_calls == [{"q": "Alex", "order": "name", "limit": 5, "offset": 1}]

    assert fetched["ok"] is True
    assert fetched["contact"]["emails"][0]["value"] == "alex@example.com"
    assert contacts.get_calls == ["contact-1"]


def test_lookup_contact_requires_one_filter(monkeypatch):
    client = types.SimpleNamespace(contacts=FakeContacts())
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, client, None))

    none = json.loads(tools.inkbox_lookup_contact({}))
    too_many = json.loads(tools.inkbox_lookup_contact({"email": "a@example.com", "phone": "+15555550123"}))

    assert "exactly one" in none["error"]
    assert "exactly one" in too_many["error"]
