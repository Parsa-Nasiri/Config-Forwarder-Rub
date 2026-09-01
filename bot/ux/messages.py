"""Message rendering.

Every function returns a ``(text, keypad, metadata)`` triple. ``metadata`` is
an optional Rubika rich-text payload (bold title); the send helper degrades
gracefully to plain text if Rubika ever rejects it.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from ..engine.dispatcher import QueuedItem
from ..engine.models import ProxyConfig
from ..storage.base import User
from .i18n import t

SEPARATOR = "────────────────────────────"
MAX_URI_IN_CARD = 480


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _utf16_len(text: str) -> int:
    """Rubika (like Telegram) counts offsets in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def title_metadata(text: str) -> dict[str, Any] | None:
    """Bold the first line of a message."""
    first_line = text.split("\n", 1)[0]
    length = _utf16_len(first_line)
    if not length or length > 160:
        return None
    return {"meta_data_parts": [{"type": "Bold", "from_index": 0, "length": length}]}


def grade_of(score: float, lang: str = "en") -> str:
    if score >= 88:
        return t(lang, "grade_excellent")
    if score >= 72:
        return t(lang, "grade_good")
    if score >= 58:
        return t(lang, "grade_fair")
    return t(lang, "grade_weak")


def time_ago(timestamp: float, lang: str = "en") -> str:
    delta = max(0, int(time.time() - timestamp))
    if delta < 90:
        return t(lang, "fresh_now")
    if delta < 5400:
        return t(lang, "fresh_min", n=max(1, delta // 60))
    return t(lang, "fresh_hour", n=delta // 3600)


def describe(cfg: ProxyConfig) -> str:
    """'🇩🇪 Germany · Hetzner FSN1' style location line."""
    parts: list[str] = []
    if cfg.geo:
        parts.append(cfg.geo)
    remark = cfg.short_remark
    if remark and remark.lower() not in (cfg.geo or "").lower():
        parts.append(remark)
    return " · ".join(parts) or t("en", "unknown_geo")


def _detail_line(cfg: ProxyConfig) -> str:
    bits: list[str] = [f":{cfg.port}"]
    if cfg.network and cfg.network not in {"tcp", ""}:
        bits.append(cfg.network)
    if cfg.security:
        bits.append(cfg.security)
    if cfg.sni:
        bits.append(f"sni {cfg.sni}")
    if cfg.path and cfg.path not in {"/", ""}:
        bits.append(f"path {cfg.path[:28]}")
    method = (cfg.extras or {}).get("method")
    if method and cfg.protocol in {"ss", "ssr"}:
        bits.append(str(method))
    return " · ".join(bits)


def _card(
    *,
    index: int,
    heading: str,
    score: float,
    lang: str,
    location: str,
    detail: str,
    raw: str,
    age: float | None = None,
    seen: int = 0,
) -> str:
    numbers = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
    marker = numbers[index] if index < len(numbers) else f"{index + 1}."

    meta_bits: list[str] = [f"⭐ {score:.0f}", grade_of(score, lang)]
    if age:
        meta_bits.append(time_ago(age, lang))
    if seen > 1:
        meta_bits.append(t(lang, "seen_times", n=seen))

    uri = raw if len(raw) <= MAX_URI_IN_CARD else raw[:MAX_URI_IN_CARD] + "…"
    return (
        f"{marker} {heading}\n"
        f"     {' · '.join(meta_bits)}\n"
        f"     {location}\n"
        f"     {detail}\n"
        f"     {uri}"
    )


def _card_for_item(item: QueuedItem, index: int, lang: str) -> str:
    cfg = item.cfg
    assert cfg is not None
    heading = cfg.label
    if cfg.security:
        heading += f" · {cfg.security.capitalize()}"
    return _card(
        index=index,
        heading=heading,
        score=item.score,
        lang=lang,
        location=describe(cfg),
        detail=_detail_line(cfg),
        raw=cfg.raw,
    )


def _card_for_row(row: dict[str, Any], index: int, lang: str) -> str:
    raw = str(row.get("raw") or "")
    cfg = ProxyConfig(
        protocol=str(row.get("protocol") or ""),
        server=str(row.get("server") or ""),
        port=int(row.get("port") or 0),
        network=str(row.get("network") or ""),
        security=str(row.get("security") or ""),
        remark=str(row.get("remark") or ""),
        raw=raw,
        geo=str(row.get("geo") or ""),
    )
    heading = cfg.label
    if cfg.security:
        heading += f" · {cfg.security.capitalize()}"
    return _card(
        index=index,
        heading=heading,
        score=float(row.get("score") or 0),
        lang=lang,
        location=describe(cfg),
        detail=_detail_line(cfg),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------


def render_welcome(user: User, channel_count: int) -> tuple[str, dict, dict | None]:
    lang = user.language
    text = (
        f"🚀 {t(lang, 'welcome_title')}\n"
        f"{SEPARATOR}\n\n"
        f"{t(lang, 'welcome_body', channels=channel_count, batch=user.max_per_batch, min_score=user.min_score)}\n\n"
        f"{t(lang, 'welcome_hint')}"
    )
    from ..rubika_side.keypads import main_chat_keypad

    return text, main_chat_keypad(lang), title_metadata(text)


def render_help(user: User) -> tuple[str, dict, dict | None]:
    lang = user.language
    text = f"📖 {t(lang, 'btn_help')}\n{SEPARATOR}\n\n{t(lang, 'help_body')}"
    from ..rubika_side.keypads import main_chat_keypad

    return text, main_chat_keypad(lang), title_metadata(text)


def render_batch(
    user: User, items: Sequence[QueuedItem], batch_id: str, seq: int
) -> tuple[str, dict, dict | None]:
    """The main event: a digest of freshly scored configs."""
    from ..rubika_side.keypads import batch_keypad

    lang = user.language
    count = len(items)
    header = f"🚀 {t(lang, 'digest_title', count=count, s='' if count == 1 else 's')}"

    blocks = [_card_for_item(item, idx, lang) for idx, item in enumerate(items)]
    body = f"\n{SEPARATOR}\n".join(blocks)
    footer = (
        f"{SEPARATOR}\n"
        f"📦 {t(lang, 'digest_footer', seq=seq, count=count, s='' if count == 1 else 's', min_score=user.min_score)}"
    )
    text = f"{header}\n\n{body}\n{footer}"
    return text, batch_keypad(lang, batch_id, count), title_metadata(text)


def render_single(
    user: User, cfg: ProxyConfig, score: float, index: int, batch_id: str, seen: int = 0
) -> tuple[str, dict, dict | None]:
    """Expanded view of one config - optimised for a clean long-press copy."""
    from ..rubika_side.keypads import single_keypad

    lang = user.language
    heading = cfg.label
    if cfg.security:
        heading += f" · {cfg.security.capitalize()}"
    card = _card(
        index=index,
        heading=heading,
        score=score,
        lang=lang,
        location=describe(cfg),
        detail=_detail_line(cfg),
        raw=cfg.raw,
        age=time.time(),
        seen=seen,
    )
    text = f"{t(lang, 'copy_header', idx=index + 1)}\n{SEPARATOR}\n\n{card}"
    # Send the clean URI as its own last block so it is trivial to select.
    text += f"\n\n{SEPARATOR}\n{cfg.raw}"
    return text, single_keypad(lang, batch_id, index), title_metadata(text)


def render_copy_all(
    user: User, cfgs: Sequence[ProxyConfig]
) -> tuple[str, dict, dict | None]:
    lang = user.language
    text = (
        f"{t(lang, 'copy_all_header', count=len(cfgs))}\n"
        f"{SEPARATOR}\n\n"
        + "\n".join(cfg.raw for cfg in cfgs)
    )
    return text, {}, title_metadata(text)


def render_stats(
    user: User,
    stats: dict[str, int],
    counters: dict[str, int],
    last_hour: int,
    delivered_to_user: int,
) -> tuple[str, dict, dict | None]:
    from ..rubika_side.keypads import stats_keypad

    lang = user.language
    protocols = ", ".join(user.protocols) if user.protocols else t(lang, "filters_all")
    live = t(lang, "on") if user.live_mode else t(lang, "off")
    text = (
        f"📊 {t(lang, 'stats_title')}\n"
        f"{SEPARATOR}\n\n"
        + t(
            lang,
            "stats_body",
            delivered=delivered_to_user,
            last_hour=last_hour,
            min_score=user.min_score,
            protocols=protocols,
            live=live,
        )
        + "\n"
        + t(
            lang,
            "stats_global",
            configs=stats.get("configs", 0),
            users=stats.get("users", 0),
            deliveries=stats.get("delivered", 0),
        )
    )
    return text, stats_keypad(lang), title_metadata(text)


def render_filters(user: User) -> tuple[str, dict, dict | None]:
    from ..rubika_side.keypads import filters_keypad

    lang = user.language
    protocols = ", ".join(user.protocols) if user.protocols else t(lang, "filters_all")
    live = t(lang, "on") if user.live_mode else t(lang, "off")
    text = (
        f"⚙️ {t(lang, 'filters_title')}\n"
        f"{SEPARATOR}\n\n"
        + t(
            lang,
            "filters_body",
            protocols=protocols,
            min_score=user.min_score,
            batch=user.max_per_batch,
            live=live,
        )
    )
    return text, filters_keypad(lang, user), title_metadata(text)


def render_top(user: User, rows: Sequence[dict[str, Any]]) -> tuple[str, dict, dict | None]:
    from ..rubika_side.keypads import stats_keypad

    lang = user.language
    if not rows:
        text = f"🏆 {t(lang, 'btn_top')}\n{SEPARATOR}\n\n{t(lang, 'digest_empty')}"
        return text, stats_keypad(lang), title_metadata(text)

    blocks = [_card_for_row(row, idx, lang) for idx, row in enumerate(rows)]
    text = (
        f"🏆 {t(lang, 'btn_top')}\n"
        f"{SEPARATOR}\n\n" + f"\n{SEPARATOR}\n".join(blocks)
    )
    return text, stats_keypad(lang), title_metadata(text)


def render_message(user: User, key: str, **kwargs: Any) -> tuple[str, dict, dict | None]:
    """One-off informational message with the main bar attached."""
    from ..rubika_side.keypads import main_chat_keypad

    lang = user.language
    text = t(lang, key, **kwargs)
    return text, main_chat_keypad(lang), title_metadata(text)


__all__ = [
    "render_welcome", "render_help", "render_batch", "render_single",
    "render_copy_all", "render_stats", "render_filters", "render_top",
    "render_message", "title_metadata", "grade_of", "time_ago",
]
