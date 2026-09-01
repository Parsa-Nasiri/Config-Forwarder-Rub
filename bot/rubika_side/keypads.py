"""Keypad builders.

Rubika models a keypad as::

    {"rows": [{"buttons": [{"id": ..., "type": "Simple", "button_text": ...}]}],
     "resize_keyboard": true, "one_time_keyboard": false}

``chat_keypad`` renders as the persistent bottom bar, ``inline_keypad`` as the
glass buttons under a message. Button ``id`` values are what come back in
``aux_data.button_id`` when a user taps, so we encode a short action token
into them.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..storage.base import User
from ..ux.i18n import t

# Short action tokens (kept tiny because they travel in the button id).
CB_COPY = "cp"        # cp:<batch>:<idx>
CB_ALL = "all"        # all:<batch>
CB_DEAD = "dd"        # dd:<batch>:<idx>
CB_WORKS = "wk"       # wk:<batch>:<idx>
CB_NOW = "now"
CB_STATS = "st"
CB_FILTERS = "fl"
CB_PAUSE = "ps"       # ps:<hours>
CB_RESUME = "rs"
CB_HELP = "hp"
CB_LANG = "lg"        # lg:<code>
CB_MENU = "mn"
CB_TOP = "tp"
CB_SCORE = "sc"       # sc:<value>
CB_PROTO = "pr"       # pr:<protocol|all>
CB_BATCHSIZE = "bs"   # bs:<n>
CB_LIVE = "lv"        # lv:<0|1>
CB_CLOSE = "cl"

PROTOCOLS = ("vless", "vmess", "trojan", "ss", "hysteria2", "tuic")
SCORE_CHOICES = (40, 55, 70, 85)
BATCH_CHOICES = (1, 3, 5, 10)
PAUSE_CHOICES = (1, 6, 24)


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def button(btn_id: str, text: str, btype: str = "Simple", **extra: Any) -> dict[str, Any]:
    btn: dict[str, Any] = {"id": btn_id, "type": btype, "button_text": text}
    btn.update(extra)
    return btn


def row(*buttons: dict[str, Any]) -> dict[str, Any]:
    return {"buttons": list(buttons)}


def inline_keypad(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Glass buttons attached underneath a message."""
    return {"rows": list(rows)}


def chat_keypad(rows: Sequence[dict[str, Any]], *, resize: bool = True,
                one_time: bool = False) -> dict[str, Any]:
    """Persistent bottom bar."""
    return {
        "rows": list(rows),
        "resize_keyboard": resize,
        "one_time_keyboard": one_time,
    }


# ---------------------------------------------------------------------------
# app keypads
# ---------------------------------------------------------------------------


def main_chat_keypad(lang: str) -> dict[str, Any]:
    """Always-available bottom bar."""
    return chat_keypad(
        [
            row(
                button(CB_NOW, f"⚡ {t(lang, 'btn_send_now')}"),
                button(CB_FILTERS, f"⚙️ {t(lang, 'btn_filters')}"),
            ),
            row(
                button(CB_STATS, f"📊 {t(lang, 'btn_stats')}"),
                button(CB_PAUSE + ":1", f"⏸ {t(lang, 'btn_pause')}"),
                button(CB_HELP, f"❓ {t(lang, 'btn_help')}"),
            ),
        ]
    )


def batch_keypad(lang: str, batch_id: str, count: int) -> dict[str, Any]:
    """Inline keypad under a config digest."""
    rows: list[dict[str, Any]] = []

    # One button per config, max three per row.
    numbers = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
    per_row = 3
    for start in range(0, min(count, 10), per_row):
        buttons = []
        for idx in range(start, min(start + per_row, count, 10)):
            buttons.append(button(f"{CB_COPY}:{batch_id}:{idx}", numbers[idx]))
        rows.append(row(*buttons))

    rows.append(
        row(
            button(f"{CB_ALL}:{batch_id}", f"📦 {t(lang, 'btn_all')}"),
            button(CB_NOW, f"⚡ {t(lang, 'btn_send_now')}"),
            button(f"{CB_PAUSE}:1", f"⏸ {t(lang, 'btn_pause')}"),
        )
    )
    rows.append(
        row(
            button(CB_FILTERS, f"⚙️ {t(lang, 'btn_filters')}"),
            button(CB_STATS, f"📊 {t(lang, 'btn_stats')}"),
            button(CB_LANG, f"🌐 {t(lang, 'btn_lang')}"),
        )
    )
    return inline_keypad(rows)


