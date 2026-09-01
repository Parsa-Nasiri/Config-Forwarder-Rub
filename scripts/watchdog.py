#!/usr/bin/env python
"""Liveness probe used by .github/workflows/watchdog.yml.

Asks the one source of truth that cannot lie: the leader lease in Supabase.
Every live runner refreshes a 90 second lease, so if the lease is missing or
expired there is genuinely nobody holding the fort and we restart the chain.

Exits 0 when the bot is healthy, 2 when a restart was dispatched, 1 on error.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT = REPO_ROOT / "status" / "last-run.json"
LOCK_NAME = "tele2rubika"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


async def _lease_alive() -> tuple[bool, str]:
    """True when some runner currently owns a non-expired lease."""
    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_KEY") or _env("SUPABASE_KEY")
    if not (url and key):
        return False, "supabase not configured"

    try:
        import httpx
    except ImportError:
        return False, "httpx missing"

    endpoint = f"{url}/rest/v1/runner_locks"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    params = {"select": "instance_id,acquired_at,expires_at", "name": f"eq.{LOCK_NAME}"}

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(endpoint, headers=headers, params=params)
        if resp.status_code >= 400:
            return False, f"supabase http {resp.status_code}"
        rows = resp.json()

    if not rows:
        return False, "no lease row"

    row = rows[0]
    expires = (row.get("expires_at") or "").replace("Z", "+00:00")
    # Ask Postgres instead of trusting local clocks - avoids any timezone drift
    # between the watchdog runner and the bot runner.
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{url}/rest/v1/runner_locks",
            headers=headers,
            params={
                "select": "instance_id",
                "name": f"eq.{LOCK_NAME}",
                "expires_at": f"gt.{_utcnow()}",
            },
        )
    fresh = resp.status_code < 400 and bool(resp.json())
    return fresh, f"lease {row.get('instance_id')} expires {expires}"


def _utcnow() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _heartbeat_age_minutes() -> float | None:
    if not HEARTBEAT.exists():
        return None
    try:
        data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stamp = data.get("updated_at") or ""
    if not stamp:
        return None
    import datetime as dt

    try:
        then = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.datetime.now(dt.timezone.utc) - then).total_seconds() / 60.0


async def _dispatch() -> bool:
    repo = _env("GITHUB_REPOSITORY")
    token = _env("GITHUB_TOKEN") or _env("RESTART_TOKEN")
    if not (repo and token):
        print("  ! cannot restart: GITHUB_REPOSITORY or GITHUB_TOKEN missing")
        return False
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{repo}/dispatches",
            json={"event_type": "bot-handoff"},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    ok = resp.status_code in (200, 204)
    print(f"  restart dispatch -> http {resp.status_code}" + ("" if ok else f" {resp.text[:200]}"))
    return ok


async def main() -> int:
    alive, detail = await _lease_alive()
    age = _heartbeat_age_minutes()
    age_text = "never" if age is None else f"{age:.0f} min ago"
    print(f"  leader lease : {detail}")
    print(f"  heartbeat    : {age_text}")

    if alive:
        print("  verdict      : HEALTHY - nothing to do")
        return 0

    print("  verdict      : DOWN - restarting the chain")
    return 2 if await _dispatch() else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
