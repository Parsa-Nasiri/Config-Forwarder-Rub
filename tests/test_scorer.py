"""Scoring tests: the score must react to evidence the way users expect."""

from __future__ import annotations

from bot.engine.extractor import parse_uri
from bot.engine.models import ProxyConfig

from .conftest import run


def _cfg(**overrides) -> ProxyConfig:
    base = dict(
        protocol="vless",
        server="score.example.net",
        port=443,
        identity="aaaaaaaa-1111-2222-3333-444444444444",
        network="ws",
        security="tls",
        sni="cdn.example.net",
    )
    base.update(overrides)
    return ProxyConfig(**base)


def test_score_is_bounded(scorer):
    result = run(scorer.score(_cfg(security="reality", network="grpc"), source_count=9))
    assert 0.0 <= result.value <= 100.0


def test_reality_beats_plain_tcp(scorer):
    best = run(scorer.score(_cfg(security="reality", network="grpc"))).value
    worst = run(scorer.score(_cfg(security="none", network="tcp"))).value
    assert best > worst + 20


def test_vless_beats_ssr(scorer):
    vless = run(scorer.score(_cfg(protocol="vless"))).value
    ssr = run(scorer.score(_cfg(protocol="ssr"))).value
    assert vless > ssr


def test_fresh_beats_stale(scorer):
    import time

    fresh = run(scorer.score(_cfg(), first_seen=time.time())).value
    stale = run(scorer.score(_cfg(), first_seen=time.time() - 48 * 3600)).value
    assert fresh > stale


def test_corroboration_raises_the_score(scorer):
    solo = run(scorer.score(_cfg(), source_count=1)).value
    agreed = run(scorer.score(_cfg(), source_count=4)).value
    assert agreed > solo


def test_dead_reports_sink_the_score(scorer):
    clean = run(scorer.score(_cfg(), dead_reports=0)).value
    dead = run(scorer.score(_cfg(), dead_reports=4)).value
    assert dead < clean - 40


def test_live_reports_lift_the_score(scorer):
    neutral = run(scorer.score(_cfg())).value
    loved = run(scorer.score(_cfg(), live_reports=5)).value
    assert loved > neutral


def test_failed_probe_is_punished_hard(scorer):
    unknown = run(scorer.score(_cfg(), health_ok=None)).value
    fast = run(scorer.score(_cfg(), health_ok=True, latency_ms=80)).value
    dead = run(scorer.score(_cfg(), health_ok=False)).value
    assert fast > unknown > dead


def test_slow_probe_gets_less_credit_than_fast(scorer):
    fast = run(scorer.score(_cfg(), health_ok=True, latency_ms=60)).value
    slow = run(scorer.score(_cfg(), health_ok=True, latency_ms=1500)).value
    assert fast > slow


def test_spam_remark_is_penalised(scorer):
    plain = run(scorer.score(_cfg(remark="FRA-01"))).value
    spammy = run(scorer.score(_cfg(remark="BUY NOW cheap vpn shop order"))).value
    assert spammy < plain


def test_bad_channel_reputation_is_penalised(store, settings, scorer):
    """A channel that publishes dead configs must lose standing."""
    from bot.engine.scorer import Scorer

    run(store.bump_channel("badchan", "total", 100))
    run(store.bump_channel("badchan", "dead", 90))

    good = run(scorer.score(_cfg(), source_channel="unknownchan")).value
    bad = run(scorer.score(_cfg(), source_channel="badchan")).value
    assert bad < good - 5

    # ...and the cache must not hide an updated reputation forever.
    scorer.invalidate()
    fresh = Scorer(store, settings)
    assert run(fresh.score(_cfg(), source_channel="badchan")).value < good - 5


def test_flooded_server_is_penalised(store, scorer):
    """One host advertising 400 ports is a scanner, not a service."""
    for i in range(40):
        run(
            store.add_config(
                fingerprint=f"flood{i}",
                protocol="ss",
                server="flood.example.net",
                port=10000 + i,
                remark="",
                raw="",
                score=50,
                geo="",
                network="tcp",
                security="",
                source_channel="c",
                source_message="1",
            )
        )
    flooded = run(scorer.score(_cfg(server="flood.example.net"))).value
    clean = run(scorer.score(_cfg(server="clean.example.net"))).value
    assert flooded < clean


def test_grades(scorer):
    from bot.engine.scorer import ScoreResult

    assert ScoreResult(95).grade == "excellent"
    assert ScoreResult(75).grade == "good"
    assert ScoreResult(60).grade == "fair"
    assert ScoreResult(20).grade == "weak"


def test_top_reasons_returns_the_biggest_signals(scorer):
    result = run(scorer.score(_cfg(security="reality")))
    names = [name for name, _ in result.top_reasons(3)]
    assert "protocol" in names
    assert "encryption" in names


def test_real_world_uri_scores_reasonably(scorer):
    from .conftest import VLESS_REALITY

    cfg = parse_uri(VLESS_REALITY)
    assert cfg
    result = run(scorer.score(cfg))
    assert result.value >= 80, f"a reality+grpc vless should score high, got {result.value}"
