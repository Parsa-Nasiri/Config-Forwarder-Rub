"""Token bucket rate limiting.

Rubika throttles bots that fire too fast, and a busy config channel can easily
produce hundreds of messages per minute. Two limiter layers keep both the
individual chat and the shared API budget safe.
"""

from __future__ import annotations

import time


class TokenBucket:
    """Classic token bucket. ``rate`` is tokens per second."""

    def __init__(self, rate_per_minute: float, capacity: float | None = None) -> None:
        self.rate = max(0.0, rate_per_minute) / 60.0
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_minute / 60.0 * 30)
        self.capacity = max(1.0, self.capacity)
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now

    def take(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    @property
    def available(self) -> float:
        self._refill()
        return self.tokens

    def retry_after(self) -> float:
        """Seconds until one token becomes available."""
        if self.rate <= 0:
            return 60.0
        self._refill()
        deficit = 1.0 - self.tokens
        return max(0.0, deficit / self.rate)


class RateLimiter:
    """Per-chat buckets plus one global bucket."""

    def __init__(self, per_chat_per_minute: float, global_per_minute: float) -> None:
        self.per_chat = per_chat_per_minute
        self.global_bucket = TokenBucket(global_per_minute)
        self.chats: dict[str, TokenBucket] = {}

    def allow(self, chat_id: str, tokens: float = 1.0) -> bool:
        bucket = self.chats.get(chat_id)
        if bucket is None:
            bucket = TokenBucket(self.per_chat)
            self.chats[chat_id] = bucket
        if not self.global_bucket.take(tokens):
            return False
        if not bucket.take(tokens):
            # Give the global token back - we did not actually send.
            self.global_bucket.tokens = min(
                self.global_bucket.capacity, self.global_bucket.tokens + tokens
            )
            return False
        return True

    def wait_hint(self, chat_id: str) -> float:
        bucket = self.chats.get(chat_id)
        chat_wait = bucket.retry_after() if bucket else 0.0
        return max(chat_wait, self.global_bucket.retry_after())

    def prune(self, max_idle_seconds: float = 3600.0) -> None:
        """Drop buckets for chats we have not talked to in a while."""
        now = time.monotonic()
        stale = [
            key for key, b in self.chats.items()
            if now - b.updated > max_idle_seconds and b.available >= b.capacity - 0.01
        ]
        for key in stale:
            self.chats.pop(key, None)


__all__ = ["TokenBucket", "RateLimiter"]
