"""UX tests: every renderer must produce readable output in both languages."""

from __future__ import annotations

import time

from bot.engine.models import ProxyConfig
from bot.storage.base import User
from bot.ux import messages as M
from bot.ux.i18n import STRINGS, t

from .conftest import run


def _user(lang: str = "en", **kw) -> User:
    return User(chat_id="c1", language=lang, **kw)


def _cfg(server: str = "de-fra01.example.net", protocol: str = "vless",
         security: str = "reality", network: str = "grpc",
         remark: str = "🇩🇪 FRA-01 | Reality") -> ProxyConfig:
    uri = f"{protocol}://aaaaaaaa-1111-2222-3333-444444444444@{server}:443"
    return ProxyConfig(
        protocol=protocol, server=server, port=443,
        identity="aaaaaaaa-1111-2222-3333-444444444444",
        network=network, security=security, sni="www.microsoft.com",
        remark=remark, geo="🇩🇪 Germany", raw=uri,
    )


# ---------------------------------------------------------------------------
# i18n completeness
# ---------------------------------------------------------------------------


def test_both_languages_have_the_same_keys():
    assert set(STRINGS["en"]) == set(STRINGS["fa"])
    assert len(STRINGS["en"]) >= 50


def test_no_string_is_left_empty():
    for lang, table in STRINGS.items():
        for key, value in table.items():
            assert isinstance(value, str) and value.strip(), f"{lang}.{key} is empty"


def test_safe_format_leaves_missing_placeholders_intact():
    # Persian has no plural -s; calling without `s` must not raise and must
    # not eat the rest of the template.
    out = t("fa", "digest_title", count=3)
    assert out.strip()
    assert "3" in out


def test_unknown_key_is_still_usable():
    assert t("en", "this_key_does_not_exist").strip()


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------


def test_render_welcome_mentions_personal_settings():
    user = _user(max_per_batch=3, min_score=70)
    text, keypad, metadata = M.render_welcome(user, channel_count=12)
    assert text.strip()
    assert keypad and "rows" in keypad          # the persistent bottom bar
    assert "12" in text                         # channel count
    assert metadata is not None                 # bold title


def test_render_help_explains_the_bot():
    text, keypad, _ = M.render_help(_user())
    assert text.strip()
    assert "rows" in keypad


def test_render_batch_shows_every_config_and_a_keypad():
    from bot.engine.dispatcher import QueuedItem

    items = [
        QueuedItem("fp1", 92.0, "vless", time.time(), _cfg("a.example.net")),
        QueuedItem("fp2", 70.0, "trojan", time.time(),
                   _cfg("b.example.net", protocol="trojan", security="tls")),
    ]
    text, keypad, metadata = M.render_batch(_user(), items, "batch-1", 7)
    assert "a.example.net" in text
    assert "b.example.net" in text
    assert "92" in text                              # the score is visible
    assert keypad and "rows" in keypad               # numbered copy buttons
    assert metadata is not None
    # The bold title must cover exactly the first line, in UTF-16 units.
    part = metadata["meta_data_parts"][0]
    assert part["type"] == "Bold" and part["from_index"] == 0
    first_line = text.split("\n", 1)[0]
    assert part["length"] == len(first_line.encode("utf-16-le")) // 2


def test_render_batch_works_in_persian():
    from bot.engine.dispatcher import QueuedItem

    items = [QueuedItem("fp1", 90.0, "vless", time.time(), _cfg())]
    text, keypad, _ = M.render_batch(_user("fa"), items, "batch-1", 1)
    assert text.strip()
    assert "de-fra01.example.net" in text             # hostnames stay latin
    assert keypad and "rows" in keypad


def test_render_batch_empty_is_graceful():
    text, keypad, metadata = M.render_batch(_user(), [], "batch-1", 1)
    assert isinstance(text, str)


def test_render_single_exposes_the_uri_for_copying():
    cfg = _cfg()
    text, keypad, _ = M.render_single(_user(), cfg, 91.0, 1, "batch-1")
    assert cfg.raw in text                          # the URI is included
    assert text.rstrip().endswith(cfg.raw)           # as a clean last block


def test_render_copy_all_lists_every_uri():
    cfgs = [_cfg("a.example.net"), _cfg("b.example.net", protocol="trojan")]
    text, keypad, _ = M.render_copy_all(_user(), cfgs)
    assert "a.example.net" in text
    assert "b.example.net" in text


def test_render_stats_reports_real_numbers():
    from bot.storage.memory_store import MemoryStore

    store = MemoryStore()
    run(store.upsert_user(User(chat_id="c1")))
    run(store.upsert_user(User(chat_id="c2")))
    for i in range(3):
        run(store.add_config(
            fingerprint=f"fp{i}", protocol="vless", server=f"s{i}.example.net",
            port=443, remark="n", raw="r", score=80, geo="", network="ws",
            security="tls", source_channel="chan", source_message="1",
        ))
    stats = run(store.stats())
    text, keypad, _ = M.render_stats(_user("en"), stats, counters={}, last_hour=0,
                                     delivered_to_user=5)
    assert "3" in text                              # config count
    assert "2" in text                              # user count


def test_render_filters_reflects_user_settings():
    user = _user(protocols=["vless", "trojan"], min_score=70, max_per_batch=3)
    text, keypad, _ = M.render_filters(user)
    lowered = text.lower()
    assert "vless" in lowered
    assert "trojan" in lowered
    assert "70" in text
    assert "rows" in keypad


def test_render_top_lists_the_best_configs():
    rows = [
        {"protocol": "vless", "server": "best.example.net", "score": 95,
         "delivered_count": 12, "remark": "FRA",
         "raw": "vless://aaaaaaaa-1111-2222-3333-444444444444@best.example.net:443",
         "port": 443, "network": "ws", "security": "tls", "geo": "🇩🇪 Germany"},
        {"protocol": "trojan", "server": "ok.example.net", "score": 78,
         "delivered_count": 3, "remark": "IST",
         "raw": "trojan://pw@ok.example.net:443",
         "port": 443, "network": "ws", "security": "tls", "geo": "🇹🇷 Türkiye"},
    ]
    text, keypad, _ = M.render_top(_user(), rows)
    assert "best.example.net" in text               # via the raw URI
    assert "95" in text


def test_render_top_with_no_rows_still_renders():
    text, keypad, _ = M.render_top(_user(), [])
    assert text.strip()


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def test_grades_change_with_score():
    assert M.grade_of(95, "en") == M.grade_of(95, "en")
    assert M.grade_of(95, "en") != M.grade_of(50, "en")


def test_grades_exist_in_both_languages():
    for lang in ("en", "fa"):
        for score in (95, 75, 60, 30):
            assert M.grade_of(score, lang).strip()


def test_time_ago_is_human():
    now = time.time()
    assert M.time_ago(now, "en").strip()
    assert M.time_ago(now - 7200, "en").strip()


def test_describe_shows_location():
    text = M.describe(_cfg())
    assert "Germany" in text                       # from the geo lookup


def test_title_metadata_uses_utf16_offsets():
    text = "🚀 Fresh configs\nsecond line"
    meta = M.title_metadata(text)
    assert meta is not None
    part = meta["meta_data_parts"][0]
    first_line = text.split("\n", 1)[0]
    # The bold range must count the rocket emoji as 2 UTF-16 units.
    assert part["length"] == len(first_line.encode("utf-16-le")) // 2


def test_title_metadata_skips_overlong_titles():
    text = "x" * 300 + "\nbody"
    assert M.title_metadata(text) is None