def single_keypad(lang: str, batch_id: str, idx: int) -> dict[str, Any]:
    """Inline keypad under a single expanded config."""
    return inline_keypad(
        [
            row(
                button(f"{CB_COPY}:{batch_id}:{idx}", f"📋 {t(lang, 'btn_copy')}"),
                button(f"{CB_WORKS}:{batch_id}:{idx}", f"✅ {t(lang, 'btn_works')}"),
                button(f"{CB_DEAD}:{batch_id}:{idx}", f"❌ {t(lang, 'btn_dead')}"),
            ),
            row(button(CB_MENU, f"🏠 {t(lang, 'btn_menu')}")),
        ]
    )


def filters_keypad(lang: str, user: User) -> dict[str, Any]:
    live_state = t(lang, "on") if user.live_mode else t(lang, "off")
    return inline_keypad(
        [
            row(button("fl:proto", f"🔌 {t(lang, 'btn_protocols')}")),
            row(
                button("fl:score", f"⭐ {t(lang, 'btn_score')}"),
                button("fl:batch", f"📦 {t(lang, 'btn_batchsize')}"),
            ),
            row(button(f"{CB_LIVE}:{0 if user.live_mode else 1}", f"⚡ {t(lang, 'btn_send_now')} · {live_state}")),
            row(
                button(CB_STATS, f"📊 {t(lang, 'btn_stats')}"),
                button(CB_MENU, f"🏠 {t(lang, 'btn_menu')}"),
            ),
        ]
    )


def protocol_keypad(lang: str, selected: Sequence[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    current = 0
    buttons: list[dict[str, Any]] = []
    for proto in PROTOCOLS:
        mark = "✅" if proto in selected else "⬜"
        buttons.append(button(f"{CB_PROTO}:{proto}", f"{mark} {proto}"))
        current += 1
        if current == 2:
            rows.append(row(*buttons))
            buttons, current = [], 0
    if buttons:
        rows.append(row(*buttons))
    rows.append(row(button(f"{CB_PROTO}:all", f"🌐 {t(lang, 'filters_all')}")))
    rows.append(row(button(CB_FILTERS, f"↩️ {t(lang, 'btn_back')}")))
    return inline_keypad(rows)


def score_keypad(lang: str, current: int) -> dict[str, Any]:
    buttons = [
        button(f"{CB_SCORE}:{value}", f"{'✅' if value == current else '⬜'} {value}+")
        for value in SCORE_CHOICES
    ]
    return inline_keypad(
        [
            row(*buttons[:2]),
            row(*buttons[2:]),
            row(button(CB_FILTERS, f"↩️ {t(lang, 'btn_back')}")),
        ]
    )


def batchsize_keypad(lang: str, current: int) -> dict[str, Any]:
    buttons = [
        button(f"{CB_BATCHSIZE}:{value}", f"{'✅' if value == current else '⬜'} {value}")
        for value in BATCH_CHOICES
    ]
    return inline_keypad(
        [
            row(*buttons[:2]),
            row(*buttons[2:]),
            row(button(CB_FILTERS, f"↩️ {t(lang, 'btn_back')}")),
        ]
    )


def language_keypad() -> dict[str, Any]:
    return inline_keypad([row(button(f"{CB_LANG}:en", "🇬🇧 English"),
                              button(f"{CB_LANG}:fa", "🇮🇷 فارسی"))])


def pause_keypad(lang: str) -> dict[str, Any]:
    buttons = [button(f"{CB_PAUSE}:{h}", f"⏸ {h}h") for h in PAUSE_CHOICES]
    return inline_keypad(
        [
            row(*buttons),
            row(button(CB_RESUME, f"▶️ {t(lang, 'btn_menu')}")),
        ]
    )


def stats_keypad(lang: str) -> dict[str, Any]:
    return inline_keypad(
        [
            row(
                button(CB_NOW, f"⚡ {t(lang, 'btn_send_now')}"),
                button(CB_TOP, f"🏆 {t(lang, 'btn_top')}"),
            ),
            row(button(CB_MENU, f"🏠 {t(lang, 'btn_menu')}")),
        ]
    )


__all__ = [
    "button", "row", "inline_keypad", "chat_keypad",
    "main_chat_keypad", "batch_keypad", "single_keypad",
    "filters_keypad", "protocol_keypad", "score_keypad",
    "batchsize_keypad", "language_keypad", "pause_keypad", "stats_keypad",
]
