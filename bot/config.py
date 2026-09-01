"""Central settings object.

Every knobs lives here so the rest of the codebase never touches ``os.environ``
directly. All values are read once at startup from environment variables,
which makes the bot behave identically on a laptop and inside GitHub Actions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# env helpers
# --------------------------------------------------------------------------


def _s(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _i(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _b(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "enabled"}


def _l(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    out: list[str] = []
    for part in raw.replace("\n", ",").replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


# --------------------------------------------------------------------------
# scoring defaults - the "what is a good config" prior
# --------------------------------------------------------------------------

# Baseline desirability of each protocol. Deliberately mid-range so the
# modifiers can push a great config up to ~95 and drag a dead one below the
# delivery floor - if the bases sat near 100 the clamp would flatten the
# ranking and every positive delta above it would disappear.
PROTOCOL_BASE: dict[str, int] = {
    "vless": 58,
    "vmess": 54,
    "trojan": 56,
    "hysteria2": 54,
    "tuic": 46,
    "wireguard": 52,
    "ss": 44,
    "snell": 32,
    "ssr": 24,
}

# How the transport layer shifts the score.
TRANSPORT_BONUS: dict[str, int] = {
    "grpc": 7,
    "ws": 5,
    "httpupgrade": 4,
    "splithttp": 4,
    "xhttp": 4,
    "h2": 4,
    "quic": 6,
    "http": 2,
    "tcp": 0,
    "": 0,
}

# How the encryption layer shifts the score.
SECURITY_BONUS: dict[str, int] = {
    "reality": 16,
    "xtls": 11,
    "tls": 9,
    "": 0,
    "none": -14,
    "plain": -20,
}


@dataclass
class Settings:
    """Fully-resolved runtime configuration."""

    # --- Telegram ---
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: str = ""
    telegram_channels: list[str] = field(default_factory=list)
    telegram_catchup_limit: int = 50
    telegram_parse_documents: bool = True

    # --- Rubika ---
    rubika_token: str = ""
    rubika_api_base: str = "https://botapi.rubika.ir/v3"
    rubika_poll_interval: float = 2.0

    # --- Supabase ---
    supabase_url: str = ""
    supabase_key: str = ""

    # --- delivery engine ---
    min_score: int = 55
    instant_score: int = 88
    batch_window: float = 45.0
    batch_max: int = 5
    user_hourly_cap: int = 60
    rate_limit_per_chat: float = 12.0
    rate_limit_global: float = 120.0
    health_check_enabled: bool = False
    health_check_timeout: float = 6.0

    # --- runtime ---
    max_runtime: int = 20400
    handoff_lead: int = 180
    log_level: str = "INFO"
    default_language: str = "en"
    instance_id: str = "local"

    # --- GitHub handoff ---
    github_repo: str = ""
    github_token: str = ""
    github_run_id: str = ""
    heartbeat_file: str = "status/last-run.json"

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            telegram_api_id=_i("TELEGRAM_API_ID", 0),
            telegram_api_hash=_s("TELEGRAM_API_HASH"),
            telegram_session=_s("TELEGRAM_SESSION_STRING"),
            telegram_channels=_l("TELEGRAM_CHANNELS"),
            telegram_catchup_limit=_i("TELEGRAM_CATCHUP_LIMIT", 50),
            telegram_parse_documents=_b("TELEGRAM_PARSE_DOCUMENTS", True),
            rubika_token=_s("RUBIKA_BOT_TOKEN"),
            rubika_api_base=_s("RUBIKA_API_BASE", "https://botapi.rubika.ir/v3"),
            rubika_poll_interval=_f("RUBIKA_POLL_INTERVAL", 2.0),
            supabase_url=_s("SUPABASE_URL"),
            supabase_key=_s("SUPABASE_SERVICE_KEY") or _s("SUPABASE_KEY"),
            min_score=_i("MIN_SCORE", 55),
            instant_score=_i("INSTANT_SCORE", 88),
            batch_window=_f("BATCH_WINDOW", 45.0),
            batch_max=_i("BATCH_MAX", 5),
            user_hourly_cap=_i("USER_HOURLY_CAP", 60),
            rate_limit_per_chat=_f("RATE_LIMIT_PER_CHAT", 12.0),
            rate_limit_global=_f("RATE_LIMIT_GLOBAL", 120.0),
            health_check_enabled=_b("HEALTH_CHECK_ENABLED", False),
            health_check_timeout=_f("HEALTH_CHECK_TIMEOUT", 6.0),
            max_runtime=_i("MAX_RUNTIME", 20400),
            handoff_lead=_i("HANDOFF_LEAD", 180),
            log_level=_s("LOG_LEVEL", "INFO").upper(),
            default_language=_s("DEFAULT_LANGUAGE", "en").lower(),
            instance_id=_s("INSTANCE_ID") or _s("GITHUB_RUN_ID") or "local",
            github_repo=_s("GITHUB_REPOSITORY"),
            github_token=_s("GITHUB_TOKEN"),
            github_run_id=_s("GITHUB_RUN_ID"),
            heartbeat_file=_s("HEARTBEAT_FILE", "status/last-run.json"),
        )
        s.validate()
        return s

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Never raise - a misconfigured optional integration just disables it."""
        if self.rubika_token and "botapi" not in self.rubika_api_base:
            # tolerate a user pasting the full base url with the token slot
            self.rubika_api_base = self.rubika_api_base.rstrip("/")

        if self.telegram_catchup_limit < 0:
            self.telegram_catchup_limit = 0
        if self.batch_max < 1:
            self.batch_max = 1
        if self.batch_window < 1:
            self.batch_window = 1.0
        if self.max_runtime < 120:
            self.max_runtime = 120
        if self.handoff_lead < 10:
            self.handoff_lead = 10
        if self.handoff_lead > self.max_runtime // 2:
            self.handoff_lead = max(10, self.max_runtime // 2)
        if self.default_language not in {"en", "fa"}:
            self.default_language = "en"

    # ------------------------------------------------------------------
    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash and self.telegram_session)

    @property
    def rubika_enabled(self) -> bool:
        return bool(self.rubika_token)

    def problems(self) -> list[str]:
        """Human readable list of missing pieces (used by /status and startup)."""
        out: list[str] = []
        if not self.rubika_enabled:
            out.append("RUBIKA_BOT_TOKEN is missing - the Rubika side is disabled.")
        if not self.telegram_enabled:
            out.append(
                "Telegram credentials/session missing - no new configs will be "
                "collected (run scripts/auth_telegram.py)."
            )
        elif not self.telegram_channels:
            out.append("TELEGRAM_CHANNELS is empty - there is no channel to watch.")
        if not self.supabase_enabled:
            out.append(
                "Supabase is not configured - state is ephemeral and resets on "
                "every GitHub Actions run."
            )
        return out
