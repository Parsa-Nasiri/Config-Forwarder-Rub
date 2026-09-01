"""Leader election.

GitHub Actions hands over by starting the next run *before* the current one
ends, so for a short window two processes are alive. Only the one holding the
Supabase lease is allowed to poll Rubika, read Telegram and send messages -
otherwise every user would get everything twice.

The lease is a compare-and-swap in Postgres (``acquire_runner_lock``) with a
TTL, so a runner that dies without releasing it is replaced automatically.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..storage.base import Store

log: logging.Logger = logging.getLogger("runner.leader")

LOCK_NAME = "tele2rubika"


class LeaderLock:
    def __init__(self, store: Store, instance: str, ttl: int = 90) -> None:
        self.store = store
        self.instance = instance
        self.ttl = ttl
        self._is_leader = False
        self.leader_since: float | None = None
        self._releasing = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def __call__(self) -> bool:
        """Usable directly as the ``is_leader`` predicate passed to workers."""
        return self._is_leader

    @property
    def uptime(self) -> float:
        return time.time() - self.leader_since if self.leader_since else 0.0

    async def try_acquire(self) -> bool:
        if self._releasing:
            self._is_leader = False
            return False
        try:
            ok = await self.store.acquire_lock(
                LOCK_NAME,
                self.instance,
                self.ttl,
                meta={"pid_time": time.time()},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("lock acquire failed: %s", exc)
            return self._is_leader
        if ok and not self._is_leader:
            self.leader_since = time.time()
            log.info("this instance is now the leader (%s)", self.instance)
        elif not ok and self._is_leader:
            log.warning("leadership lost - standing by")
            self.leader_since = None
        self._is_leader = ok
        return ok

    async def release(self) -> None:
        """Voluntarily hand over (called just before a graceful exit)."""
        self._releasing = True
        self._is_leader = False
        self.leader_since = None
        await self.store.release_lock(LOCK_NAME, self.instance)
        log.info("leadership released - standby can take over immediately")

    async def run(self, should_stop) -> None:
        """Background renewal loop."""
        renew_every = max(5.0, self.ttl / 3)
        while not await should_stop():
            if self._releasing:
                await self._sleep(5.0, should_stop)
                continue
            if self._is_leader:
                await self.try_acquire()
                await self._sleep(renew_every, should_stop)
            else:
                got = await self.try_acquire()
                # Standby retries fast so the handoff gap stays sub-second.
                await self._sleep(renew_every if got else 3.0, should_stop)
        if self._is_leader:
            await self.release()

    async def _sleep(self, seconds: float, should_stop) -> None:
        """Sleep in small slices so a stop request is honoured quickly."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or await should_stop():
                return
            await asyncio.sleep(min(1.0, remaining))


__all__ = ["LeaderLock", "LOCK_NAME"]
