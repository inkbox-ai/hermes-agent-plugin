import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("inkbox_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("inkbox_plugin", pkg)

from inkbox_plugin import adapter


class _Subscriptions:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.deleted = []

    def list(self, **_owner):
        return list(self.rows)

    def create(self, **kwargs):
        desired_families = {
            event_type.split(".", 1)[0] for event_type in kwargs["event_types"]
        }
        assert all(
            row.url != kwargs["url"]
            or not desired_families
            & {
                event_type.split(".", 1)[0]
                for event_type in row.event_types
            }
            for row in self.rows
        )
        row = SimpleNamespace(
            id=f"sub-{len(self.rows) + 1}",
            url=kwargs["url"],
            event_types=list(kwargs["event_types"]),
        )
        self.rows.append(row)
        return row

    def update(self, sub_id, *, event_types, url=None):
        row = next(row for row in self.rows if row.id == sub_id)
        if url is not None:
            row.url = url
        row.event_types = list(event_types)
        return row

    def delete(self, sub_id):
        self.deleted.append(sub_id)
        self.rows = [row for row in self.rows if row.id != sub_id]


def _client(rows=()):
    subscriptions = _Subscriptions(rows)
    return (
        SimpleNamespace(
            webhooks=SimpleNamespace(subscriptions=subscriptions),
        ),
        subscriptions,
    )


def _reconcile(client, url, events, previous=None):
    return adapter._reconcile_imessage_subscription(
        client,
        "identity-1",
        desired_url=url,
        previous_webhook_url=previous,
        desired_events=events,
    )


def test_a2a_and_imessage_share_the_canonical_url():
    client, subscriptions = _client()
    base = "https://agent.example/webhook"

    _reconcile(client, base, adapter._DESIRED_A2A_EVENTS)
    _reconcile(client, base, adapter._DESIRED_IMESSAGE_EVENTS)

    assert [(row.url, tuple(row.event_types)) for row in subscriptions.rows] == [
        (base, adapter._DESIRED_A2A_EVENTS),
        (base, adapter._DESIRED_IMESSAGE_EVENTS),
    ]


def test_reconcile_keeps_one_row_per_channel_and_receiver():
    base = "https://agent.example/webhook"
    extra_a2a = SimpleNamespace(
        id="sub-a2a-extra",
        url=f"{base}?unused=true",
        event_types=list(adapter._DESIRED_A2A_EVENTS),
    )
    imessage = SimpleNamespace(
        id="sub-imessage",
        url=base,
        event_types=list(adapter._DESIRED_IMESSAGE_EVENTS),
    )
    client, subscriptions = _client([extra_a2a, imessage])

    _reconcile(client, base, adapter._DESIRED_A2A_EVENTS)

    assert subscriptions.deleted == []
    assert [(row.url, tuple(row.event_types)) for row in subscriptions.rows] == [
        (base, adapter._DESIRED_A2A_EVENTS),
        (base, adapter._DESIRED_IMESSAGE_EVENTS),
    ]
