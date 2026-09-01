"""Durable storage backed by Supabase (Postgres via PostgREST).

We talk to the REST layer directly with httpx instead of pulling in the
official ``supabase`` python package: it keeps the dependency tree tiny (the
workflow reinstalls requirements on every 5h40m handoff) and the surface we
actually use is only a handful of table operations.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, Iterable

import httpx

from ..logging_setup import get_logger
from .base import Store, User, _iso

log = get_logger("storage.supabase")


def _utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


class SupabaseStore(Store):
    name = "supabase"

    def __init__(self, url: str, key: str, timeout: float = 20.0) -> None:
        self.base = url.rstrip("/")
        self.rest = f"{self.base}/rest/v1"
        self._key = key
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "apikey": self._key,
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        # Fail fast with a clear message when the project/keys/tables are wrong.
        try:
            await self._get("users", {"select": "chat_id", "limit": "1"})
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Cannot reach Supabase at {self.base} ({exc}). "
                "Check SUPABASE_URL / SUPABASE_SERVICE_KEY and make sure "
                "bot/storage/schema.sql has been executed."
            ) from exc
        log.info("supabase connected (%s)", self.base)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def durable(self) -> bool:
        return True

    # ------------------------------------------------------------- http layer
    @property
    def c(self) -> httpx.AsyncClient:
        if self._client is None:  # pragma: no cover - defensive
            raise RuntimeError("SupabaseStore.start() was never awaited")
        return self._client

    def _check(self, resp: httpx.Response, action: str) -> None:
        if resp.status_code >= 400:
            raise RuntimeError(f"{action} failed [{resp.status_code}]: {resp.text[:300]}")

    async def _get(self, table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        resp = await self.c.get(f"{self.rest}/{table}", params=params)
        self._check(resp, f"GET {table}")
        return resp.json()

    async def _post(
        self, table: str, body: Any, params: dict[str, Any] | None = None, prefer: str = ""
    ) -> list[dict[str, Any]]:
        headers = {"Prefer": prefer} if prefer else None
        resp = await self.c.post(f"{self.rest}/{table}", json=body, params=params, headers=headers)
        self._check(resp, f"POST {table}")
        if resp.text:
            return resp.json()
        return []

    async def _patch(
        self, table: str, params: dict[str, Any], body: dict[str, Any], prefer: str = ""
    ) -> list[dict[str, Any]]:
        headers = {"Prefer": prefer} if prefer else None
        resp = await self.c.patch(f"{self.rest}/{table}", params=params, json=body, headers=headers)
        self._check(resp, f"PATCH {table}")
        if resp.text:
            return resp.json()
        return []

    async def _delete(self, table: str, params: dict[str, Any]) -> None:
        resp = await self.c.delete(f"{self.rest}/{table}", params=params)
        self._check(resp, f"DELETE {table}")

    async def _count(self, table: str, params: dict[str, Any] | None = None) -> int:
        p = dict(params or {})
        p["select"] = "id" if table == "deliveries" else "chat_id" if table == "users" else "fingerprint"
        resp = await self.c.head(f"{self.rest}/{table}", params=p, headers={"Prefer": "count=exact"})
        if resp.status_code >= 400:
            return 0
        content_range = resp.headers.get("content-range", "")
        # Format: "0-24/1234" or "*/1234"
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[-1]
            return int(tail) if tail.isdigit() else 0
        return 0

    async def _rpc(self, fn: str, body: dict[str, Any]) -> Any:
        resp = await self.c.post(f"{self.rest}/rpc/{fn}", json=body)
        self._check(resp, f"RPC {fn}")
        if not resp.text:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text.strip('"')

    # ------------------------------------------------------------------ users
    async def get_user(self, chat_id: str) -> User | None:
        rows = await self._get("users", {"select": "*", "chat_id": f"eq.{chat_id}", "limit": "1"})
        return User.from_row(rows[0]) if rows else None

    async def upsert_user(self, user: User) -> None:
        await self._post(
            "users",
            user.to_row(),
            params={"on_conflict": "chat_id"},
            prefer="resolution=merge-duplicates,return=minimal",
        )

    async def update_user(self, chat_id: str, **fields: Any) -> None:
        if not fields:
            return
        payload = {
            k: (_utc(v) if k in {"paused_until", "last_seen_at"} and isinstance(v, (int, float)) else v)
            for k, v in fields.items()
        }
        payload.setdefault("updated_at", _utc(time.time()))
        await self._patch("users", {"chat_id": f"eq.{chat_id}"}, payload)

    async def list_users(self, *, only_deliverable: bool = False) -> list[User]:
        params: dict[str, Any] = {"select": "*", "order": "created_at.asc", "limit": "5000"}
        if only_deliverable:
            params["is_active"] = "eq.true"
        rows = await self._get("users", params)
        users = [User.from_row(r) for r in rows]
        if only_deliverable:
            users = [u for u in users if not u.paused]
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
        now = _utc(time.time())
        row = {
            "fingerprint": fingerprint,
            "protocol": protocol,
            "server": server,
            "port": port,
            "remark": remark,
            "raw": raw,
            "score": round(float(score), 2),
            "geo": geo,
            "network": network,
            "security": security,
            "source_channel": source_channel,
            "source_message": source_message,
            "first_seen_at": now,
            "last_seen_at": now,
            "seen_count": 1,
            "source_count": 1,
        }
        inserted = await self._post(
            "configs",
            row,
            prefer="resolution=ignore-duplicates,return=minimal",
        )
        if inserted is not None and len(inserted) > 0:
            return True

        # Already known -> bump counters and refresh the score/geo.
        existing = await self.get_config(fingerprint)
        patch: dict[str, Any] = {
            "seen_count": int(existing.get("seen_count") or 1) + 1,
            "last_seen_at": now,
        }
        if existing and (existing.get("source_channel") or "") != source_channel:
            patch["source_count"] = int(existing.get("source_count") or 1) + 1
        if existing and (existing.get("score") or 0) < score:
            patch["score"] = round(float(score), 2)
        if existing and not existing.get("geo") and geo:
            patch["geo"] = geo
        await self.patch_config(fingerprint, **patch)
        return False

    async def get_config(self, fingerprint: str) -> dict[str, Any] | None:
        rows = await self._get(
            "configs", {"select": "*", "fingerprint": f"eq.{fingerprint}", "limit": "1"}
        )
        return rows[0] if rows else None

    async def patch_config(self, fingerprint: str, **fields: Any) -> None:
        if not fields:
            return
        await self._patch("configs", {"fingerprint": f"eq.{fingerprint}"}, fields)

    async def top_configs(self, limit: int = 10, protocol: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"select": "*", "order": "score.desc", "limit": str(max(1, limit))}
        if protocol:
            params["protocol"] = f"eq.{protocol.lower()}"
        return await self._get("configs", params)

    async def count_configs_on_server(self, server: str) -> int:
        return await self._count("configs", {"server": f"eq.{server}"})

    async def stats(self) -> dict[str, int]:
        sent = await self._count("deliveries", {"status": "eq.sent"})
        return {
            "configs": await self._count("configs"),
            "users": await self._count("users", {"is_active": "eq.true"}),
            "delivered": sent,
        }

    # ------------------------------------------------------------- deliveries
    async def enqueue(self, chat_id: str, fingerprint: str, score: float) -> bool:
        row = {
            "chat_id": chat_id,
            "fingerprint": fingerprint,
            "score": round(float(score), 2),
            "status": "queued",
            "attempts": 0,
            "next_attempt": _utc(time.time()),
        }
        inserted = await self._post(
            "deliveries",
            row,
            params={"on_conflict": "chat_id,fingerprint"},
            prefer="resolution=ignore-duplicates,return=minimal",
        )
        return bool(inserted)

    async def list_queued(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        now = _utc(time.time())
        return await self._get(
            "deliveries",
            {
                "select": "id,fingerprint,score,status",
                "chat_id": f"eq.{chat_id}",
                "status": "eq.queued",
                "next_attempt": f"lte.{now}",
                "order": "score.desc",
                "limit": str(max(1, min(limit, 1000))),
            },
        )

    async def claim_batch(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        now = _utc(time.time())
        params = {
            "select": "*",
            "chat_id": f"eq.{chat_id}",
            "status": "eq.queued",
            "next_attempt": f"lte.{now}",
            "order": "score.desc",
            "limit": str(max(1, limit)),
        }
        rows = await self._get("deliveries", params)
        if not rows:
            return []
        batch_id = f"b{int(time.time() * 1000):x}"
        for idx, row in enumerate(rows):
            await self._patch(
                "deliveries",
                {"id": f"eq.{row['id']}"},
                {"status": "sending", "batch_id": batch_id, "ord": idx},
            )
        return [dict(r, batch_id=batch_id, ord=i) for i, r in enumerate(rows)]

    async def mark_sent(
        self, batch_id: str, fingerprints: Iterable[str], message_id: str
    ) -> None:
        fps = [str(f) for f in fingerprints]
        if not fps:
            return
        await self._patch(
            "deliveries",
            {"batch_id": f"eq.{batch_id}", "fingerprint": f"in.({_in_list(fps)})"},
            {"status": "sent", "sent_at": _utc(time.time()), "message_id": message_id},
        )
        # Keep the per-config delivery counter in sync for the ranking queries.
        for fp in fps:
            cfg = await self.get_config(fp)
            if cfg:
                await self.patch_config(
                    fp, delivered_count=int(cfg.get("delivered_count") or 0) + 1
                )

    async def mark_failed(
        self, batch_id: str, fingerprints: Iterable[str], error: str, retryable: bool
    ) -> None:
        fps = [str(f) for f in fingerprints]
        if not fps:
            return
        rows = await self._get(
            "deliveries",
            {"select": "id,attempts", "batch_id": f"eq.{batch_id}", "fingerprint": f"in.({_in_list(fps)})"},
        )
        now = time.time()
        for row in rows:
            attempts = int(row.get("attempts") or 0) + 1
            if not retryable or attempts >= 4:
                await self._patch(
                    "deliveries",
                    {"id": f"eq.{row['id']}"},
                    {
                        "status": "failed",
                        "attempts": attempts,
                        "error": (error or "")[:400],
                        "next_attempt": _utc(now),
                    },
                )
            else:
                backoff = min(900, 30 * (2 ** (attempts - 1)))
                await self._patch(
                    "deliveries",
                    {"id": f"eq.{row['id']}"},
                    {
                        "status": "queued",
                        "attempts": attempts,
                        "error": (error or "")[:400],
                        "next_attempt": _utc(now + backoff),
                    },
                )

    async def mark_copied(self, chat_id: str, fingerprint: str) -> None:
        await self._patch(
            "deliveries",
            {"chat_id": f"eq.{chat_id}", "fingerprint": f"eq.{fingerprint}"},
            {"copied_at": _utc(time.time())},
        )
        cfg = await self.get_config(fingerprint)
        if cfg:
            await self.patch_config(
                fingerprint,
                copy_count=int(cfg.get("copy_count") or 0) + 1,
                score=round(float(cfg.get("score") or 0) + 1.5, 2),
            )

    async def recent_delivery_count(self, chat_id: str, since: float) -> int:
        return await self._count(
            "deliveries",
            {"chat_id": f"eq.{chat_id}", "status": "eq.sent", "sent_at": f"gte.{_utc(since)}"},
        )

    async def batch_fingerprints(self, chat_id: str, batch_id: str) -> list[str]:
        rows = await self._get(
            "deliveries",
            {
                "select": "fingerprint,ord",
                "chat_id": f"eq.{chat_id}",
                "batch_id": f"eq.{batch_id}",
                "order": "ord.asc",
            },
        )
        return [str(r["fingerprint"]) for r in rows]

    async def requeue_stale(self, older_than: float) -> int:
        rows = await self._get(
            "deliveries",
            {
                "select": "id",
                "status": "eq.sending",
                "sent_at": f"is.null",
                "created_at": f"lt.{_utc(older_than)}",
                "limit": "200",
            },
        )
        for row in rows:
            await self._patch(
                "deliveries",
                {"id": f"eq.{row['id']}"},
                {"status": "queued", "next_attempt": _utc(time.time())},
            )
        return len(rows)

    # ----------------------------------------------------------- feedback loop
    async def report(self, fingerprint: str, verdict: str, chat_id: str = "") -> None:
        cfg = await self.get_config(fingerprint)
        if not cfg:
            return
        if verdict == "dead":
            await self.patch_config(
                fingerprint,
                dead_reports=int(cfg.get("dead_reports") or 0) + 1,
                score=max(0.0, float(cfg.get("score") or 0) - 22),
                health_ok=False,
            )
            channel = cfg.get("source_channel")
            if channel:
                await self.bump_channel(channel, "dead", 1)
        else:
            await self.patch_config(
                fingerprint,
                live_reports=int(cfg.get("live_reports") or 0) + 1,
                score=min(100.0, float(cfg.get("score") or 0) + 6),
                health_ok=True,
            )
        if chat_id and verdict == "live":
            await self._patch(
                "deliveries",
                {"chat_id": f"eq.{chat_id}", "fingerprint": f"eq.{fingerprint}"},
                {"copied_at": _utc(time.time())},
            )

    # ------------------------------------------------------------ channel rep
    async def bump_channel(self, channel: str, field: str, amount: int = 1) -> None:
        if field not in {"total", "dead", "copies"}:  # guard against injection
            return
        rows = await self._get(
            "channel_stats", {"select": "*", "channel": f"eq.{channel}", "limit": "1"}
        )
        if rows:
            row = rows[0]
            await self._patch(
                "channel_stats",
                {"channel": f"eq.{channel}"},
                {field: int(row.get(field) or 0) + amount, "updated_at": _utc(time.time())},
            )
        else:
            payload = {"channel": channel, "total": 0, "dead": 0, "copies": 0}
            payload[field] = amount
            await self._post("channel_stats", payload, prefer="return=minimal")

    async def channel_reputation(self, channel: str) -> float:
        rows = await self._get(
            "channel_stats", {"select": "*", "channel": f"eq.{channel}", "limit": "1"}
        )
        if not rows:
            return 0.0
        row = rows[0]
        total = int(row.get("total") or 0)
        dead = int(row.get("dead") or 0)
        copies = int(row.get("copies") or 0)
        if total <= 0:
            return 0.0
        # Start neutral, reward a low dead-rate and an engaged audience.
        dead_rate = dead / max(1, total)
        copy_rate = copies / max(1, total)
        return max(-1.0, min(1.0, 0.6 * (1 - 2 * dead_rate) + 0.4 * min(1.0, copy_rate * 4)))

    # ------------------------------------------------------------------ state
    async def get_state(self, key: str, default: Any = None) -> Any:
        rows = await self._get("bot_state", {"select": "value", "key": f"eq.{key}", "limit": "1"})
        if not rows:
            return default
        return rows[0].get("value")

    async def set_state(self, key: str, value: Any) -> None:
        await self._post(
            "bot_state",
            {"key": key, "value": value, "updated_at": _utc(time.time())},
            params={"on_conflict": "key"},
            prefer="resolution=merge-duplicates,return=minimal",
        )

    # ------------------------------------------------------------------ locks
    async def acquire_lock(
        self, name: str, instance: str, ttl: int, meta: dict | None = None
    ) -> bool:
        result = await self._rpc(
            "acquire_runner_lock",
            {
                "p_name": name,
                "p_instance": instance,
                "p_ttl_seconds": int(ttl),
                "p_meta": meta or {},
            },
        )
        return bool(result)

    async def release_lock(self, name: str, instance: str) -> None:
        try:
            await self._rpc("release_runner_lock", {"p_name": name, "p_instance": instance})
        except Exception as exc:  # noqa: BLE001
            log.warning("could not release lock %s: %s", name, exc)

    async def read_lock(self, name: str) -> dict[str, Any] | None:
        rows = await self._get("runner_locks", {"select": "*", "name": f"eq.{name}", "limit": "1"})
        return rows[0] if rows else None

    # ---------------------------------------------------------------- sources
    async def list_sources(self) -> list[str]:
        try:
            rows = await self._get("sources", {"select": "channel", "enabled": "eq.true"})
            return [str(r["channel"]) for r in rows]
        except Exception:  # noqa: BLE001 - table is optional
            return []


def _in_list(values: Iterable[str]) -> str:
    """PostgREST `in.()` filter body: in.("a","b")."""
    return ",".join('"' + v.replace('"', "") + '"' for v in values)


__all__ = ["SupabaseStore", "User", "_iso"]
