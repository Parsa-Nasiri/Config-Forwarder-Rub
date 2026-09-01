"""Trigger the next GitHub Actions run before this one hits the 6h limit."""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("runner.handoff")

API = "https://api.github.com"


async def dispatch_handoff(repo: str, token: str, event_type: str = "bot-handoff") -> bool:
    """Fire a ``repository_dispatch`` that starts the next runner.

    The default ``GITHUB_TOKEN`` is allowed to trigger ``repository_dispatch``
    and ``workflow_dispatch`` even though other GITHUB_TOKEN-triggered events
    do not start new runs.
    """
    if not repo or not token:
        log.warning("cannot hand off: GITHUB_REPOSITORY or GITHUB_TOKEN is missing")
        return False

    url = f"{API}/repos/{repo}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json={"event_type": event_type}, headers=headers)
    except Exception as exc:  # noqa: BLE001
        log.error("handoff dispatch raised: %s", exc)
        return False

    if resp.status_code in (200, 204):
        log.info("handoff dispatched (%s) - the next run is queued", event_type)
        return True
    log.error("handoff dispatch failed [%d]: %s", resp.status_code, resp.text[:300])
    return False


def set_github_output(key: str, value: str) -> None:
    """Write a step output so the workflow can skip its fallback handoff."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")
    except OSError as exc:
        log.debug("could not write GITHUB_OUTPUT: %s", exc)


__all__ = ["dispatch_handoff", "set_github_output"]
