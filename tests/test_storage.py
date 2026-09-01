"""Storage contract tests, run against the in-memory implementation.

These double as the spec for :class:`SupabaseStore`: every behaviour asserted
here is one the production store must reproduce.
"""

from __future__ import annotations

import time

from bot.storage.base import User
from bot.storage.memory_store import MemoryStore

from .conftest import run


async def _add(store: MemoryStore, fp: str, server: str = "s.example.net", port: int = 443,
              protocol: str = "vless", channel: str = "chan") -> bool:
    return await store.add_config(
        fingerprint=fp, protocol=protocol, server=server, port=port,
        remark="node", raw=f"raw-{fp}", score=70.0, geo="",
        network="ws", security="tls", source_channel=channel, source_message="1",
    )


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def test_user_round_trip(store):
    user = User(chat_id="c1", first_name="Parsa", language="fa", min_score=70)
    run(store.upsert_user(user))
    loaded = run(store.get_user("c1"))
    assert loaded is not None
    assert loaded.chat_id == "c1"
    assert loaded.first_name == "Parsa"
    assert loaded.language == "fa"
    assert loaded.min_score == 70


def test_update_user_touches_only_given_fields(store):
    run(store.upsert_user(User(chat_id="c1", first_name="A", min_score=50)))
    run(store.update_user("c1", min_score=80, live_mode=True))
    user = run(store.get_user("c1"))
    assert user.first_name == "A"       # untouched
    assert user.min_score == 80
    assert user.live_mode is True


def test_list_users_deliverable_filters(store):
    run(store.upsert_user(User(chat_id="ok")))
    run(store.upsert_user(User(chat_id="paused", is_paused=True)))
    run(store.upsert_user(User(chat_id="inactive", is_active=False)))
    deliverable = {u.chat_id for u in run(store.list_users(only_deliverable=True))}
    assert deliverable == {"ok"}


def test_timed_pause_expires(store):
    user = User(chat_id="c1", is_paused=True, paused_until=time.time() - 10)
    assert user.paused is False          # the deadline already passed
    user2 = User(chat_id="c2", is_paused=True, paused_until=time.time() + 3600)
    assert user2.paused is True
    user3 = User(chat_id="c3")           # never paused
    assert user3.paused is False


def test_user_accepts_respects_all_filters():
    user = User(chat_id="c1", min_score=60, protocols=["vless"])
    assert user.accepts("vless", 80)
    assert not user.accepts("trojan", 80)     # protocol filter
    assert not user.accepts("vless", 40)      # score filter
    paused = User(chat_id="c2", is_paused=True)
    assert not paused.accepts("vless", 90)
    inactive = User(chat_id="c3", is_active=False)
    assert not inactive.accepts("vless", 90)


def test_user_row_round_trip():
    user = User(chat_id="c1", protocols=["vless", "trojan"], affinity={"vless": 1.5})
    clone = User.from_row(user.to_row())
    assert clone.chat_id == "c1"
    assert clone.protocols == ["vless", "trojan"]
    assert clone.affinity == {"vless": 1.5}


# ---------------------------------------------------------------------------
# configs + dedupe
# ---------------------------------------------------------------------------


def test_add_config_new_then_duplicate(store):
    assert run(_add(store, "fp1")) is True
    assert run(_add(store, "fp1")) is False          # same fingerprint -> dedupe
    stats = run(store.stats())
    assert stats["configs"] == 1


def test_repost_from_new_channel_counts_as_corroboration(store):
    run(_add(store, "fp1", channel="chan-a"))
    run(_add(store, "fp1", channel="chan-b"))
    row = run(store.get_config("fp1"))
    assert row["source_count"] == 2
    assert row["seen_count"] == 2


def test_top_configs_orders_by_score(store):
    run(_add(store, "low"))
    run(store.patch_config("low", score=30))
    run(_add(store, "high"))
    run(store.patch_config("high", score=90))
    rows = run(store.top_configs(2))
    assert [r["fingerprint"] for r in rows] == ["high", "low"]


def test_report_feedback_updates_the_row(store):
    run(_add(store, "fp1"))
    run(store.report("fp1", "dead"))
    run(store.report("fp1", "dead"))
    run(store.report("fp1", "live"))
    row = run(store.get_config("fp1"))
    assert row["dead_reports"] == 2
    assert row["live_reports"] == 1


# ---------------------------------------------------------------------------
# deliveries
# ---------------------------------------------------------------------------


def test_enqueue_is_idempotent_per_chat(store):
    assert run(store.enqueue("c1", "fp1", 80)) is True
    assert run(store.enqueue("c1", "fp1", 80)) is False   # already queued for c1
    assert run(store.enqueue("c2", "fp1", 80)) is True    # other chats still get it


def test_claim_batch_moves_rows_to_sending(store):
    run(_add(store, "fp1"))
    run(_add(store, "fp2"))
    run(store.enqueue("c1", "fp1", 80))
    run(store.enqueue("c1", "fp2", 70))
    claimed = run(store.claim_batch("c1", 5))
    assert len(claimed) == 2
    assert all(row["status"] == "sending" for row in claimed)
    assert len({row["batch_id"] for row in claimed}) == 1


