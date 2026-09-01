"""Storage contract.

Two implementations exist:
  * :class:`bot.storage.supabase_store.SupabaseStore` - durable, used in prod.
  * :class:`bot.storage.memory_store.MemoryStore`     - ephemeral, used for
    local development and for the unit tests.

Both are async so they can live inside the same event loop as Telethon.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

SUPPORTED_LANGUAGES = ("en", "fa")


@dataclass
class User:
    """One Rubika chat that talks to the bot."""

    chat_id: str
    user_id: str = ""
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language: str = "en"
    is_active: bool = True
    is_paused: bool = False
    paused_until: float = 0.0
    protocols: list[str] = field(default_factory=list)  # empty = all
    min_score: int = 55
    max_per_batch: int = 5
    live_mode: bool = False
    affinity: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)

    # -----------------------------------------------------------------
    @property
    def display_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name or self.username or self.chat_id

    @property
    def paused(self) -> bool:
        if self.is_paused:
            # A timed pause ("snooze 1h") expires on its own.
            if self.paused_until and self.paused_until <= time.time():
                return False
            return True
        return False

    def accepts(self, protocol: str, score: float) -> bool:
        if not self.is_active or self.paused:
            return False
        if score < self.min_score:
            return False
        if self.protocols and protocol.lower() not in [p.lower() for p in self.protocols]:
            return False
        return True

    # -----------------------------------------------------------------
    def to_row(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "username": self.username,
            "language": self.language,
            "is_active": self.is_active,
            "is_paused": self.is_paused,
            "paused_until": None if not self.paused_until else _iso(self.paused_until),
            "protocols": self.protocols,
            "min_score": self.min_score,
            "max_per_batch": self.max_per_batch,
            "live_mode": self.live_mode,
            "affinity": self.affinity,
            "last_seen_at": _iso(self.last_seen_at),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "User":
        def as_list(v: Any) -> list[str]:
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    return [str(x) for x in parsed] if isinstance(parsed, list) else []
                except (ValueError, TypeError):
                    return []
            return [str(x) for x in v] if isinstance(v, list) else []

        def as_dict(v: Any) -> dict[str, float]:
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    return parsed if isinstance(parsed, dict) else {}
                except (ValueError, TypeError):
                    return {}
            return {str(k): float(val) for k, val in v.items()} if isinstance(v, dict) else {}

        paused_until = row.get("paused_until")
        return cls(
            chat_id=str(row["chat_id"]),
            user_id=str(row.get("user_id") or ""),
            first_name=row.get("first_name") or "",
            last_name=row.get("last_name") or "",
            username=row.get("username") or "",
            language=(row.get("language") or "en").lower(),
            is_active=bool(row.get("is_active", True)),
            is_paused=bool(row.get("is_paused", False)),
            paused_until=_parse_ts(paused_until),
            protocols=as_list(row.get("protocols")),
            min_score=int(row.get("min_score") or 55),
            max_per_batch=int(row.get("max_per_batch") or 5),
            live_mode=bool(row.get("live_mode", False)),
            affinity=as_dict(row.get("affinity")),
            created_at=_parse_ts(row.get("created_at")) or time.time(),
            last_seen_at=_parse_ts(row.get("last_seen_at")) or time.time(),
        )


def _iso(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()


def _parse_ts(value: Any) -> float:
    """Best-effort timestamp parsing for values coming back from PostgREST."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        try:
            return _dt.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=_dt.timezone.utc
            ).timestamp()
        except ValueError:
            return 0.0


class Store(ABC):
    """Everything the bot needs to persist."""

    name = "abstract"

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Called once at startup. May raise to abort early."""

    async def close(self) -> None:
        """Flush and release resources."""

    @property
    def durable(self) -> bool:
        """True when state survives a process restart."""
        return False

    # ------------------------------------------------------------------- users
    @abstractmethod
    async def get_user(self, chat_id: str) -> User | None: ...

    @abstractmethod
    async def upsert_user(self, user: User) -> None: ...

    @abstractmethod
    async def update_user(self, chat_id: str, **fields: Any) -> None: ...

    @abstractmethod
    async def list_users(self, *, only_deliverable: bool = False) -> list[User]: ...

    # ----------------------------------------------------------------- configs
    @abstractmethod
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
        """Insert a config. Returns True when it was brand new, False on dedupe."""

    @abstractmethod
    async def get_config(self, fingerprint: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def patch_config(self, fingerprint: str, **fields: Any) -> None: ...

    @abstractmethod
    async def top_configs(self, limit: int = 10, protocol: str = "") -> list[dict[str, Any]]: ...

    @abstractmethod
    async def count_configs_on_server(self, server: str) -> int: ...

    @abstractmethod
    async def stats(self) -> dict[str, int]: ...

    # -------------------------------------------------------------- deliveries
    @abstractmethod
    async def enqueue(self, chat_id: str, fingerprint: str, score: float) -> bool:
        """Queue a config for a chat. False when already known for that chat."""

    @abstractmethod
    async def list_queued(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Read-only view of the pending queue (used to survive a handoff)."""

    @abstractmethod
    async def claim_batch(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        """Atomically move up to `limit` queued rows for a chat into 'sending'."""

    @abstractmethod
    async def mark_sent(self, batch_id: str, fingerprints: Iterable[str], message_id: str) -> None: ...

    @abstractmethod
    async def mark_failed(
        self, batch_id: str, fingerprints: Iterable[str], error: str, retryable: bool
    ) -> None: ...

    @abstractmethod
    async def mark_copied(self, chat_id: str, fingerprint: str) -> None: ...

    @abstractmethod
    async def recent_delivery_count(self, chat_id: str, since: float) -> int: ...

    @abstractmethod
    async def batch_fingerprints(self, chat_id: str, batch_id: str) -> list[str]: ...

    @abstractmethod
    async def requeue_stale(self, older_than: float) -> int: ...

    # ---------------------------------------------------------- feedback loop
    @abstractmethod
    async def report(self, fingerprint: str, verdict: str, chat_id: str = "") -> None:
        """verdict is 'dead' or 'live'."""

    # ------------------------------------------------------------- channel rep
    @abstractmethod
    async def bump_channel(self, channel: str, field: str, amount: int = 1) -> None: ...

    @abstractmethod
    async def channel_reputation(self, channel: str) -> float:
        """-1.0 .. +1.0 quality signal for a monitored channel."""

    # ------------------------------------------------------------------- state
    @abstractmethod
    async def get_state(self, key: str, default: Any = None) -> Any: ...

    @abstractmethod
    async def set_state(self, key: str, value: Any) -> None: ...

    # ------------------------------------------------------------------ locks
    @abstractmethod
    async def acquire_lock(self, name: str, instance: str, ttl: int, meta: dict | None = None) -> bool: ...

    @abstractmethod
    async def release_lock(self, name: str, instance: str) -> None: ...

    @abstractmethod
    async def read_lock(self, name: str) -> dict[str, Any] | None: ...

    # ---------------------------------------------------------------- sources
    async def list_sources(self) -> list[str]:
        """Extra Telegram channels stored in the database (may be empty)."""
        return []
