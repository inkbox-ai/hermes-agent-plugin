"""Best-effort cleanup for the live email suite.

After the suite runs, delete the artifacts it created so nothing accumulates in
the test identities: the ``smoke-*`` email threads (in both mailboxes) and any
contact the suite seeded (identified by its distinctive test name, so a
pre-existing real contact is never touched). All best-effort — cleanup never
fails the suite.
"""

from __future__ import annotations

import os

import pytest

_REMOTE = os.environ.get("REMOTE_INKBOX_API_KEY")
_AUT = os.environ.get("HERMES_INKBOX_API_KEY")
_BASE = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
_SEED_NAMES = {"penny", "tester"}  # marker for suite-created contacts


def _sweep_threads(client) -> None:
    try:
        email = client.mailboxes.list()[0].email_address
    except Exception:
        return
    seen: set = set()
    try:
        for i, m in enumerate(client.messages.list(email)):
            if i > 80:
                break
            tid = getattr(m, "thread_id", None)
            if tid and tid not in seen and "smoke-" in (getattr(m, "subject", "") or ""):
                seen.add(tid)
                try:
                    client.threads.delete(email, tid)
                except Exception:
                    pass
    except Exception:
        pass


@pytest.fixture(scope="module", autouse=True)
def _cleanup_live_artifacts():
    yield
    # Opt out (e.g. to inspect a run in the Inkbox console) by setting
    # LIVE_KEEP_ARTIFACTS=1 — the test emails + seeded contact are then left behind.
    if os.environ.get("LIVE_KEEP_ARTIFACTS", "").lower() in ("1", "true", "yes"):
        print("[live-cleanup] LIVE_KEEP_ARTIFACTS set — keeping test emails + contacts")
        return
    if not (_REMOTE and _AUT):
        return
    try:
        from inkbox import Inkbox
    except Exception:
        return
    remote = Inkbox(api_key=_REMOTE, base_url=_BASE)
    aut = Inkbox(api_key=_AUT, base_url=_BASE)

    print("[live-cleanup] sweeping smoke-* threads from both mailboxes + seeded contacts")
    _sweep_threads(remote)
    _sweep_threads(aut)

    # Delete only contacts this suite seeded (distinctive test name).
    try:
        remote_email = remote.mailboxes.list()[0].email_address
        for c in aut.contacts.lookup(email=remote_email):
            names = {
                (getattr(c, "given_name", "") or "").lower(),
                (getattr(c, "family_name", "") or "").lower(),
            }
            if names & _SEED_NAMES:
                try:
                    aut.contacts.delete(c.id)
                except Exception:
                    pass
    except Exception:
        pass
