"""Uptime heartbeat file.

Written on every run (and refreshed periodically) so the repo has a visible
liveness trail. The workflow commits it, which doubles as the repository
activity that keeps GitHub's scheduled workflows enabled on public repos.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("runner.heartbeat")


def write_heartbeat(path: str, payload: dict) -> bool:
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("could not write heartbeat to %s: %s", path, exc)
        return False


def build_payload(*, instance: str, started: float, leader: bool,
                  counters: dict, extra: dict | None = None) -> dict:
    payload = {
        "instance": instance,
        "started_at": _iso(started),
        "updated_at": _iso(time.time()),
        "uptime_seconds": int(time.time() - started),
        "leader": leader,
        "counters": dict(counters),
    }
    if extra:
        payload.update(extra)
    return payload


def _iso(ts: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["write_heartbeat", "build_payload"]
