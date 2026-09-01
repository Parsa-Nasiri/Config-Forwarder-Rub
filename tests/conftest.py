"""Shared test fixtures.

Everything runs against the in-memory store, so the suite needs no Supabase
project, no Telegram account and no network access.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Keep Settings.from_env() deterministic no matter what the dev machine has set.
for _name in list(os.environ):
    if _name.startswith(("TELEGRAM_", "RUBIKA_", "SUPABASE_")):
        os.environ.pop(_name, None)


def run(coro):
    """Run a coroutine from a synchronous test (avoids a pytest-asyncio dep)."""
    return asyncio.run(coro)


# A realistic corpus used by several test modules.
VLESS_REALITY = (
    "vless://7a2b3c4d-1111-2222-3333-444455556666@de-fra01.example.net:443"
    "?type=grpc&security=reality&encryption=none&pbk=xyz&fp=chrome"
    "&sni=www.microsoft.com&sid=6&spx=%2F&flow=xtls-rprx-vision"
    "&serviceName=grpc#%F0%9F%87%A9%F0%9F%87%AA%20FRA-01%20%7C%20Reality"
)

VMESS_WS = (
    "vmess://eyJ2IjoiMiIsInBzIjoiXHUxZjFlZVx1MWYxZjcgTkxBTSIsImFkZCI6Im5sLWFtcy5leGFtcGxlLm5ldCIs"
    "InBvcnQiOiI0NDMiLCJpZCI6ImFiY2RlZjAxLTAwMDEtMjIyMi0zMzMzLTQ0NDQ1NTU1NjY2NiIsImFpZCI6IjAiLCJu"
    "ZXQiOiJ3cyIsInR5cGUiOiJub25lIiwiaG9zdCI6ImNsb3VkLmV4YW1wbGUuY29tIiwicGF0aCI6Ii93cyIsInRscyI6"
    "InRscyIsInNuaSI6ImNsb3VkLmV4YW1wbGUuY29tIn0="
)

TROJAN_WS = (
    "trojan://p4ssw0rd@tr-ist01.example.org:8443"
    "?security=tls&sni=cdn.example.org&type=ws&path=%2Ftrojan#Turkey-01"
)

SS_2022 = "ss://2022-blake3-aes-256-gcm:Str0ngPass@fi-hel01.example.net:443#Finland"

HY2 = "hy2://hy2pass@nl-ams01.example.io:443?sni=example.io&obfs=salamander#NL-AMS"

TUIC = (
    "tuic://11111111-2222-3333-4444-555555555555:tuicpass@jp-tyo01.example.net:443"
    "?sni=example.net&alpn=h3&congestion_control=bbr#Japan"
)

SSR = (
    "ssr://czEwMS5leGFtcGxlLmNvbToxMjM0OmF1dGhfYWVzMTI4X3NoYTE6YWVzLTEyOC1jZmI6cGxhaW46ZEdWemRRPT0v"
    "P29iZnNwYXJhbT0mcHJvdG9wYXJhbT0mcmVtYXJrcz1TU1I="
)

CORPUS = [VLESS_REALITY, VMESS_WS, TROJAN_WS, SS_2022, HY2, TUIC, SSR]


@pytest.fixture
def corpus() -> list[str]:
    return list(CORPUS)


@pytest.fixture
def settings():
    from bot.config import Settings

    s = Settings(
        min_score=0,
        instant_score=88,
        batch_window=0.0,      # flush on the first tick
        batch_max=5,
        user_hourly_cap=0,
        rate_limit_per_chat=1000,
        rate_limit_global=100000,
        health_check_enabled=False,
        default_language="en",
    )
    return s


@pytest.fixture
def store():
    from bot.storage.memory_store import MemoryStore

    return MemoryStore()


@pytest.fixture
def scorer(store, settings):
    from bot.engine.scorer import Scorer

    return Scorer(store, settings)
