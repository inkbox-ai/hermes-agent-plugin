"""Dispatch durable managed deliveries through the existing channel handlers."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from .managed_inbox import ManagedInbox
from .managed_work import DeliveryWork, scope

logger = logging.getLogger(__name__)


class ManagedIngress:
    def __init__(self, gateway, activation):
        self.gateway = gateway
        key = hashlib.sha256(f'{gateway._base_url}|{gateway._identity_id}'.encode()).hexdigest()
        root = Path(os.getenv('HERMES_HOME') or Path.home() / '.hermes') / 'inkbox'
        self.inbox = ManagedInbox(root / 'managed' / key / 'events.sqlite3', activation)
        self.tasks = []

    def start(self):
        self.tasks = [asyncio.create_task(self.loop()) for _ in range(4)]
        self.tasks.append(asyncio.create_task(self.loop(protocol_only=True)))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.inbox.close()

    async def loop(self, *, protocol_only=False):
        while True:
            claimed = self.inbox.claim(protocol_only=protocol_only)
            if claimed is None:
                await asyncio.sleep(0.25)
                continue
            event_id, payload = claimed
            completed = False
            try:
                async with asyncio.timeout(86400):
                    if payload.get('companion') is not None:
                        await self.gateway._companion.accept(payload)
                        completed = True
                    else:
                        work = DeliveryWork()
                        with scope((work,)):
                            response = await self.gateway._route_inkbox_event(payload['event_type'], payload)
                        work.seal()
                        completed = 200 <= response.status < 300 and await work.done
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.warning("Managed delivery outcome is unknown (%s)", type(error).__name__)
                completed = False
            finally:
                self.inbox.finish(event_id, completed=completed)
