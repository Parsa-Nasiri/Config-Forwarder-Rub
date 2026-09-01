"""Storage package: pick the right backend for the environment."""

from __future__ import annotations

from ..config import Settings
from ..logging_setup import get_logger
from .base import Store, User
from .memory_store import MemoryStore
from .supabase_store import SupabaseStore

log = get_logger("storage")


async def build_store(settings: Settings) -> Store:
    """Supabase when configured, otherwise fall back to memory."""
    if settings.supabase_enabled:
        store: Store = SupabaseStore(settings.supabase_url, settings.supabase_key)
        try:
            await store.start()
            return store
        except Exception as exc:  # noqa: BLE001
            log.warning("falling back to memory storage: %s", exc)
    else:
        log.warning(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY not set - running with "
            "ephemeral in-memory storage. State resets on every restart."
        )
    store = MemoryStore()
    await store.start()
    return store


__all__ = ["Store", "User", "MemoryStore", "SupabaseStore", "build_store"]
