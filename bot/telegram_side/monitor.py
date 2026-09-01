"""Telegram side: watch channels with Telethon and harvest configs.

The Telethon session is a ``StringSession`` kept in Supabase, so a brand new
GitHub Actions runner can resume the exact same authorised session every
5h40m without ever touching a phone number again.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

from ..config import Settings
from ..engine.extractor import (
    dedupe,
    extract_from_text,
    parse_subscription_body,
    subscription_urls,
)
from ..logging_setup import get_logger
from ..storage.base import Store

log: logging.Logger = get_logger("telegram.monitor")

SESSION_KEY = "telegram_session"
LAST_KEY = "telegram_last"

MAX_SUB_BYTES = 3 * 1024 * 1024
MAX_CONFIGS_PER_MESSAGE = 120
MAX_CONFIGS_PER_SUBSCRIPTION = 250


class TelegramMonitor:
    def __init__(self, settings: Settings, store: Store, on_config) -> None:
        self.settings = settings
        self.store = store
        self.on_config = on_config

        self.client: TelegramClient | None = None
        self.channels: list[str] = []
        self._entities: list = []
        self._connected = False
        self._last_persist = 0.0
        self._http: httpx.AsyncClient | None = None
        self.counters = {"messages": 0, "configs": 0, "subscriptions": 0}

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.settings.telegram_enabled

    def _build_channels(self, extra: list[str]) -> list[str]:
        merged: list[str] = []
        for item in list(self.settings.telegram_channels) + list(extra):
            if item not in merged:
                merged.append(item)
        return merged

    # ------------------------------------------------------------------
    async def _persist_session(self, force: bool = False) -> None:
        if self.client is None or not self._connected:
            return
        now = time.time()
        if not force and now - self._last_persist < 600:
            return
        try:
            session_string = self.client.session.save()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not serialise session: %s", exc)
            return
        if session_string:
            await self.store.set_state(SESSION_KEY, session_string)
            self._last_persist = now

    async def connect(self) -> bool:
        session_string = self.settings.telegram_session or await self.store.get_state(SESSION_KEY)
        if not session_string:
            log.error(
                "no Telegram session available. Generate one locally with "
                "`python scripts/auth_telegram.py` and set TELEGRAM_SESSION_STRING."
            )
            return False

        extra = await self.store.list_sources()
        self.channels = self._build_channels(extra)
        if not self.channels:
            log.error("TELEGRAM_CHANNELS is empty - nothing to monitor.")
            return False

        self.client = TelegramClient(
            StringSession(session_string),
            self.settings.telegram_api_id,
            self.settings.telegram_api_hash,
            connection_retries=5,
            retry_delay=3,
            request_retries=5,
        )
        try:
            await self.client.connect()
        except (OSError, RPCError) as exc:
            log.error("telegram connect failed: %s", exc)
            return False

        if not await self.client.is_user_authorized():
            log.error(
                "Telegram session is not authorised. Re-run "
                "`python scripts/auth_telegram.py` and update TELEGRAM_SESSION_STRING."
            )
            await self.client.disconnect()
            return False

        me = await self.client.get_me()
        log.info("telegram connected as %s", getattr(me, "username", None) or getattr(me, "first_name", "?"))

        entities = []
        for channel in self.channels:
            try:
                entities.append(await self.client.get_entity(channel))
            except (ValueError, RPCError) as exc:
                log.warning("cannot resolve channel %r: %s", channel, exc)
        if not entities:
            log.error("none of the configured channels could be resolved.")
            await self.client.disconnect()
            return False

        self._entities = entities
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=entities),
        )
        self._connected = True
        self._http = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Tele2Rubika/1.0"},
        )
        await self._persist_session(force=True)
        log.info("monitoring %d channel(s): %s", len(entities), ", ".join(self.channels))
        return True

    async def disconnect(self) -> None:
        if self.client is not None:
            await self._persist_session(force=True)
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._connected = False
        self.client = None

    # ------------------------------------------------------------------
    async def _harvest(self, text: str, channel: str, message_id: str) -> int:
        configs = dedupe(extract_from_text(text))[:MAX_CONFIGS_PER_MESSAGE]
        for cfg in configs:
            await self.on_config(cfg, source_channel=channel, source_message=str(message_id))
        self.counters["configs"] += len(configs)
        return len(configs)

    async def _harvest_subscription(self, url: str, channel: str, message_id: str) -> int:
        if self._http is None:
            return 0
        try:
            resp = await self._http.get(url)
            if resp.status_code >= 400:
                return 0
            body = resp.text
        except Exception as exc:  # noqa: BLE001
            log.debug("subscription fetch failed %s: %s", url, exc)
            return 0
        if len(body) > MAX_SUB_BYTES:
            body = body[:MAX_SUB_BYTES]
        configs = dedupe(parse_subscription_body(body))[:MAX_CONFIGS_PER_SUBSCRIPTION]
        for cfg in configs:
            await self.on_config(cfg, source_channel=channel, source_message=str(message_id))
        self.counters["subscriptions"] += 1
        self.counters["configs"] += len(configs)
        log.info("subscription %s yielded %d config(s)", url[:60], len(configs))
        return len(configs)

    async def _process(self, message, channel: str) -> None:
        text = getattr(message, "message", "") or getattr(message, "text", "") or ""
        message_id = str(getattr(message, "id", "") or "")
        self.counters["messages"] += 1

        found = 0
        if text:
            found += await self._harvest(text, channel, message_id)
            for url in subscription_urls(text)[:3]:
                found += await self._harvest_subscription(url, channel, message_id)

        if self.settings.telegram_parse_documents and getattr(message, "document", None):
            content = await self._document_text(message)
            if content:
                found += await self._harvest(content, channel, message_id)

        if found:
            log.debug("message %s@%s -> %d config(s)", message_id, channel, found)

    async def _document_text(self, message) -> str:
        doc = getattr(message, "document", None)
        if doc is None:
            return ""
        size = int(getattr(doc, "size", 0) or 0)
        if size > MAX_SUB_BYTES:
            return ""
        try:
            raw = await message.download_media(bytes)
        except Exception as exc:  # noqa: BLE001
            log.debug("document download failed: %s", exc)
            return ""
        if not raw:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="ignore")

    # ------------------------------------------------------------------
    async def _on_new_message(self, event) -> None:
        try:
            channel = self._label_for(event)
            await self._process(event.message, channel)
        except FloodWaitError as exc:
            log.warning("telegram flood wait %ss", exc.seconds)
            await asyncio.sleep(min(exc.seconds, 300))
        except Exception:  # noqa: BLE001
            log.exception("error processing live message")

    def _label_for(self, event) -> str:
        chat = getattr(event, "chat", None)
        if chat is None:
            return ""
        return getattr(chat, "username", None) or getattr(chat, "title", None) or str(
            getattr(event, "chat_id", "")
        )

    async def catch_up(self) -> None:
        """Scan recent history so a fresh runner is useful immediately."""
        if self.client is None or not self.channels:
            return
        state: dict[str, int] = await self.store.get_state(LAST_KEY, {}) or {}
        limit = max(0, self.settings.telegram_catchup_limit)
        if limit == 0:
            return

        for channel, entity in zip(self.channels, self._entities):
            last_id = int(state.get(channel, 0) or 0)
            newest = last_id
            scanned = 0
            try:
                kwargs: dict = {"limit": limit}
                if last_id:
                    kwargs["min_id"] = last_id
                messages = []
                async for message in self.client.iter_messages(entity, **kwargs):
                    messages.append(message)
                for message in reversed(messages):  # oldest -> newest
                    await self._process(message, channel)
                    newest = max(newest, int(message.id))
                    scanned += 1
                    if scanned % 25 == 0:
                        await asyncio.sleep(0.5)
            except FloodWaitError as exc:
                wait = min(exc.seconds, 180)
                log.warning("catch-up flood wait on %s (%ss)", channel, wait)
                await asyncio.sleep(wait)
            except RPCError as exc:
                log.warning("catch-up failed for %s: %s", channel, exc)
            state[channel] = newest
            if scanned:
                log.info("catch-up %s: %d message(s), last id %d", channel, scanned, newest)
        await self.store.set_state(LAST_KEY, state)

    # ------------------------------------------------------------------
    async def run(self, is_leader, should_stop) -> None:
        if not self.enabled:
            log.warning("telegram monitoring disabled (missing credentials or session)")
            return

        backoff = 15.0
        while not await should_stop():
            if not await is_leader():
                if self._connected:
                    log.info("lost leadership - releasing telegram session")
                    await self.disconnect()
                await asyncio.sleep(5.0)
                continue

            if not self._connected:
                try:
                    ok = await self.connect()
                except Exception:  # noqa: BLE001
                    log.exception("telegram connect crashed")
                    ok = False
                if not ok:
                    await asyncio.sleep(backoff)
                    backoff = min(300.0, backoff * 2)
                    continue
                backoff = 15.0
                try:
                    await self.catch_up()
                except Exception:  # noqa: BLE001
                    log.exception("catch-up crashed")

            await asyncio.sleep(30.0)
            await self._persist_session()

        await self.disconnect()
        log.info(
            "telegram monitor stopped (%d messages, %d configs, %d subscriptions)",
            self.counters["messages"], self.counters["configs"], self.counters["subscriptions"],
        )


__all__ = ["TelegramMonitor", "SESSION_KEY", "LAST_KEY"]
