"""Non-destructive reconciliation for an identity's notification receiver."""

from __future__ import annotations

import json


_RECEIVED = {"message.received", "text.received", "imessage.received"}


def _context(row):
    config = getattr(row, "context_config", None)
    return {key: value for key, value in (config or {}).items() if value is not None} or None


def reconcile_identity_subscription(client, identity_id, url, events):
    """Preserve receiver coverage and retry conditional writes from fresh state."""
    subscriptions = client.webhooks.subscriptions
    desired = set(events)
    for attempt in range(4):
        rows = [
            row for row in subscriptions.list(agent_identity_id=identity_id)
            if row.url == url
            and str(getattr(row, "owner_identity_id", None) or getattr(row, "agent_identity_id", None)) == str(identity_id)
            and getattr(row, "status", "active") == "active"
        ]
        compatible = []
        for row in rows:
            if getattr(row, "has_auth_token", False) or getattr(row, "auth_token", None) is not None:
                if desired.intersection(row.event_types):
                    raise RuntimeError("Webhook receiver has conflicting delivery authentication; review its subscriptions.")
                continue
            compatible.append(row)
        contexts = {
            json.dumps(_context(row), sort_keys=True)
            for row in compatible if _RECEIVED.intersection(row.event_types)
        }
        if len(contexts) > 1:
            raise RuntimeError("Webhook receiver has conflicting context settings; review its subscriptions.")
        context = json.loads(next(iter(contexts))) if contexts else None
        covered = {event for row in compatible for event in row.event_types}
        missing = desired - covered
        compatible.sort(key=lambda row: (str(getattr(row, "agent_identity_id", None)) != str(identity_id), str(row.id)))
        if not missing:
            return compatible[0]
        target = next((row for row in compatible if _context(row) == context), None)
        try:
            if target is not None:
                revision = getattr(target, "revision", None)
                if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                    raise RuntimeError("Webhook revision is unavailable; upgrade the Inkbox SDK and retry.")
                return subscriptions.update(
                    target.id,
                    event_types=sorted(set(target.event_types) | missing),
                    expected_revision=revision,
                )
            kwargs = {"agent_identity_id": identity_id, "url": url, "event_types": sorted(missing)}
            if context is not None:
                kwargs["context_config"] = context
            return subscriptions.create(**kwargs)
        except Exception as exc:
            if getattr(exc, "status_code", None) not in (404, 409) or attempt == 3:
                raise
    raise RuntimeError("Webhook subscriptions changed repeatedly; retry setup.")
