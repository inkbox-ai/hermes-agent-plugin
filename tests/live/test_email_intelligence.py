"""Live intelligence suite over email — the agent's REAL brain + tools.

Runs against a real OpenAI model (``LIVE_REAL_MODEL=1``, real key) so it proves
the agent actually reasons and uses its Inkbox tools/data — not a mock. A remote
identity emails questions; we verify the replies against values looked up live
through the API keys (NO hardcoded expectations):

  * basic        — answers a simple question (sanity).
  * own identity — reports its own handle / email / phone (looked up via the AUT key).
  * sender       — reports who the sender is, from the contact card it can see
                   (looked up via the AUT key).
  * tools        — names its real Inkbox tools (derived from plugin.yaml).
  * contact CRUD — with LIVE_CONTACT_CRUD=1, creates/updates/deletes a
                   temporary contact through the real agent loop.

Skipped unless both keys + LIVE_REAL_MODEL=1 are set.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable

import pytest

REMOTE_KEY = os.environ.get("REMOTE_INKBOX_API_KEY")
AUT_KEY = os.environ.get("HERMES_INKBOX_API_KEY")
BASE_URL = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
TIMEOUT_S = float(os.environ.get("LIVE_EMAIL_TIMEOUT", "150"))
POLL_EVERY_S = 5.0
ERROR_MARKERS = ("non-retryable error", "missing authentication", "http 401", "http 403", "traceback")

pytestmark = pytest.mark.skipif(
    not (REMOTE_KEY and AUT_KEY and os.environ.get("LIVE_REAL_MODEL") == "1"),
    reason="real-model intelligence suite: needs both keys + LIVE_REAL_MODEL=1",
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _phone_present(phone: str, body: str) -> bool:
    """True if the agent reported ``phone`` in ``body``.

    Accepts either the full number (all digits present) or a privacy-masked
    form the model tends to emit in formal identity listings, where it keeps a
    leading prefix + the last 4 and masks the middle (e.g. ``+192****3235``).
    The masked branch requires a run of mask chars immediately followed by the
    real last-4, so it won't false-match on markdown bold (``**name:**``).
    """
    want = _digits(phone)
    if want[-10:] in _digits(body):
        return True
    tail = re.escape(want[-4:])
    return bool(re.search(r"[*xX•·]{2,}\D{0,2}" + tail, body))


def _mailbox(client) -> str:
    boxes = client.mailboxes.list()
    assert boxes, "identity has no mailbox"
    return boxes[0].email_address


def _first_phone(client) -> str | None:
    nums = client.phone_numbers.list()
    return nums[0].number if nums else None


def _client(key):
    from inkbox import Inkbox

    return Inkbox(api_key=key, base_url=BASE_URL)


def _manifest_tool_names() -> list[str]:
    import yaml

    manifest = yaml.safe_load(Path(__file__).resolve().parents[2].joinpath("plugin.yaml").read_text())
    return [t for t in manifest.get("provides_tools", []) if isinstance(t, str)]


def _ask(
    remote,
    aut_email: str,
    remote_email: str,
    question: str,
    accept: Callable[[str], bool] | None = None,
) -> str:
    """Email the agent and return a matching message created after the request.

    Hermes may answer in-thread or send the substantive content through the
    email tool before emitting a generic confirmation. Inspect all new mail
    from the AUT so assertions follow the requested content, not delivery style.
    """
    from inkbox.mail.types import MessageDirection

    def _inbound():
        return list(remote.messages.list(remote_email, direction=MessageDirection.INBOUND))

    before = {str(msg.id) for msg in _inbound()}
    nonce = f"smoke-{uuid.uuid4().hex[:8]}"
    sent = remote.messages.send(remote_email, to=[aut_email], subject=f"[{nonce}] {question[:40]}", body_text=question)
    thread_id = str(getattr(sent, "thread_id", "") or "")

    def _is_reply(msg) -> bool:
        if thread_id and str(getattr(msg, "thread_id", "") or "") == thread_id:
            return True
        frm = (getattr(msg, "from_address", "") or "").lower()
        return aut_email.lower() in frm and nonce in (getattr(msg, "subject", "") or "")

    seen: set[str] = set()
    candidates: list[str] = []
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        for msg in _inbound():
            msg_id = str(msg.id)
            if msg_id in before or msg_id in seen:
                continue
            sender = (getattr(msg, "from_address", "") or "").lower()
            if aut_email.lower() not in sender:
                continue
            seen.add(msg_id)
            body = getattr(remote.messages.get(remote_email, msg.id), "body_text", "") or ""
            lowered = body.lower()
            bad = [m for m in ERROR_MARKERS if m in lowered]
            assert not bad, f"reply is an error, not a real answer: {bad}\n{body[:300]}"
            candidates.append(body)
            if (accept is None and _is_reply(msg)) or (accept is not None and accept(lowered)):
                return lowered
        time.sleep(POLL_EVERY_S)
    previews = "\n---\n".join(body[:500] for body in candidates) or "(none)"
    pytest.fail(
        f"no acceptable reply within {TIMEOUT_S:.0f}s to: {question!r}\n"
        f"new emails from AUT:\n{previews}"
    )


@pytest.fixture(scope="module")
def ctx():
    remote = _client(REMOTE_KEY)
    aut = _client(AUT_KEY)
    return {
        "remote": remote,
        "aut": aut,
        "remote_email": _mailbox(remote),
        "aut_email": _mailbox(aut),
    }


def test_basic_reply(ctx):
    body = _ask(ctx["remote"], ctx["aut_email"], ctx["remote_email"],
                "Please reply with a one-sentence acknowledgement that you received this email.")
    assert len(body.strip()) > 0, "empty reply"


def test_reports_own_identity(ctx):
    aut = ctx["aut"]
    handle = _mailbox(aut).split("@", 1)[0]
    aut_email = ctx["aut_email"]
    aut_phone = _first_phone(aut)
    assert aut_phone, "AUT identity has no phone number to report"

    body = _ask(
        ctx["remote"], aut_email, ctx["remote_email"],
        "What is your full Inkbox identity? Reply with your handle, display "
        "name, email address, and phone number. Write the phone number in "
        "full — every digit, with no masking, asterisks, or abbreviation.",
        accept=lambda candidate: (
            handle in candidate and aut_email in candidate and _phone_present(aut_phone, candidate)
        ),
    )
    assert handle in body, f"reply missing handle {handle!r}\n{body[:400]}"
    assert aut_email in body, f"reply missing email {aut_email!r}\n{body[:400]}"
    # Accept a privacy-masked phone (the model self-redacts the middle digits in
    # formal listings even with Hermes secret redaction off) as well as full.
    assert _phone_present(aut_phone, body), f"reply missing phone {aut_phone!r}\n{body[:400]}"


def test_reports_sender_details(ctx):
    """The agent must report who the sender is, from the contact card it can see."""
    aut, remote = ctx["aut"], ctx["remote"]
    remote_email = ctx["remote_email"]

    # Look up (or seed) the sender's contact in the AUT org — the card the agent sees.
    matches = aut.contacts.lookup(email=remote_email)
    if not matches:
        from inkbox.contacts.types import ContactEmail, ContactPhone
        rphone = _first_phone(remote)
        aut.contacts.create(
            given_name="Penny",
            family_name="Tester",
            emails=[ContactEmail(label="work", value=remote_email)],
            phones=[ContactPhone(label="mobile", value=rphone)] if rphone else None,
        )
        matches = aut.contacts.lookup(email=remote_email)
    assert matches, "could not establish a contact card for the sender"
    contact = matches[0]
    name = (getattr(contact, "preferred_name", None) or getattr(contact, "given_name", None) or "")
    emails = [e.value for e in getattr(contact, "emails", [])]
    phones = [p.value for p in getattr(contact, "phones", [])]

    body = _ask(
        ctx["remote"], ctx["aut_email"], remote_email,
        "Who am I to you? Tell me everything you have on file about me. "
        "Include my phone number in full — every digit, with no masking, "
        "asterisks, or abbreviation.",
        accept=lambda candidate: (
            (not name or name.lower() in candidate)
            and any(e.lower() in candidate for e in emails)
            and (not phones or any(_phone_present(p, candidate) for p in phones))
        ),
    )
    if name:
        assert name.lower() in body, f"reply missing sender name {name!r}\n{body[:400]}"
    assert any(e.lower() in body for e in emails), f"reply missing sender email {emails}\n{body[:400]}"
    if phones:
        # Accept full or privacy-masked (see _phone_present).
        assert any(_phone_present(p, body) for p in phones), \
            f"reply missing sender phone {phones}\n{body[:400]}"


def test_aware_of_inkbox_tools(ctx):
    """Non-LLM proof the agent is wired with real tools: it names them."""
    tool_names = _manifest_tool_names()
    assert tool_names, "plugin.yaml provides_tools is empty"
    contact_tools = {
        "inkbox_lookup_contact",
        "inkbox_list_contacts",
        "inkbox_get_contact",
        "inkbox_create_contact",
        "inkbox_update_contact",
        "inkbox_delete_contact",
    }
    assert contact_tools <= set(tool_names)
    assert "inkbox_export_contact_vcard" not in tool_names

    body = _ask(
        ctx["remote"], ctx["aut_email"], ctx["remote_email"],
        "List the exact names of all the Inkbox tools you have access to, one per line.",
        accept=lambda candidate: all(t.lower() in candidate for t in contact_tools),
    )
    hits = [t for t in tool_names if t.lower() in body]
    assert len(hits) >= 3, f"agent named only {hits} of its tools {tool_names}\n{body[:500]}"
    missing_contacts = sorted(t for t in contact_tools if t.lower() not in body)
    assert not missing_contacts, f"agent did not name contact tools {missing_contacts}\n{body[:500]}"
    assert "inkbox_export_contact_vcard" not in body


def _contacts_by_email(client, email: str):
    return list(client.contacts.lookup(email=email) or [])


def _delete_contacts_by_email(client, email: str) -> None:
    for contact in _contacts_by_email(client, email):
        contact_id = str(getattr(contact, "id", "") or "")
        if contact_id:
            client.contacts.delete(contact_id)


@pytest.mark.skipif(
    os.environ.get("LIVE_CONTACT_CRUD") != "1",
    reason="mutating contact CRUD live test: set LIVE_CONTACT_CRUD=1 to opt in",
)
def test_contact_crud_tool_use(ctx):
    """The real agent can reason about and use contact write tools end to end."""
    aut = ctx["aut"]
    nonce = f"hermes-live-{uuid.uuid4().hex[:8]}"
    contact_name = f"Hermes Live {nonce}"
    contact_email = f"{nonce}@example.com"
    updated_notes = f"updated-notes-{nonce}"

    _delete_contacts_by_email(aut, contact_email)
    try:
        created = _ask(
            ctx["remote"],
            ctx["aut_email"],
            ctx["remote_email"],
            "Use inkbox_create_contact now. Create a new contact named "
            f"{contact_name} with email {contact_email}. Do not just describe the action. "
            f"After the tool succeeds, reply exactly: CREATED {nonce}",
            accept=lambda candidate: "created" in candidate and nonce in candidate,
        )
        assert "created" in created and nonce in created, created[:500]
        matches = _contacts_by_email(aut, contact_email)
        assert matches, f"agent said it created {contact_email}, but lookup found nothing"
        contact_id = str(getattr(matches[0], "id", "") or "")
        assert contact_id, f"created contact has no id: {matches[0]!r}"

        updated = _ask(
            ctx["remote"],
            ctx["aut_email"],
            ctx["remote_email"],
            "Use inkbox_update_contact now. Update contactId "
            f"{contact_id} and set notes to {updated_notes}. Do not create a second contact. "
            f"After the tool succeeds, reply exactly: UPDATED {nonce}",
            accept=lambda candidate: "updated" in candidate and nonce in candidate,
        )
        assert "updated" in updated and nonce in updated, updated[:500]
        fetched = aut.contacts.get(contact_id)
        assert updated_notes.lower() in str(getattr(fetched, "notes", "") or "").lower()

        deleted = _ask(
            ctx["remote"],
            ctx["aut_email"],
            ctx["remote_email"],
            "I confirm this is a temporary test contact. Use inkbox_delete_contact now "
            f"to delete contactId {contact_id}. After the tool succeeds, reply exactly: DELETED {nonce}",
            accept=lambda candidate: "deleted" in candidate and nonce in candidate,
        )
        assert "deleted" in deleted and nonce in deleted, deleted[:500]
        assert not _contacts_by_email(aut, contact_email)
    finally:
        _delete_contacts_by_email(aut, contact_email)
