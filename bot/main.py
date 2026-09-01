"""Tele2Rubika entrypoint.

Wires Telegram (Telethon) -> extraction/scoring -> Rubika bot, supervises
leadership, and hands over to a fresh GitHub Actions run before the 6h job
limit kills us mid-sentence.

Usage::

    python -m bot.main                     # normal run
    python -m bot.main --check             # validate configuration and exit
    python -m bot.main --self-test         # run the parser test corpus
    python -m bot.main --max-runtime 120   # short run (local experiments)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time

from . import __version__
from .config import Settings
from .engine import Dispatcher, Scorer
from .engine.extractor import dedupe, extract_from_text
from .handlers import BotHandlers
from .logging_setup import get_logger, setup_logging
from .rubika_side import RubikaClient
from .rubika_side.poller import RubikaPoller
from .runner import (
    LeaderLock,
    build_payload,
    dispatch_handoff,
    set_github_output,
    write_heartbeat,
)
from .storage import build_store
from .telegram_side import TelegramMonitor
from .ux import messages as M

log = logging.getLogger("tele2rubika")

BANNER = r"""
  _____    _        ___   ____        _
 |_   _|__| | ___  |_ _| |  _ \ _   _| |__   __ _ _ __ ___
   | |/ _ \ |/ _ \  | |  | |_) | | | | '_ \ / _` | '_ ` _ \
   | |  __/ |  __/  | |  |  _ <| |_| | |_) | (_| | | | | | |
   |_|\___|_|\___| |___| |_| \_\\__,_|_.__/ \__,_|_| |_| |_|
"""


# ---------------------------------------------------------------------------
# self test corpus
# ---------------------------------------------------------------------------

SAMPLES = [
    "vless://b1a74f2c-9d3e-4a51-8b62-1c2d3e4f5a6b@de1.example.net:443?type=ws&security=tls&sni=google.com&path=%2Fws#%F0%9F%87%A9%F0%9F%87%AA%20Germany",
    "vmess://eyJ2IjoiMiIsInBzIjoiXHUxZjFlOVx1MWYxZWEgR2VybWFueSIsImFkZCI6Im5sMS5leGFtcGxlLm5ldCIsInBvcnQiOiI0NDMiLCJpZCI6IjExMTExMTExLTExMTEtMTExMS0xMTExLTExMTExMTExMTExMSIsImFpZCI6IjAiLCJuZXQiOiJ3cyIsInR5cGUiOiJub25lIiwiaG9zdCI6Imdvb2dsZS5jb20iLCJwYXRoIjoiL3dzIiwidGxzIjoidGxzIiwic25pIjoiZ29vZ2xlLmNvbSJ9",
    "trojan://secret-password@tr1.example.org:8443?security=tls&sni=cloudflare.com&type=ws&path=%2Ftr#Turkey",
    "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@se1.example.io:8388#Sweden",
    "ss://2022-blake3-aes-256-gcm:AnotherPass@fi1.example.net:443#Finland",
    "hy2://strongpass@hz1.example.net:443?sni=example.net&obfs=salamander#Netherlands",
    "tuic://11111111-2222-3333-4444-555555555555:tuicpass@tu1.example.net:443?sni=example.net&alpn=h3#Japan",
    "ssr://czEuZXhhbXBsZS5jb206MTIzNDphdXRoX2FlczEyOF9zaGExOmFlcy0xMjgtY2ZiOnBsYWluOmRHVnpkQT09Lz9vYmZzcGFyYW09JnByb3RvcGFyYW09JnJlbWFya3M9UlNT",
]


def self_test() -> int:
    """Parse the bundled corpus and print what was understood."""
    failures = 0
    print(f"Tele2Rubika {__version__} - extractor self test\n")
    text = "\n".join(SAMPLES)
    configs = dedupe(extract_from_text(text))
    for cfg in configs:
        ok = "OK " if cfg.is_valid else "BAD"
        if not cfg.is_valid:
            failures += 1
        print(
            f"  [{ok}] {cfg.label:<12} {cfg.server}:{cfg.port:<6} "
            f"{cfg.network or '-':<6} {cfg.security or '-':<8} "
            f"geo={cfg.geo or '-':<18} fp={cfg.fingerprint[:12]}"
        )
    print(f"\n{len(configs)}/{len(SAMPLES)} samples parsed, {failures} invalid")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def run(settings: Settings) -> int:
    started = time.time()
    deadline = started + settings.max_runtime
    stop_event = asyncio.Event()

    async def should_stop() -> bool:
        return stop_event.is_set() or time.time() >= deadline

    # --- storage ---------------------------------------------------------
    store = await build_store(settings)

    # --- rubika -----------------------------------------------------------
    rubika = RubikaClient(settings.rubika_token, settings.rubika_api_base)
    await rubika.start()
    me = await rubika.get_me()
    log.info("rubika bot ready: %s", me.get("username") or me.get("id") or "?")

    # --- engine -----------------------------------------------------------
    scorer = Scorer(store, settings)

    async def send_message(chat_id: str, text: str, keypad=None, metadata=None):
        return await rubika.send_message(
            chat_id, text, inline_keypad=keypad or None, metadata=metadata
        )

    dispatcher = Dispatcher(settings, store, scorer, send_message, M.render_batch)
    handlers = BotHandlers(settings, store, rubika, dispatcher,
                           channel_count=lambda: len(settings.telegram_channels))
    await handlers.start()

    poller = RubikaPoller(rubika, store, handlers.handle_update, settings.rubika_poll_interval)

    monitor = TelegramMonitor(settings, store, dispatcher.ingest)
    handlers.channel_count = lambda: len(monitor.channels) or len(settings.telegram_channels)

    leader = LeaderLock(store, settings.instance_id, ttl=90)

    # --- workers -----------------------------------------------------------
    async def heartbeat_loop() -> None:
        while not await should_stop():
            write_heartbeat(
                settings.heartbeat_file,
                build_payload(
                    instance=settings.instance_id,
                    started=started,
                    leader=leader.is_leader,
                    counters=dict(dispatcher.counters, **monitor.counters),
                    extra={
                        "storage": store.name,
                        "channels": len(settings.telegram_channels),
                        "version": __version__,
                    },
                ),
            )
            await asyncio.sleep(300)

    workers = [
        asyncio.create_task(leader.run(should_stop), name="leader"),
        asyncio.create_task(dispatcher.run(leader, should_stop), name="dispatcher"),
        asyncio.create_task(poller.run(leader, should_stop), name="rubika-poller"),
        asyncio.create_task(monitor.run(leader, should_stop), name="telegram-monitor"),
        asyncio.create_task(heartbeat_loop(), name="heartbeat"),
    ]

    def request_stop(*_: object) -> None:
        log.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):  # Windows
            pass

    # --- supervise ---------------------------------------------------------
    handoff_at = deadline - settings.handoff_lead
    exit_code = 0
    try:
        while not await should_stop():
            if time.time() >= handoff_at:
                break
            dead = [t.get_name() for t in workers if t.done()]
            if dead:
                log.error("worker(s) exited unexpectedly: %s", ", ".join(dead))
                exit_code = 1
                break
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        stop_event.set()

    # --- graceful handoff ---------------------------------------------------
    log.info(
        "run complete: %d ingested, %d unique, %d delivered, %d failed",
        dispatcher.counters["ingested"], dispatcher.counters["unique"],
        dispatcher.counters["delivered"], dispatcher.counters["failed"],
    )

    handed_off = False
    if settings.github_repo and settings.github_token:
        handed_off = await dispatch_handoff(settings.github_repo, settings.github_token)
        if handed_off:
            # Release early so the successor becomes leader without waiting for
            # the 90s lease to expire.
            await leader.release()
    else:
        handed_off = True  # local run: nothing to hand over to

    set_github_output("handed_off", "true" if handed_off else "false")
    stop_event.set()

    try:
        await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), timeout=90)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("workers did not stop cleanly - cancelling")
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    write_heartbeat(
        settings.heartbeat_file,
        build_payload(
            instance=settings.instance_id,
            started=started,
            leader=False,
            counters=dict(dispatcher.counters, **monitor.counters),
            extra={"storage": store.name, "handed_off": handed_off, "version": __version__},
        ),
    )

    await monitor.disconnect()
    await rubika.close()
    await store.close()
    log.info("goodbye (uptime %.0fs)", time.time() - started)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tele2rubika", description="Telegram -> Rubika config bridge")
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--self-test", action="store_true", help="run the parser corpus and exit")
    parser.add_argument("--max-runtime", type=int, default=None, help="override MAX_RUNTIME (seconds)")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        from dotenv import load_dotenv  # optional, dev only

        load_dotenv()
    except ImportError:
        pass

    settings = Settings.from_env()
    if args.max_runtime is not None:
        settings.max_runtime = max(120, args.max_runtime)
        settings.validate()
    if args.log_level:
        settings.log_level = args.log_level.upper()

    setup_logging(settings.log_level)
    print(BANNER)
    log.info("Tele2Rubika %s starting (instance %s)", __version__, settings.instance_id)

    for problem in settings.problems():
        log.warning("config: %s", problem)

    if args.check:
        fatal = not settings.rubika_enabled
        log.info("--check complete: %s", "configuration is usable" if not fatal else "RUBIKA_BOT_TOKEN is required")
        return 1 if fatal else 0

    if not settings.rubika_enabled:
        log.error("RUBIKA_BOT_TOKEN is required to run the bot.")
        return 1

    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
