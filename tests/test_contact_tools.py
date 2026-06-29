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
        self.create_calls = []
        self.update_calls = []
        self.delete_calls = []

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

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return {"id": "contact-3", "preferred_name": kwargs.get("preferred_name") or kwargs.get("given_name")}

    def update(self, contact_id, **kwargs):
        self.update_calls.append((contact_id, kwargs))
        return {"id": contact_id, "preferred_name": kwargs.get("preferred_name")}

    def delete(self, contact_id):
        self.delete_calls.append(contact_id)
        return None


class FakeContactEmail:
    def __init__(self, *, label, value, is_primary):
        self.label = label
        self.value = value
        self.is_primary = is_primary


class FakeContactPhone:
    def __init__(self, *, label, value, is_primary):
        self.label = label
        self.value = value
        self.is_primary = is_primary


def _install_fake_inkbox_contact_types(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "inkbox",
        types.SimpleNamespace(ContactEmail=FakeContactEmail, ContactPhone=FakeContactPhone),
    )


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


def test_contact_write_tools(monkeypatch):
    contacts = FakeContacts()
    client = types.SimpleNamespace(contacts=contacts)
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, client, None))
    _install_fake_inkbox_contact_types(monkeypatch)

    created = json.loads(tools.inkbox_create_contact({
        "givenName": "Ada",
        "familyName": "Lovelace",
        "emails": ["ada@example.com"],
        "phones": [{"value": "+15555550123", "label": "mobile"}],
    }))
    updated = json.loads(tools.inkbox_update_contact({
        "contactId": "contact-3",
        "preferredName": "Ada L.",
        "emails": [{"value": "ada.l@example.com", "label": "work", "isPrimary": True}],
        "phones": [],
    }))
    deleted = json.loads(tools.inkbox_delete_contact({"contactId": "contact-3"}))

    assert created["ok"] is True
    create_payload = contacts.create_calls[0]
    assert create_payload["given_name"] == "Ada"
    assert create_payload["family_name"] == "Lovelace"
    assert create_payload["emails"][0].value == "ada@example.com"
    assert create_payload["emails"][0].is_primary is True
    assert create_payload["phones"][0].label == "mobile"
    assert create_payload["phones"][0].value == "+15555550123"

    assert updated["ok"] is True
    assert contacts.update_calls[0][0] == "contact-3"
    update_payload = contacts.update_calls[0][1]
    assert update_payload["preferred_name"] == "Ada L."
    assert update_payload["emails"][0].value == "ada.l@example.com"
    assert update_payload["phones"] == []

    assert deleted == {"ok": True, "deleted_contact_id": "contact-3"}
    assert contacts.delete_calls == ["contact-3"]


def test_lookup_contact_requires_one_filter(monkeypatch):
    client = types.SimpleNamespace(contacts=FakeContacts())
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, client, None))

    none = json.loads(tools.inkbox_lookup_contact({}))
    too_many = json.loads(tools.inkbox_lookup_contact({"email": "a@example.com", "phone": "+15555550123"}))

    assert "exactly one" in none["error"]
    assert "exactly one" in too_many["error"]


def test_contact_write_tools_validate_required_inputs(monkeypatch):
    client = types.SimpleNamespace(contacts=FakeContacts())
    monkeypatch.setattr(tools, "_client_and_identity", lambda: (None, client, None))
    _install_fake_inkbox_contact_types(monkeypatch)

    empty_create = json.loads(tools.inkbox_create_contact({}))
    empty_update = json.loads(tools.inkbox_update_contact({"contactId": "contact-1"}))
    missing_update_id = json.loads(tools.inkbox_update_contact({"givenName": "Ada"}))
    missing_delete_id = json.loads(tools.inkbox_delete_contact({}))

    assert "at least one contact field" in empty_create["error"]
    assert "at least one contact field" in empty_update["error"]
    assert "`contactId` is required" in missing_update_id["error"]
    assert "`contactId` is required" in missing_delete_id["error"]