def test_claim_batch_respects_the_limit(store):
    for i in range(5):
        fp = f"fp{i}"
        run(_add(store, fp))
        run(store.enqueue("c1", fp, 80))
    claimed = run(store.claim_batch("c1", 2))
    assert len(claimed) == 2


def test_mark_sent_then_nothing_left_to_claim(store):
    run(_add(store, "fp1"))
    run(store.enqueue("c1", "fp1", 80))
    claimed = run(store.claim_batch("c1", 5))
    batch_id = claimed[0]["batch_id"]
    run(store.mark_sent(batch_id, [r["fingerprint"] for r in claimed], "m1"))
    assert run(store.claim_batch("c1", 5)) == []
    assert run(store.recent_delivery_count("c1", time.time() - 60)) == 1


def test_mark_failed_requeues_for_retry(store):
    run(_add(store, "fp1"))
    run(store.enqueue("c1", "fp1", 80))
    claimed = run(store.claim_batch("c1", 5))
    batch_id = claimed[0]["batch_id"]
    run(store.mark_failed(batch_id, ["fp1"], "boom", retryable=True))
    # Back in the queue with a backoff delay before it is due again.
    row = next(d for d in store.deliveries if d["fingerprint"] == "fp1")
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["next_attempt"] > time.time()


def test_mark_failed_gives_up_after_max_attempts(store):
    run(_add(store, "fp1"))
    run(store.enqueue("c1", "fp1", 80))
    claimed = run(store.claim_batch("c1", 5))
    batch_id = claimed[0]["batch_id"]
    for _ in range(5):
        run(store.mark_failed(batch_id, ["fp1"], "boom", retryable=True))
    row = next(d for d in store.deliveries if d["fingerprint"] == "fp1")
    assert row["status"] == "failed"


def test_delivered_count_only_counts_the_batch(store):
    run(_add(store, "fp1"))
    run(_add(store, "fp2"))
    run(store.enqueue("c1", "fp1", 80))
    run(store.enqueue("c1", "fp2", 80))
    claimed = run(store.claim_batch("c1", 5))
    batch_id = claimed[0]["batch_id"]
    run(store.mark_sent(batch_id, ["fp1"], "m1"))
    assert run(store.get_config("fp1"))["delivered_count"] == 1
    assert run(store.get_config("fp2"))["delivered_count"] == 0


def test_list_queued_survives_a_handoff(store):
    run(_add(store, "fp1"))
    run(_add(store, "fp2"))
    run(store.enqueue("c1", "fp1", 80))
    run(store.enqueue("c1", "fp2", 70))
    rows = run(store.list_queued("c1"))
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# channel reputation
# ---------------------------------------------------------------------------


def test_channel_reputation_penalises_dead_feeds(store):
    run(store.bump_channel("good", "total", 100))
    run(store.bump_channel("good", "dead", 1))
    run(store.bump_channel("bad", "total", 100))
    run(store.bump_channel("bad", "dead", 90))
    good = run(store.channel_reputation("good"))
    bad = run(store.channel_reputation("bad"))
    assert good > 0.3
    assert bad < -0.4
    assert run(store.channel_reputation("unknown")) == 0.0


# ---------------------------------------------------------------------------
# leader lock
# ---------------------------------------------------------------------------


def test_lock_acquire_renew_and_block(store):
    assert run(store.acquire_lock("tele2rubika", "runner-1", 90)) is True
    assert run(store.acquire_lock("tele2rubika", "runner-2", 90)) is False  # held
    assert run(store.acquire_lock("tele2rubika", "runner-1", 90)) is True   # renew


def test_lock_expires_and_can_be_stolen(store):
    assert run(store.acquire_lock("tele2rubika", "runner-1", ttl=-1)) is True
    assert run(store.acquire_lock("tele2rubika", "runner-2", 90)) is True   # lease expired
    assert run(store.read_lock("tele2rubika"))["instance_id"] == "runner-2"


def test_lock_release_only_by_owner(store):
    run(store.acquire_lock("tele2rubika", "runner-1", 90))
    run(store.release_lock("tele2rubika", "runner-2"))     # not the owner
    assert run(store.read_lock("tele2rubika"))["instance_id"] == "runner-1"
    run(store.release_lock("tele2rubika", "runner-1"))     # the owner
    assert run(store.read_lock("tele2rubika")) is None


# ---------------------------------------------------------------------------
# state bag
# ---------------------------------------------------------------------------


def test_state_round_trip(store):
    run(store.set_state("telegram_session", "SESSION123"))
    assert run(store.get_state("telegram_session")) == "SESSION123"
    assert run(store.get_state("missing", "fallback")) == "fallback"
    run(store.set_state("last", {"chan": 42}))
    assert run(store.get_state("last")) == {"chan": 42}
