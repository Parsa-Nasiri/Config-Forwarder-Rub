"""Optional TCP reachability probe.

Disabled by default: GitHub Actions blocks most outbound ports, so a probe
there would mark everything unreachable. Enable it with
``HEALTH_CHECK_ENABLED=true`` when you self-host the bot.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .models import ProxyConfig

log = logging.getLogger("engine.health")


async def probe(cfg: ProxyConfig, timeout: float = 6.0) -> tuple[bool | None, int | None]:
    """Open a TCP connection to the config endpoint.

    Returns ``(reachable, latency_ms)``. ``reachable`` is None when the probe
    itself could not run (DNS failure, blocked egress, ...) so we do not
    punish a config for our own network problems.
    """
    if not cfg.server or not cfg.port:
        return None, None
    started = time.monotonic()
    conn = asyncio.open_connection(cfg.server, cfg.port)
    try:
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return False, int((time.monotonic() - started) * 1000)
    except OSError as exc:
        # ENETUNREACH / EHOSTUNREACH & friends mean *we* cannot route, not that
        # the server is down.
        log.debug("probe %s:%s -> %s", cfg.server, cfg.port, exc)
        return None, None
    except Exception:  # noqa: BLE001
        return None, None
    else:
        latency = int((time.monotonic() - started) * 1000)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True, latency


__all__ = ["probe"]
