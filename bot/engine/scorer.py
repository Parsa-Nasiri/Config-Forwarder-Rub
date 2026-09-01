"""Quality scoring.

Every config gets a 0-100 score before it is allowed anywhere near a user's
chat. The score is a *blend* of static properties (what the config is) and
learned signals (how the config and its source have behaved over time), and it
is recomputed whenever new evidence arrives.

Breakdown of the signal budget:

    protocol base      24-58   what kind of tunnel it is
    transport           0..7    ws / grpc / h2 / quic
    encryption        -20..16   reality / tls / none
    freshness          0..14    brand new configs are worth more
    corroboration      0..12    seen on several independent channels
    source reputation -18..18   historical dead-rate of the channel
    health probe      -40..12   TCP reachability + latency (optional)
    crowd feedback    -60..10   dead / live / copied reports from users
    anti-spam         -18..0    one server flooding hundreds of ports

The raw sum is passed through a soft knee above 80 (see :func:`_compress`) so
the very best configs crowd towards 100 without a hard clamp flattening the
ranking: a delta at the top still moves the number, which is what keeps the
priority queue honest. Everything below ``MIN_SCORE`` is dropped.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from ..config import PROTOCOL_BASE, SECURITY_BONUS, TRANSPORT_BONUS, Settings
from ..storage.base import Store
from .models import ProxyConfig

log = logging.getLogger("engine.scorer")

# Above this raw sum the score is compressed instead of clamped, so multiple
# positive signals still produce an *ordering* at the top end.
KNEE = 80.0


def _compress(raw: float) -> float:
    """Map an unbounded raw sum into 0..100 with a soft knee at 80."""
    raw = max(0.0, raw)
    if raw <= KNEE:
        return raw
    return KNEE + (100.0 - KNEE) * (1.0 - math.exp(-(raw - KNEE) / 25.0))

# Remarks that advertise themselves rather than describe the server.
_SPAM_WORDS = (
    "buy", "shop", "order", "price", "payment", "support", "admin", "channel",
    "join", "telegram", "instagram", "click", "link", "promo", "discount",
    "خرید", "فروش", "کانال", "پشتیبانی", "اشتراک",
)


@dataclass
class ScoreResult:
    value: float
    reasons: dict[str, float] = field(default_factory=dict)

    @property
    def grade(self) -> str:
        v = self.value
        if v >= 88:
            return "excellent"
        if v >= 72:
            return "good"
        if v >= 58:
            return "fair"
        return "weak"

    def top_reasons(self, n: int = 3) -> list[tuple[str, float]]:
        items = [(k, v) for k, v in self.reasons.items() if v != 0]
        items.sort(key=lambda kv: -abs(kv[1]))
        return items[:n]


class Scorer:
    """Stateful scorer: caches per-server lookups so a burst stay cheap."""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self._server_cache: dict[str, tuple[int, float]] = {}
        self._server_ttl = 300.0
        self._reputation_cache: dict[str, tuple[float, float]] = {}
        self._reputation_ttl = 600.0

    # ------------------------------------------------------------------
    async def score(
        self,
        cfg: ProxyConfig,
        *,
        source_channel: str = "",
        source_count: int = 1,
        first_seen: float | None = None,
        dead_reports: int = 0,
        live_reports: int = 0,
        health_ok: bool | None = None,
        latency_ms: int | None = None,
    ) -> ScoreResult:
        reasons: dict[str, float] = {}

        base = float(PROTOCOL_BASE.get(cfg.protocol, 50))
        reasons["protocol"] = round(base, 1)

        transport = float(TRANSPORT_BONUS.get(cfg.network, 0))
        reasons["transport"] = transport

        security = float(SECURITY_BONUS.get(cfg.security, 0))
        reasons["encryption"] = security

        # --- freshness: linear decay over the first ~30 hours -------------
        age_hours = (time.time() - (first_seen or time.time())) / 3600.0
        freshness = max(0.0, 14.0 - age_hours * 0.45)
        reasons["freshness"] = round(freshness, 1)

        # --- corroboration: independent channels agreeing -----------------
        corroboration = min(12.0, max(0, source_count - 1) * 4.0)
        reasons["corroboration"] = corroboration

        # --- source reputation --------------------------------------------
        rep = 0.0
        if source_channel:
            rep = await self._reputation(source_channel)
        reasons["source"] = round(rep * 18, 1)

        # --- health probe ---------------------------------------------------
        health = 0.0
        if health_ok is True:
            health = 12.0 if (latency_ms or 9999) < 300 else 7.0
            if (latency_ms or 0) > 900:
                health = 2.0
        elif health_ok is False:
            health = -40.0
        if health:
            reasons["health"] = health

        # --- crowd feedback --------------------------------------------------
        feedback = 0.0
        if dead_reports:
            feedback -= 12.0 * min(5, dead_reports)
        if live_reports:
            feedback += 2.0 * min(5, live_reports)
        if feedback:
            reasons["feedback"] = round(feedback, 1)

        # --- anti-spam -------------------------------------------------------
        spam = 0.0
        on_server = await self._server_count(cfg.server)
        if on_server > 30:
            spam -= 18.0
        elif on_server > 12:
            spam -= 10.0
        lowered = (cfg.remark or "").lower()
        if any(word in lowered for word in _SPAM_WORDS):
            spam -= 8.0
        if (cfg.security or "") == "none" and cfg.protocol in {"vless", "vmess", "trojan"}:
            spam -= 6.0
        if spam:
            reasons["anti_spam"] = spam

        total = base + transport + security + freshness + corroboration + (rep * 18)
        total += health + feedback + spam
        return ScoreResult(value=round(_compress(total), 1), reasons=reasons)

    # ------------------------------------------------------------------
    async def _server_count(self, server: str) -> int:
        if not server:
            return 0
        now = time.time()
        cached = self._server_cache.get(server)
        if cached and now - cached[1] < self._server_ttl:
            return cached[0]
        try:
            count = await self.store.count_configs_on_server(server)
        except Exception:  # noqa: BLE001
            count = 0
        self._server_cache[server] = (count, now)
        if len(self._server_cache) > 2000:
            self._server_cache.clear()
        return count

    async def _reputation(self, channel: str) -> float:
        now = time.time()
        cached = self._reputation_cache.get(channel)
        if cached and now - cached[1] < self._reputation_ttl:
            return cached[0]
        try:
            rep = await self.store.channel_reputation(channel)
        except Exception:  # noqa: BLE001
            rep = 0.0
        self._reputation_cache[channel] = (rep, now)
        return rep

    # ------------------------------------------------------------------
    def invalidate(self) -> None:
        self._server_cache.clear()
        self._reputation_cache.clear()


__all__ = ["Scorer", "ScoreResult"]
