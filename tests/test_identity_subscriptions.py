"""Receiver coverage, conflict recovery, and configuration preservation."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from webhook_subscriptions import reconcile_identity_subscription

URL = "https://agent.example/webhook"
EVENTS = ["message.received", "text.received", "imessage.received", "call.ended", "a2a.task.created"]


def row(id="one", events=None, **extra):
    values = dict(id=id, url=URL, event_types=events or list(EVENTS), agent_identity_id="agent", owner_identity_id="agent",
                  status="active", revision=1, has_auth_token=False, auth_token=None, context_config=None)
    values.update(extra)
    return SimpleNamespace(**values)


class Conflict(Exception):
    status_code = 409


class Subscriptions:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.created = []
        self.updates = []
        self.race = None

    def list(self, **kwargs):
        assert kwargs == {"agent_identity_id": "agent"}
        return [SimpleNamespace(**vars(r)) for r in self.rows]

    def create(self, **kwargs):
        self.created.append(kwargs)
        if self.race:
            race, self.race = self.race, None
            race()
            raise Conflict()
        created = row(events=kwargs["event_types"], **{k: v for k, v in kwargs.items() if k != "event_types"})
        self.rows.append(created)
        return created

    def update(self, id, **kwargs):
        self.updates.append((id, kwargs))
        if self.race:
            race, self.race = self.race, None
            race()
            raise Conflict()
        target = next(r for r in self.rows if r.id == id)
        assert kwargs["expected_revision"] == target.revision
        target.event_types = kwargs["event_types"]
        target.revision += 1
        return target

    def delete(self, *args, **kwargs):
        raise AssertionError("Reconciliation must never delete another subscription")


def reconcile(subs):
    return reconcile_identity_subscription(SimpleNamespace(webhooks=SimpleNamespace(subscriptions=subs)), "agent", URL, EVENTS)


def test_creates_one_identity_receiver_without_channels():
    subs = Subscriptions()
    reconcile(subs)
    reconcile(subs)
    assert subs.created == [{"agent_identity_id": "agent", "url": URL, "event_types": sorted(EVENTS)}]
    assert subs.updates == []


def test_preserves_superset_context_and_id():
    config = {"email": {"mode": "count", "count": 3}}
    subs = Subscriptions([row(events=EVENTS + ["message.sent"], context_config=config)])
    assert reconcile(subs).id == "one"
    assert subs.updates == subs.created == []
    assert subs.rows[0].context_config == config


def test_adds_missing_events_with_revision_without_removing_extras():
    subs = Subscriptions([row(events=["message.received", "message.sent"], revision=7)])
    reconcile(subs)
    assert subs.updates == [("one", {"expected_revision": 7, "event_types": sorted(EVENTS + ["message.sent"])})]


def test_adopts_legacy_coverage_without_consolidating():
    subs = Subscriptions([row("mail", ["message.received"], agent_identity_id=None), row("rest", EVENTS[1:])])
    reconcile(subs)
    assert subs.updates == subs.created == []
    assert len(subs.rows) == 2


def test_only_adds_uncovered_events_to_existing_canonical_row():
    subs = Subscriptions([row("mail", ["message.received"], agent_identity_id=None), row("rest", ["call.ended"])])
    reconcile(subs)
    assert subs.updates[0][1]["event_types"] == sorted(EVENTS[1:])


def test_unrelated_url_query_and_identity_are_never_changed():
    subs = Subscriptions([row("other", url="https://other.example/webhook"), row("query", url=URL+"?receiver=other"),
                          row("foreign", agent_identity_id="other", owner_identity_id="other")])
    reconcile(subs)
    assert len(subs.created) == 1
    assert subs.updates == []


@pytest.mark.parametrize("extra", [{"has_auth_token": True}, {"auth_token": "synthetic-delivery-auth"}])
def test_conflicting_or_unreadable_token_is_not_adopted(extra):
    subs = Subscriptions([row(**extra)])
    with pytest.raises(RuntimeError, match="authentication"):
        reconcile(subs)
    assert subs.created == subs.updates == []


def test_conflicting_received_contexts_are_not_combined():
    subs = Subscriptions([row("mail", ["message.received"], context_config={"email": {"mode": "count", "count": 3}}),
                          row("rest", EVENTS[1:])])
    with pytest.raises(RuntimeError, match="context"):
        reconcile(subs)
    assert subs.updates == []


def test_nonreceived_context_does_not_conflict_with_received_coverage():
    subs = Subscriptions([row("received", EVENTS[:3], context_config={"email": {"mode": "count", "count": 3}}),
                          row("other", EVENTS[3:])])
    reconcile(subs)
    assert subs.updates == subs.created == []


def test_patch_conflict_recomputes_union_instead_of_losing_concurrent_edit():
    subs = Subscriptions([row(events=["message.received"])])
    def race():
        subs.rows[0].event_types.append("message.sent")
        subs.rows[0].revision += 1
    subs.race = race
    reconcile(subs)
    assert subs.updates[-1][1] == {"expected_revision": 2, "event_types": sorted(EVENTS + ["message.sent"])}


def test_create_race_adopts_winner_without_deleting_it():
    subs = Subscriptions()
    subs.race = lambda: subs.rows.append(row("winner"))
    assert reconcile(subs).id == "winner"
    assert len(subs.created) == 1
    assert subs.updates == []


def test_missing_revision_fails_without_unconditional_write():
    subs = Subscriptions([row(events=["message.received"], revision=None)])
    with pytest.raises(RuntimeError, match="revision"):
        reconcile(subs)
    assert subs.updates == []


def test_persistent_conflict_is_bounded():
    subs = Subscriptions([row(events=["message.received"])])
    subs.update = Mock(side_effect=Conflict())
    with pytest.raises(RuntimeError, match="changed repeatedly"):
        reconcile(subs)
    assert subs.update.call_count == 4


def test_conditionally_extends_legacy_receiver_without_duplicate_coverage():
    subs = Subscriptions([row("legacy", ["message.received"], agent_identity_id=None)])
    reconcile(subs)
    assert subs.created == []
    assert subs.updates == [("legacy", {"expected_revision": 1, "event_types": sorted(EVENTS)})]


def test_retry_accounts_for_new_sibling_coverage():
    subs = Subscriptions([row(events=["message.received"])])
    subs.race = lambda: subs.rows.append(row("sibling", ["text.received"]))
    reconcile(subs)
    assert subs.updates[-1][1]["event_types"] == sorted(set(EVENTS) - {"text.received"})


def test_retry_rejects_concurrent_context_ambiguity():
    subs = Subscriptions([row(events=["message.received"])])
    subs.race = lambda: subs.rows.append(row("sibling", ["text.received"], context_config={"email": {"mode": "count", "count": 3}}))
    with pytest.raises(RuntimeError, match="context"):
        reconcile(subs)
    assert len(subs.updates) == 1


def test_deleted_receiver_is_not_resurrected():
    subs = Subscriptions([row("deleted", status="deleted")])
    reconcile(subs)
    assert len(subs.created) == 1
    assert subs.updates == []


@pytest.mark.parametrize("detail", ["Too many active webhook subscriptions", {"detail": "Maximum 10 subscriptions reached"}, {"code": "subscription_limit_reached"}])
def test_capacity_conflict_does_not_retry_or_modify_previous_destination(detail):
    previous = row("previous", url="https://old.example/webhook")
    subs = Subscriptions([previous])
    error = Conflict()
    error.detail = detail
    subs.create = Mock(side_effect=error)
    with pytest.raises(RuntimeError, match="capacity reached.*verified previous destination"):
        reconcile(subs)
    assert subs.create.call_count == 1
    assert subs.updates == []
    assert previous.url == "https://old.example/webhook"


def test_changed_host_does_not_claim_other_destination_or_copy_context():
    previous = row("previous", url="https://old.example/webhook", context_config={"email": {"mode": "count", "count": 3}})
    subs = Subscriptions([previous])
    reconcile(subs)
    assert subs.updates == []
    assert previous.url == "https://old.example/webhook"
    assert "context_config" not in subs.created[0]
