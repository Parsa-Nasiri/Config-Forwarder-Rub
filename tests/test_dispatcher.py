"""End-to-end tests of the delivery pipeline with a fake Rubika sender."""

from __future__ import annotations

import asyncio
import time

from bot.engine import Dispatcher, Scorer
from bot.engine.extractor import parse_uri
from bot.engine.models import ProxyConfig
from bot.storage.base import User
from bot.ux import messages as M

from .conftest import VLESS_REALITY, run


class FakeSender:
    """Stands in for RubikaClient.send_message - records what the bot sent."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def __call__(self, chat_id: str, text: str, keypad=None, metadata=None):
        if self.fail:
            raise RuntimeError("rubika exploded")
        self.sent.append({"chat_id": chat_id, "text": text, "keypad": keypad,
                          "metadata": metadata})
        return f"msg-{len(self.sent)}"


def _make(settings, store, sender):
    scorer = Scorer(store, settings)
    return Dispatcher(settings, store, scorer, sender, M.render_batch)


def _cfg(server: str, protocol: str = "vless", security: str = "tls",
         network: str = "ws", score_boost: bool = True) -> ProxyConfig:
    return ProxyConfig(
        protocol=protocol,
        server=server,
        port=443,
        identity="aaaaaaaa-1111-2222-3333-444444444444",
        network=network,
        security=security if score_boost else "none",
        sni="cdn.example.net",
        remark=f"Node {server}",
    )


# ---------------------------------------------------------------------------
# ingest -> fan-out
# ---------------------------------------------------------------------------


def test_ingest_persists_and_fans_out(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1")))
    run(d.refresh_users())

    score = run(d.ingest(_cfg("a.example.net"), source_channel="chan"))
    assert score > settings.min_score
    assert d.counters["ingested"] == 1
    assert d.counters["unique"] == 1
    assert "c1" in d.pending
    assert run(store.get_config(_cfg("a.example.net").fingerprint)) is not None


def test_duplicate_ingest_is_counted_not_requeued(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1")))
    run(d.refresh_users())

    cfg = _cfg("dup.example.net")
    run(d.ingest(cfg, source_channel="chan"))
    before = len(d.pending["c1"])
    run(d.ingest(cfg, source_channel="chan"))
    assert d.counters["duplicates"] == 1
    assert len(d.pending["c1"]) == before          # not queued twice


def test_invalid_config_never_reaches_the_queue(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1")))
    run(d.refresh_users())

    broken = ProxyConfig(protocol="vless", server="broken", port=0, identity="junk")
    assert run(d.ingest(broken)) == 0.0
    assert d.counters["ingested"] == 0
    assert d.pending == {}


def test_low_score_is_rejected_for_everyone(settings, store):
    settings.min_score = 99
    sender = FakeSender()
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1")))
    run(d.refresh_users())

    score = run(d.ingest(_cfg("weak.example.net", protocol="ssr", security="none")))
    assert score < 99
    assert d.counters["rejected_low_score"] == 1
    assert d.pending == {}


# ---------------------------------------------------------------------------
# per-user filtering (the multi-user requirement)
# ---------------------------------------------------------------------------


def test_protocol_filter_isolates_users(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="wants-vless", protocols=["vless"], min_score=0)))
    run(store.upsert_user(User(chat_id="wants-trojan", protocols=["trojan"], min_score=0)))
    run(d.refresh_users())

    run(d.ingest(_cfg("v.example.net", protocol="vless")))
    run(d.ingest(_cfg("t.example.net", protocol="trojan")))

    assert [i.protocol for i in d.pending["wants-vless"]] == ["vless"]
    assert [i.protocol for i in d.pending["wants-trojan"]] == ["trojan"]


def test_min_score_filter_is_personal(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    settings.min_score = 0
    picky = User(chat_id="picky", min_score=95)
    easy = User(chat_id="easy", min_score=0)
    run(store.upsert_user(picky))
    run(store.upsert_user(easy))
    run(d.refresh_users())

    run(d.ingest(_cfg("mid.example.net")))          # scores ~84
    assert "easy" in d.pending
    assert "picky" not in d.pending


def test_paused_user_gets_nothing(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    paused = User(chat_id="paused", is_paused=True, paused_until=time.time() + 3600)
    run(store.upsert_user(paused))
    run(store.upsert_user(User(chat_id="active")))
    run(d.refresh_users())

    run(d.ingest(_cfg("p.example.net")))
    assert "paused" not in d.pending
    assert "active" in d.pending


def test_affinity_reorders_a_users_queue(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    user = User(chat_id="c1", min_score=0)
    run(store.upsert_user(user))
    run(d.refresh_users())

    # The user has interacted with trojan configs repeatedly in the past.
    for _ in range(6):
        run(d.note_interest(user, "trojan"))
    run(d.refresh_users())

    run(d.ingest(_cfg("v.example.net", protocol="vless")))
    run(d.ingest(_cfg("t.example.net", protocol="trojan")))

    # The accumulated trojan affinity must lift it above the vless.
    scores = {i.protocol: i.score for i in d.pending["c1"]}
    assert scores["trojan"] > scores["vless"]


# ---------------------------------------------------------------------------
# flushing
# ---------------------------------------------------------------------------


def test_flush_delivers_a_digest(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    user = User(chat_id="c1", min_score=0)
    run(store.upsert_user(user))
    run(d.refresh_users())

    for i in range(3):
        run(d.ingest(_cfg(f"f{i}.example.net")))

    run(d.force_flush("c1"))
    assert len(sender.sent) == 1
    message = sender.sent[0]
    assert message["chat_id"] == "c1"
    assert "f0.example.net" in message["text"]
    assert "f2.example.net" in message["text"]
    assert message["keypad"] is not None            # numbered copy buttons
    assert d.counters["delivered"] == 3


def test_flush_respects_batch_cap(settings, store):
    settings.batch_max = 2
    sender = FakeSender()
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1", min_score=0)))
    run(d.refresh_users())

    for i in range(5):
        run(d.ingest(_cfg(f"cap{i}.example.net")))

    run(d.force_flush("c1"))
    assert len(sender.sent) == 1
    assert d.counters["delivered"] == 2             # only the cap
    assert len(d.pending["c1"]) == 3                # the rest still queued


def test_send_failure_is_retried_not_lost(settings, store):
    sender = FakeSender(fail=True)
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1", min_score=0)))
    run(d.refresh_users())

    run(d.ingest(_cfg("retry.example.net")))
    run(d.force_flush("c1"))
    assert d.counters["failed"] == 1
    assert d.counters["delivered"] == 0
    assert len(d.pending["c1"]) == 1                # put back for a retry


def test_best_configs_are_sent_first(settings, store):
    settings.batch_max = 2
    sender = FakeSender()
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1", min_score=0)))
    run(d.refresh_users())

    run(d.ingest(_cfg("meh.example.net", protocol="ss", security="none")))
    run(d.ingest(_cfg("best.example.net", security="reality", network="grpc")))
    run(d.ingest(_cfg("ok.example.net")))

    run(d.force_flush("c1"))
    text = sender.sent[0]["text"]
    assert "best.example.net" in text               # reality config made the cut
    assert "meh.example.net" not in text            # weak config did not


# ---------------------------------------------------------------------------
# hydration across a handoff
# ---------------------------------------------------------------------------


def test_hydrate_revives_the_queue_after_restart(settings, store):
    sender = FakeSender()
    d1 = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1", min_score=0)))
    run(d1.refresh_users())
    run(d1.ingest(_cfg("carry.example.net")))

    # Simulate the 5h40m handoff: a brand new dispatcher, same store.
    d2 = _make(settings, store, FakeSender())
    run(d2.hydrate())
    assert "c1" in d2.pending
    assert d2.pending["c1"][0].cfg is not None
    assert d2.pending["c1"][0].cfg.server == "carry.example.net"


# ---------------------------------------------------------------------------
# adaptive batching
# ---------------------------------------------------------------------------


def test_adaptive_window_widens_under_storm(settings, store):
    settings.batch_window = 45.0                    # a realistic window
    sender = FakeSender()
    d = _make(settings, store, sender)
    user = User(chat_id="c1", min_score=0)
    items = d.pending.setdefault("c1", [])
    from bot.engine.dispatcher import QueuedItem

    for i in range(4):
        items.append(QueuedItem(f"fp{i}", 70.0, "vless", time.time() - i))

    calm = d._adaptive_window(user, items[:1])
    d._arrival_rate = 2.0                            # channel storm in progress
    busy = d._adaptive_window(user, items[:1])
    assert busy > calm
    assert calm >= settings.batch_window * 0.6       # never collapses to zero


def test_should_flush_triggers_on_full_batch(settings, store):
    settings.batch_window = 45.0
    sender = FakeSender()
    d = _make(settings, store, sender)
    user = User(chat_id="c1", min_score=0, max_per_batch=3)
    from bot.engine.dispatcher import QueuedItem

    items = [QueuedItem(f"fp{i}", 70.0, "vless") for i in range(3)]
    # A full batch flushes immediately, window notwithstanding.
    assert d._should_flush(user, items, time.time()) is True
    assert d._should_flush(user, items[:2], time.time()) is False


def test_live_mode_flushes_hot_configs_instantly(settings, store):
    settings.batch_window = 45.0
    sender = FakeSender()
    d = _make(settings, store, sender)
    user = User(chat_id="c1", min_score=0, live_mode=True)
    from bot.engine.dispatcher import QueuedItem

    hot = [QueuedItem("fp-hot", 95.0, "vless")]
    cold = [QueuedItem("fp-cold", 60.0, "vless")]
    assert d._should_flush(user, hot, time.time()) is True
    assert d._should_flush(user, cold, time.time()) is False


# ---------------------------------------------------------------------------
# real-world uri
# ---------------------------------------------------------------------------


def test_real_world_config_flows_through(settings, store):
    sender = FakeSender()
    d = _make(settings, store, sender)
    run(store.upsert_user(User(chat_id="c1", min_score=0)))
    run(d.refresh_users())

    cfg = parse_uri(VLESS_REALITY)
    score = run(d.ingest(cfg, source_channel="chan"))
    assert score >= 80
    run(d.force_flush("c1"))
    assert len(sender.sent) == 1
    assert "de-fra01.example.net" in sender.sent[0]["text"]
