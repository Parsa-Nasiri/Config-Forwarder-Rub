"""The delivery brain.

Telegram channels dump thousands of configs a day; dumping them straight into
a chat is the fastest way to get muted. This module decides *what* each user
sees, *when* they see it, and *how much* of it.

Pipeline
--------

    ingest(config)
        │
        ├─ hard validity gate (private IPs, broken ports, missing uuid)
        ├─ fingerprint dedupe  ──────── already known ─┐
        ├─ optional TCP probe                          │
        ├─ score 0-100                                 │
        ├─ persist + channel reputation                │
        └─ fan-out: per-user preference filter ────────┤
                                                       │
    pending[chat_id] = [ ... scored items ... ]  ◄─────┘
                    │
                    │  adaptive batching: emit when the window closes,
                    │  the batch is full, or a "live mode" config is hot
                    ▼
            rate limiter (per chat + global)
                    │
                    ▼
            rendered digest  ──►  Rubika sendMessage
                    │
                    ├─ ok   -> mark_sent, remember batch for keypad callbacks
                    └─ fail -> exponential backoff, retry, then dead-letter

Feedback
--------
Tapping "dead" on a message penalises the config, the server it lives on and
the Telegram channel that published it - so the whole feed self-corrects over
time. Tapping copy rewards the protocol in that user's personal affinity map,
which re-ranks their future batches.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..logging_setup import get_logger
from ..storage.base import Store, User
from .health import probe
from .models import ProxyConfig
from .ratelimit import RateLimiter
from .scorer import Scorer

log: logging.Logger = get_logger("engine.dispatcher")

TICK_SECONDS = 2.0


@dataclass
class QueuedItem:
    fingerprint: str
    score: float
    protocol: str
    created_at: float = field(default_factory=time.time)
    cfg: ProxyConfig | None = None


class Dispatcher:
    """Owns the pending queues and the outbound pump."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        scorer: Scorer,
        send_message,
        build_message,
    ) -> None:
        self.settings = settings
        self.store = store
        self.scorer = scorer
        # async (chat_id, text, keypad, metadata) -> message_id | None
        self._send = send_message
        # (user, items, batch_id, seq) -> (text, keypad, metadata)
        self._build = build_message

        self.pending: dict[str, list[QueuedItem]] = {}
        self.users: dict[str, User] = {}
        self.limiter = RateLimiter(settings.rate_limit_per_chat, settings.rate_limit_global)

        # Adaptive batching state.
        self._arrival_rate = 0.0           # EMA of configs/second
        self._last_arrival = 0.0
        self._batch_seq = 0
        self._cursor = 0                   # round-robin fairness cursor

        # Counters surfaced by /stats and the heartbeat file.
        self.counters: dict[str, int] = {
            "ingested": 0, "unique": 0, "duplicates": 0,
            "rejected_low_score": 0, "delivered": 0, "failed": 0,
        }

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def hydrate(self) -> None:
        """Reload persisted queues and user list after a restart/handoff."""
        self.users = {u.chat_id: u for u in await self.store.list_users(only_deliverable=True)}
        self.pending = {}
        revived = 0
        for user in self.users.values():
            rows = await self.store.list_queued(user.chat_id, limit=200)
            if not rows:
                continue
            items: list[QueuedItem] = []
            for row in rows:
                cfg = await self._resolve(str(row["fingerprint"]))
                if cfg is None:
                    continue
                items.append(
                    QueuedItem(
                        fingerprint=str(row["fingerprint"]),
                        score=float(row.get("score") or 0),
                        protocol=cfg.protocol,
                        created_at=time.time(),
                        cfg=cfg,
                    )
                )
            if items:
                self.pending[user.chat_id] = items
                revived += len(items)
        log.info("hydrated %d users, %d queued items", len(self.users), revived)

    async def refresh_users(self) -> None:
        users = await self.store.list_users(only_deliverable=True)
        self.users = {u.chat_id: u for u in users}

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------
    async def ingest(
        self,
        cfg: ProxyConfig,
        *,
        source_channel: str = "",
        source_message: str = "",
    ) -> float:
        """Score, persist and fan out one config. Returns its score."""
        if not cfg.is_valid:
            return 0.0

        self.counters["ingested"] += 1
        now = time.time()
        gap = now - self._last_arrival
        self._last_arrival = now
        # EMA over arrival gaps -> smooth "how busy is the feed" signal.
        if gap > 0:
            inst = 1.0 / gap
            self._arrival_rate = (0.88 * self._arrival_rate) + (0.12 * min(inst, 5.0))

        fingerprint = cfg.fingerprint
        existing = await self.store.get_config(fingerprint)
        is_new = existing is None

        health_ok: bool | None = None
        latency_ms: int | None = None
        if self.settings.health_check_enabled and is_new:
            health_ok, latency_ms = await probe(cfg, self.settings.health_check_timeout)

        result = await self.scorer.score(
            cfg,
            source_channel=source_channel,
            source_count=int(existing.get("source_count") or 1) if existing else 1,
            first_seen=float(existing.get("first_seen_at") or now) if existing else now,
            dead_reports=int(existing.get("dead_reports") or 0) if existing else 0,
            live_reports=int(existing.get("live_reports") or 0) if existing else 0,
            health_ok=health_ok,
            latency_ms=latency_ms,
        )
        score = result.value

        stored_new = await self.store.add_config(
            fingerprint=fingerprint,
            protocol=cfg.protocol,
            server=cfg.server,
            port=cfg.port,
            remark=cfg.remark[:250],
            raw=cfg.raw,
            score=score,
            geo=cfg.geo,
            network=cfg.network,
            security=cfg.security,
            source_channel=source_channel,
            source_message=source_message,
        )
        if stored_new:
            self.counters["unique"] += 1
            if source_channel:
                await self.store.bump_channel(source_channel, "total", 1)
        else:
            self.counters["duplicates"] += 1
            # A re-post of a config users keep marking dead should never climb
            # back above the delivery floor.
            if int((existing or {}).get("dead_reports") or 0) >= 3:
                return score

        if score < self.settings.min_score:
            self.counters["rejected_low_score"] += 1
            return score

        await self._fan_out(cfg, fingerprint, score)
        return score

    async def _fan_out(self, cfg: ProxyConfig, fingerprint: str, score: float) -> None:
        """Drop the config into every eligible user's pending queue."""
        if not self.users:
            await self.refresh_users()
        if not self.users:
            return

        now = time.time()
        for user in list(self.users.values()):
            if not user.accepts(cfg.protocol, score):
                continue
            if self.settings.user_hourly_cap > 0:
                used = await self.store.recent_delivery_count(user.chat_id, now - 3600)
                if used >= self.settings.user_hourly_cap:
                    continue
            if not await self.store.enqueue(user.chat_id, fingerprint, score):
                continue  # this user already saw it

            affinity = float(user.affinity.get(cfg.protocol, 0.0))
            self.pending.setdefault(user.chat_id, []).append(
                QueuedItem(
                    fingerprint=fingerprint,
                    score=score + affinity,
                    protocol=cfg.protocol,
                    created_at=now,
                    cfg=cfg,
                )
            )

    # ------------------------------------------------------------------
    # the pump
    # ------------------------------------------------------------------
    def _adaptive_window(self, user: User, items: list[QueuedItem]) -> float:
        """Widen the batch window while the feed is busy, narrow it when calm.

        A channel storm becomes a handful of fat digests instead of a hundred
        separate pings; a quiet feed still feels instant.
        """
        base = self.settings.batch_window
        factor = 1.0 + (self._arrival_rate - 0.05) * 4.0
        factor = max(0.6, min(3.0, factor))
        # Cap the wait for tiny batches so nobody waits the full window for a
        # single mediocre config.
        if len(items) == 1 and items[0].score < self.settings.instant_score:
            factor = min(factor, 1.2)
        return base * factor

    def _should_flush(self, user: User, items: list[QueuedItem], now: float) -> bool:
        if not items:
            return False
        cap = min(user.max_per_batch or self.settings.batch_max, self.settings.batch_max)
        if len(items) >= cap:
            return True
        best = max(i.score for i in items)
        if user.live_mode and best >= self.settings.instant_score:
            return True
        window = self._adaptive_window(user, items)
        oldest = min(i.created_at for i in items)
        return (now - oldest) >= window

    async def _flush_chat(self, user: User) -> None:
        items = self.pending.get(user.chat_id)
        if not items:
            return

        items.sort(key=lambda i: i.score, reverse=True)
        cap = min(user.max_per_batch or self.settings.batch_max, self.settings.batch_max)
        take = items[:cap]

        if not self.limiter.allow(user.chat_id):
            log.debug("rate limited for chat %s (retry in %.1fs)", user.chat_id, self.limiter.wait_hint(user.chat_id))
            return

        claimed = await self.store.claim_batch(user.chat_id, cap)
        if not claimed:
            # Nothing claimable in the DB -> drop our stale in-memory copy.
            self.pending.pop(user.chat_id, None)
            return

        batch_id = str(claimed[0].get("batch_id") or f"b{int(time.time() * 1000):x}")
        fingerprints = [str(row["fingerprint"]) for row in claimed]

        # Keep only what we actually claimed; the rest waits for the next flush.
        claimed_set = set(fingerprints)
        self.pending[user.chat_id] = [i for i in items if i.fingerprint not in claimed_set]

        resolved: list[QueuedItem] = []
        by_fp = {i.fingerprint: i for i in take}
        for fp in fingerprints:
            item = by_fp.get(fp)
            if item is not None and item.cfg is not None:
                resolved.append(item)
                continue
            cfg = await self._resolve(fp)
            if cfg is None:
                continue
            resolved.append(
                QueuedItem(fp, item.score if item is not None else 0.0, cfg.protocol, time.time(), cfg)
            )

        if not resolved:
            await self.store.mark_failed(batch_id, fingerprints, "config row vanished", retryable=False)
            return

        self._batch_seq += 1
        text, keypad, metadata = self._build(user, resolved, batch_id, self._batch_seq)

        try:
            message_id = await self._send(user.chat_id, text, keypad, metadata)
        except Exception as exc:  # noqa: BLE001
            log.warning("send to %s failed: %s", user.chat_id, exc)
            await self.store.mark_failed(batch_id, fingerprints, str(exc), retryable=True)
            self.counters["failed"] += 1
            # Put the items back so they are retried instead of lost.
            self.pending.setdefault(user.chat_id, []).extend(resolved)
            return

        await self.store.mark_sent(batch_id, fingerprints, message_id or "")
        self.counters["delivered"] += len(fingerprints)
        log.info(
            "delivered %d config(s) to %s (batch %s)", len(fingerprints), user.chat_id, batch_id
        )

    async def _resolve(self, fingerprint: str) -> ProxyConfig | None:
        row = await self.store.get_config(fingerprint)
        if not row:
            return None
        return ProxyConfig(
            protocol=str(row.get("protocol") or ""),
            server=str(row.get("server") or ""),
            port=int(row.get("port") or 0),
            identity="",
            network=str(row.get("network") or ""),
            security=str(row.get("security") or ""),
            remark=str(row.get("remark") or ""),
            raw=str(row.get("raw") or ""),
            geo=str(row.get("geo") or ""),
        )

    # ------------------------------------------------------------------
    async def run(self, is_leader, should_stop) -> None:
        """Main pump loop.

        ``is_leader``  -> async callable returning True when this instance may work
        ``should_stop``-> async callable returning True when the loop must exit
        """
        log.info("dispatcher pump started")
        last_users_refresh = 0.0
        last_maintenance = 0.0
        await self.hydrate()

        while not await should_stop():
            if not await is_leader():
                await asyncio.sleep(3.0)
                continue

            now = time.time()
            if now - last_users_refresh > 120:
                await self.refresh_users()
                last_users_refresh = now
            if now - last_maintenance > 300:
                revived = await self.store.requeue_stale(now - 600)
                if revived:
                    log.info("requeued %d stale delivery row(s)", revived)
                self.limiter.prune()
                last_maintenance = now

            chats = list(self.pending.keys())
            if chats:
                # Round-robin so a chatty chat cannot starve the others.
                self._cursor %= len(chats)
                order = chats[self._cursor:] + chats[: self._cursor]
                self._cursor = (self._cursor + 1) % max(1, len(chats))

                for chat_id in order:
                    user = self.users.get(chat_id) or await self.store.get_user(chat_id)
                    if user is None or user.paused or not user.is_active:
                        continue
                    items = self.pending.get(chat_id) or []
                    if not self._should_flush(user, items, now):
                        continue
                    try:
                        await self._flush_chat(user)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("flush failed for %s: %s", chat_id, exc)

            await asyncio.sleep(TICK_SECONDS)

        # Drain anything still waiting before we hand over.
        if await is_leader() and self.pending:
            log.info("final drain of %d chat queue(s)", len(self.pending))
            for chat_id in list(self.pending.keys()):
                user = self.users.get(chat_id) or await self.store.get_user(chat_id)
                if user and not user.paused:
                    try:
                        await self._flush_chat(user)
                    except Exception:  # noqa: BLE001
                        pass
        log.info("dispatcher pump stopped")

    # ------------------------------------------------------------------
    # feedback
    # ------------------------------------------------------------------
    async def note_interest(self, user: User, protocol: str, weight: float = 1.0) -> None:
        """Reinforce a protocol the user interacts with (personal ranking)."""
        affinity = dict(user.affinity or {})
        current = float(affinity.get(protocol, 0.0))
        affinity[protocol] = round(min(8.0, current + 0.6 * weight), 3)
        # Let everything decay slightly so old habits fade.
        for key in list(affinity):
            if key != protocol:
                affinity[key] = round(max(0.0, affinity[key] * 0.97), 3)
        user.affinity = affinity
        await self.store.update_user(user.chat_id, affinity=affinity)

    async def force_flush(self, chat_id: str) -> int:
        """Flush immediately - used by the "send me something now" command."""
        user = self.users.get(chat_id) or await self.store.get_user(chat_id)
        if user is None:
            return 0
        before = self.counters["delivered"]
        await self._flush_chat(user)
        return self.counters["delivered"] - before


__all__ = ["Dispatcher", "QueuedItem"]
