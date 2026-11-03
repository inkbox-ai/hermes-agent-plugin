"""WhatsApp reconciliation sees identity-wide subscriptions without changing shared rows."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from inkbox_plugin.adapter import _reconcile_imessage_subscription, InkboxAPIError
from inkbox_plugin.whatsapp import EVENTS

URL = "https://agent.example/webhook"


def row(name, events, url=URL):
    return NS(id=name, event_types=list(events), url=url)


def reconcile(rows, *, race=False):
    subscriptions = Mock()
    seen = 0

    def listing(**kwargs):
        nonlocal seen
        seen += 1
        # Legacy identity selectors omit this family and mixed receivers.
        if kwargs.get("scope") != "identity" or (race and seen == 1):
            return []
        return rows

    subscriptions.list.side_effect = listing
    subscriptions.create.side_effect = InkboxAPIError(status_code=409, detail="Events already subscribed")
    client = NS(webhooks=NS(subscriptions=subscriptions))
    return subscriptions, lambda: _reconcile_imessage_subscription(
        client, "identity-1", desired_url=URL, previous_webhook_url=None, desired_events=EVENTS)


@pytest.mark.parametrize("race", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_existing_whatsapp_receiver_is_visible_and_adopted(race, mixed):
    active = row("active", (*EVENTS, "a2a.task.created") if mixed else EVENTS)
    other = row("other", ["imessage.received"])
    shared = row("shared", ["whatsapp.sent", "a2a.task.created"], URL + "?shared=1")
    subscriptions, run = reconcile([active, other, shared], race=race)
    assert run() == "active"
    assert subscriptions.create.call_count == int(race)
    subscriptions.update.assert_not_called()
    subscriptions.delete.assert_not_called()
    assert all(call.kwargs == {"agent_identity_id": "identity-1", "scope": "identity"}
               for call in subscriptions.list.call_args_list)


def test_incomplete_shared_receiver_is_not_truncated():
    shared = row("shared", ["whatsapp.sent", "a2a.task.created"])
    subscriptions, run = reconcile([shared])
    with pytest.raises(InkboxAPIError):
        run()
    subscriptions.update.assert_not_called()
    subscriptions.delete.assert_not_called()
