"""Ephemeral in-process storage.

Used for local development and by the unit tests. On GitHub Actions this mode
means every 5h40m handoff starts from a blank slate, so it is only acceptable
while you are experimenting - configure Supabase for anything real.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable

from ..logging_setup import get_logger
from .base import Store, User

log = get_logger("storage.memory")


class MemoryStore(Store):
    name = "memory"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.users: dict[str, User] = {}
        self.configs: dict[str, dict[str, Any]] = {}
        self.deliveries: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {}
        self.locks: dict[str, dict[str, Any]] = {}
        self.channels: dict[str, dict[str, int]] = {}
        self._seq = 0

    @property
    def durable(self) -> bool:
        return False

    # ------------------------------------------------------------------ users
    async def get_user(self, chat_id: str) -> User | None:
        return self.users.get(chat_id)

    async def upsert_user(self, user: User) -> None:
        self.users[user.chat_id] = user

    async def update_user(self, chat_id: str, **fields: Any) -> None:
        user = self.users.get(chat_id)
        if not user:
            return
        for k, v in fields.items():
            if hasattr(user, k):
                setattr(user, k, v)

    async def list_users(self, *, only_deliverable: bool = False) -> list[User]:
        users = list(self.users.values())
        if only_deliverable:
            users = [u for u in users if u.is_active and not u.paused]
        return users

    # ---------------------------------------------------------------- configs
    async def add_config(
        self,
        *,
        fingerprint: str,
        protocol: str,
        server: str,
        port: int,
        remark: str,
        raw: str,
        score: float,
        geo: str,
        network: str,
        security: str,
        source_channel: str,
        source_message: str,
    ) -> bool:
        now = time.time()
        existing = self.configs.get(fingerprint)
        if existing:
            existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
            existing["last_seen_at"] = now
            if existing.get("source_channel") != source_channel:
                existing["source_count"] = int(existing.get("source_count", 1)) + 1
            existing["score"] = max(float(existing.get("score", 0)), float(score))
            return False
        self.configs[fingerprint] = {
            "fingerprint": fingerprint,
            "protocol": protocol,
            "server": server,
            "port": port,
            "remark": remark,
            "raw": raw,
            "score": float(score),
            "geo": geo,
            "network": network,
            "security": security,
            "source_channel": source_channel,
            "source_message": source_message,
            "first_seen_at": now,
            "last_seen_at": now,
            "seen_count": 1,
            "source_count": 1,
            "dead_reports": 0,
            "live_reports": 0,
            "copy_count": 0,
            "delivered_count": 0,
            "health_ok": None,
            "latency_ms": None,
        }
        return True

    async def get_config(self, fingerprint: str) -> dict[str, Any] | None:
        return self.configs.get(fingerprint)

    async def patch_config(self, fingerprint: str, **fields: Any) -> None:
        cfg = self.configs.get(fingerprint)
        if cfg:
            cfg.update(fields)

    async def top_configs(self, limit: int = 10, protocol: str = "") -> list[dict[str, Any]]:
        rows = list(self.configs.values())
        if protocol:
            rows = [r for r in rows if (r.get("protocol") or "").lower() == protocol.lower()]
        rows.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
        return rows[: max(1, limit)]

    async def count_configs_on_server(self, server: str) -> int:
        return sum(1 for c in self.configs.values() if c.get("server") == server)

    async def stats(self) -> dict[str, int]:
        return {
            "configs": len(self.configs),
            "users": sum(1 for u in self.users.values() if u.is_active),
            "delivered": sum(1 for d in self.deliveries if d.get("status") == "sent"),
        }

    # ------------------------------------------------------------- deliveries
    async def enqueue(self, chat_id: str, fingerprint: str, score: float) -> bool:
        for d in self.deliveries:
            if d["chat_id"] == chat_id and d["fingerprint"] == fingerprint:
                return False
        self._seq += 1
        self.deliveries.append(
            {
                "id": self._seq,
                "chat_id": chat_id,
                "fingerprint": fingerprint,
                "score": float(score),
                "status": "queued",
                "attempts": 0,
                "next_attempt": time.time(),
                "batch_id": None,
                "ord": 0,
                "created_at": time.time(),
                "sent_at": None,
                "message_id": None,
                "copied_at": None,
            }
        )
        return True

    async def list_queued(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        now = time.time()
        rows = [
            d
            for d in self.deliveries
            if d["chat_id"] == chat_id and d["status"] == "queued" and d["next_attempt"] <= now
        ]
        rows.sort(key=lambda d: float(d.get("score") or 0), reverse=True)
        return rows[: max(1, limit)]

    async def claim_batch(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        now = time.time()
        candidates = [
            d
            for d in self.deliveries
            if d["chat_id"] == chat_id
            and d["status"] == "queued"
            and d["next_attempt"] <= now
        ]
        candidates.sort(key=lambda d: float(d.get("score") or 0), reverse=True)
        picked = candidates[: max(1, limit)]
        batch_id = f"b{int(now * 1000):x}"
        for idx, d in enumerate(picked):
            d["status"] = "sending"
            d["batch_id"] = batch_id
            d["ord"] = idx
        return picked

    async def mark_sent(
        self, batch_id: str, fingerprints: Iterable[str], message_id: str
    ) -> None:
        fps = {str(f) for f in fingerprints}
        now = time.time()
        for d in self.deliveries:
            if d.get("batch_id") != batch_id or d["fingerprint"] not in fps:
                continue
            d["status"] = "sent"
            d["sent_at"] = now
            d["message_id"] = message_id
            cfg = self.configs.get(d["fingerprint"])
            if cfg:
                cfg["delivered_count"] = int(cfg.get("delivered_count") or 0) + 1

    async def mark_failed(
        self, batch_id: str, fingerprints: Iterable[str], error: str, retryable: bool
    ) -> None:
        fps = {str(f) for f in fingerprints}
        now = time.time()
        for d in self.deliveries:
            if d.get("batch_id") != batch_id or d["fingerprint"] not in fps:
                continue
            d["attempts"] = int(d.get("attempts") or 0) + 1
            d["error"] = (error or "")[:400]
            if not retryable or d["attempts"] >= 4:
                d["status"] = "failed"
                d["next_attempt"] = now
            else:
                d["status"] = "queued"
                d["next_attempt"] = now + min(900, 30 * (2 ** (d["attempts"] - 1)))

    async def mark_copied(self, chat_id: str, fingerprint: str) -> None:
        now = time.time()
        for d in self.deliveries:
            if d["chat_id"] == chat_id and d["fingerprint"] == fingerprint:
                d["copied_at"] = now
        cfg = self.configs.get(fingerprint)
        if cfg:
            cfg["copy_count"] = int(cfg.get("copy_count") or 0) + 1
            cfg["score"] = min(100.0, float(cfg.get("score") or 0) + 1.5)

    async def recent_delivery_count(self, chat_id: str, since: float) -> int:
        return sum(
            1
            for d in self.deliveries
            if d["chat_id"] == chat_id
            and d["status"] == "sent"
            and (d.get("sent_at") or 0) >= since
        )

    async def batch_fingerprints(self, chat_id: str, batch_id: str) -> list[str]:
        rows = [
            d
            for d in self.deliveries
            if d["chat_id"] == chat_id and d.get("batch_id") == batch_id
        ]
        rows.sort(key=lambda d: d.get("ord") or 0)
        return [str(d["fingerprint"]) for d in rows]

    async def requeue_stale(self, older_than: float) -> int:
        n = 0
        for d in self.deliveries:
            if d["status"] == "sending" and (d.get("sent_at") is None) and d["created_at"] < older_than:
                d["status"] = "queued"
                d["next_attempt"] = time.time()
                n += 1
        return n

    # ----------------------------------------------------------- feedback loop
    async def report(self, fingerprint: str, verdict: str, chat_id: str = "") -> None:
        cfg = self.configs.get(fingerprint)
        if not cfg:
            return
        if verdict == "dead":
            cfg["dead_reports"] = int(cfg.get("dead_reports") or 0) + 1
            cfg["score"] = max(0.0, float(cfg.get("score") or 0) - 22)
            cfg["health_ok"] = False
            if cfg.get("source_channel"):
                await self.bump_channel(cfg["source_channel"], "dead", 1)
        else:
            cfg["live_reports"] = int(cfg.get("live_reports") or 0) + 1
            cfg["score"] = min(100.0, float(cfg.get("score") or 0) + 6)
            cfg["health_ok"] = True
        if chat_id and verdict == "live":
            for d in self.deliveries:
                if d["chat_id"] == chat_id and d["fingerprint"] == fingerprint:
                    d["copied_at"] = time.time()

    # ------------------------------------------------------------ channel rep
    async def bump_channel(self, channel: str, field: str, amount: int = 1) -> None:
        if field not in {"total", "dead", "copies"}:
            return
        row = self.channels.setdefault(channel, {"total": 0, "dead": 0, "copies": 0})
        row[field] = int(row.get(field) or 0) + amount

    async def channel_reputation(self, channel: str) -> float:
        row = self.channels.get(channel)
        if not row:
            return 0.0
        total = int(row.get("total") or 0)
        dead = int(row.get("dead") or 0)
        copies = int(row.get("copies") or 0)
        if total <= 0:
            return 0.0
        dead_rate = dead / max(1, total)
        copy_rate = copies / max(1, total)
        return max(-1.0, min(1.0, 0.6 * (1 - 2 * dead_rate) + 0.4 * min(1.0, copy_rate * 4)))

    # ------------------------------------------------------------------ state
    async def get_state(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    async def set_state(self, key: str, value: Any) -> None:
        self.state[key] = value

    # ------------------------------------------------------------------ locks
    async def acquire_lock(
        self, name: str, instance: str, ttl: int, meta: dict | None = None
    ) -> bool:
        now = time.time()
        row = self.locks.get(name)
        if row and row["instance_id"] != instance and row["expires_at"] >= now:
            return False
        self.locks[name] = {
            "name": name,
            "instance_id": instance,
            "acquired_at": now,
            "expires_at": now + ttl,
            "meta": meta or {},
        }
        return True

    async def release_lock(self, name: str, instance: str) -> None:
        row = self.locks.get(name)
        if row and row.get("instance_id") == instance:
            self.locks.pop(name, None)

    async def read_lock(self, name: str) -> dict[str, Any] | None:
        return self.locks.get(name)


__all__ = ["MemoryStore"]
