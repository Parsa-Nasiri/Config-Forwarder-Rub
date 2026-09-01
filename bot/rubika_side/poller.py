"""Long-polling loop for Rubika updates."""

from __future__ import annotations

import asyncio
import logging

from ..logging_setup import get_logger
from ..storage.base import Store
from .client import RubikaClient, RubikaError

log: logging.Logger = get_logger("rubika.poller")

OFFSET_KEY = "rubika_offset"


class RubikaPoller:
    """Polls ``getUpdates`` and feeds each update to the handlers.

    Only the current leader polls: two pollers on one token would split the
    update stream between them and half the messages would vanish.
    """

    def __init__(
        self,
        client: RubikaClient,
        store: Store,
        handle_update,
        interval: float = 2.0,
        limit: int = 50,
    ) -> None:
        self.client = client
        self.store = store
        self.handle_update = handle_update
        self.interval = interval
        self.limit = limit
        self.processed = 0

    async def run(self, is_leader, should_stop) -> None:
        offset: str | None = await self.store.get_state(OFFSET_KEY)
        if offset:
            log.info("resuming updates from offset %s", offset)
        idle_ticks = 0

        while not await should_stop():
            if not await is_leader():
                await asyncio.sleep(3.0)
                continue
            try:
                updates, next_offset = await self.client.get_updates(
                    limit=self.limit, offset_id=offset
                )
            except RubikaError as exc:
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(min(30.0, self.interval * 5))
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates error: %s", exc)
                await asyncio.sleep(self.interval)
                continue

            for update in updates:
                try:
                    await self.handle_update(update)
                    self.processed += 1
                except Exception:  # noqa: BLE001 - one bad update must not kill the loop
                    log.exception("failed to handle update: %s", update)

            if next_offset and next_offset != offset:
                offset = next_offset
                await self.store.set_state(OFFSET_KEY, offset)

            # Back off gently when the feed is quiet, snap back when busy.
            if updates:
                idle_ticks = 0
                await asyncio.sleep(0.2)
            else:
                idle_ticks += 1
                await asyncio.sleep(min(self.interval * 3, self.interval + idle_ticks * 0.5))

        log.info("rubika poller stopped (%d updates processed)", self.processed)


__all__ = ["RubikaPoller", "OFFSET_KEY"]
