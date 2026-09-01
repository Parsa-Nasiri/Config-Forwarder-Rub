"""Runner tests: leader election, heartbeat and the GitHub handoff."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from bot.runner.github_handoff import dispatch_handoff, set_github_output
from bot.runner.heartbeat import build_payload, write_heartbeat
from bot.runner.leader import LeaderLock

from .conftest import run


# ---------------------------------------------------------------------------
# leader lock
# ---------------------------------------------------------------------------


def test_acquires_when_free(store):
    lock = LeaderLock(store, "runner-1", ttl=90)
    assert run(lock.try_acquire()) is True
    assert lock.is_leader is True
    assert lock.uptime >= 0


def test_second_runner_stays_standby(store):
    first = LeaderLock(store, "runner-1", ttl=90)
    second = LeaderLock(store, "runner-2", ttl=90)
    assert run(first.try_acquire()) is True
    assert run(second.try_acquire()) is False
    assert second.is_leader is False


def test_lock_is_callable_as_the_is_leader_predicate(store):
    lock = LeaderLock(store, "runner-1", ttl=90)
    assert run(lock()) is False              # not acquired yet
    run(lock.try_acquire())
    assert run(lock()) is True               # usable directly by workers


def test_expired_lease_is_stolen(store):
    old = LeaderLock(store, "runner-1", ttl=-1)
    assert run(old.try_acquire()) is True
    new = LeaderLock(store, "runner-2", ttl=90)
    assert run(new.try_acquire()) is True    # the dead lease got stolen
    assert new.is_leader is True


def test_release_lets_standby_take_over_immediately(store):
    first = LeaderLock(store, "runner-1", ttl=90)
    second = LeaderLock(store, "runner-2", ttl=90)
    run(first.try_acquire())
    run(first.release())
    assert first.is_leader is False
    assert run(second.try_acquire()) is True


def test_released_lock_cannot_reacquire(store):
    first = LeaderLock(store, "runner-1", ttl=90)
    run(first.try_acquire())
    run(first.release())
    # A released lock must stay down even if try_acquire is called again.
    assert run(first.try_acquire()) is False


def test_renewal_loop_stops_on_should_stop(store):
    lock = LeaderLock(store, "runner-1", ttl=90)
    run(lock.try_acquire())

    stop = {"flag": False}

    async def should_stop() -> bool:
        return stop["flag"]

    async def scenario() -> None:
        task = asyncio.create_task(lock.run(should_stop))
        await asyncio.sleep(0.05)
        stop["flag"] = True
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    # Exiting the loop while leader releases the lease voluntarily.
    assert run(store.read_lock("tele2rubika")) is None


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_payload_shape(tmp_path):
    payload = build_payload(
        instance="run-1",
        started=time.time() - 600,
        leader=True,
        counters={"delivered": 12, "ingested": 40},
        extra={"storage": "supabase"},
    )
    assert payload["instance"] == "run-1"
    assert payload["leader"] is True
    assert payload["uptime_seconds"] >= 599
    assert payload["counters"]["delivered"] == 12
    assert payload["storage"] == "supabase"
    assert payload["updated_at"]


def test_heartbeat_write_is_atomic_and_readable(tmp_path):
    path = str(tmp_path / "status" / "last-run.json")
    payload = build_payload(instance="r", started=time.time(), leader=True, counters={})
    assert write_heartbeat(path, payload) is True
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["instance"] == "r"
    # A second write must replace, not corrupt.
    payload["instance"] = "r2"
    write_heartbeat(path, payload)
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["instance"] == "r2"


def test_heartbeat_survives_a_read_race(tmp_path):
    """Atomic replace means readers never see a half-written file."""
    path = str(tmp_path / "last-run.json")
    for i in range(25):
        payload = build_payload(instance=f"run-{i}", started=time.time(),
                                leader=True, counters={"i": i})
        write_heartbeat(path, payload)
        data = json.loads(open(path, encoding="utf-8").read())
        assert data["counters"]["i"] == i


# ---------------------------------------------------------------------------
# github handoff
# ---------------------------------------------------------------------------


def test_dispatch_handoff_posts_repository_dispatch(monkeypatch):
    seen: dict = {}

    class FakeResponse:
        status_code = 204
        text = ""

    async def fake_post(self, url, json=None, headers=None):  # noqa: A002
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    ok = asyncio.run(dispatch_handoff("Parsa-Nasiri/Tele2Rubika", "TOKEN123"))
    assert ok is True
    assert seen["url"] == "https://api.github.com/repos/Parsa-Nasiri/Tele2Rubika/dispatches"
    assert seen["json"] == {"event_type": "bot-handoff"}
    assert "TOKEN123" in seen["headers"]["Authorization"]


def test_dispatch_handoff_without_repo_is_a_noop():
    assert asyncio.run(dispatch_handoff("", "token")) is False
    assert asyncio.run(dispatch_handoff("a/b", "")) is False


def test_set_github_output(monkeypatch, tmp_path):
    out = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    set_github_output("handed_off", "true")
    assert "handed_off=true" in out.read_text()


def test_set_github_output_without_env_does_not_raise(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    set_github_output("handed_off", "true")


# ---------------------------------------------------------------------------
# watchdog probe
# ---------------------------------------------------------------------------


def test_watchdog_heartbeat_age_helper():
    from scripts.watchdog import _heartbeat_age_minutes

    # No heartbeat file -> unknown age, the caller must treat it as down.
    import scripts.watchdog as wd

    original = wd.HEARTBEAT
    try:
        wd.HEARTBEAT = original.parent / "does-not-exist.json"
        assert _heartbeat_age_minutes() is None
    finally:
        wd.HEARTBEAT = original
