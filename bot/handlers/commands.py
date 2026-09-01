"""Rubika update router: commands, text and inline keypad callbacks.

One instance serves every user. Users are keyed by Rubika ``chat_id``, so
state never leaks between them.
"""

from __future__ import annotations

import logging
import re
import time

from ..config import Settings
from ..engine.dispatcher import Dispatcher
from ..engine.models import ProxyConfig
from ..logging_setup import get_logger
from ..rubika_side.client import RubikaClient, RubikaError
from ..rubika_side import keypads as K
from ..storage.base import Store, User
from ..ux import messages as M
from ..ux.i18n import t

log: logging.Logger = get_logger("handlers")

_PAUSE_RE = re.compile(r"^\s*/pause(?:\s+(\d+)\s*([hm]))?\s*$", re.IGNORECASE)

BOT_COMMANDS = [
    {"command": "start", "description": "Subscribe to the config feed"},
    {"command": "latest", "description": "Best configs right now"},
    {"command": "filters", "description": "Protocols and quality threshold"},
    {"command": "live", "description": "Instant delivery for top configs"},
    {"command": "pause", "description": "Snooze the feed (e.g. /pause 2h)"},
    {"command": "resume", "description": "Resume the feed"},
    {"command": "stats", "description": "Your numbers and network numbers"},
    {"command": "lang", "description": "Switch language"},
    {"command": "help", "description": "How everything works"},
]


class BotHandlers:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        rubika: RubikaClient,
        dispatcher: Dispatcher,
        channel_count,
    ) -> None:
        self.settings = settings
        self.store = store
        self.rubika = rubika
        self.dispatcher = dispatcher
        self.channel_count = channel_count
        self._seen: dict[str, float] = {}

    # ------------------------------------------------------------------
    async def start(self) -> None:
        try:
            await self.rubika.set_commands(BOT_COMMANDS)
            log.info("registered %d bot commands", len(BOT_COMMANDS))
        except RubikaError as exc:
            log.warning("could not register commands: %s", exc)

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    async def send(
        self,
        user: User,
        text: str,
        keypad: dict | None = None,
        metadata: dict | None = None,
        *,
        with_menu: bool = False,
    ) -> str | None:
        """Send, transparently dropping rich-text metadata if Rubika rejects it."""
        chat_keypad = K.main_chat_keypad(user.language) if with_menu else None
        try:
            return await self.rubika.send_message(
                user.chat_id,
                text,
                inline_keypad=keypad,
                chat_keypad=chat_keypad,
                chat_keypad_type="New" if chat_keypad else None,
                metadata=metadata,
            )
        except RubikaError as exc:
            if metadata is None:
                raise
            log.debug("retrying without metadata after: %s", exc)
            return await self.rubika.send_message(
                user.chat_id,
                text,
                inline_keypad=keypad,
                chat_keypad=chat_keypad,
                chat_keypad_type="New" if chat_keypad else None,
            )

    # ------------------------------------------------------------------
    # user management
    # ------------------------------------------------------------------
    async def ensure_user(self, chat_id: str, sender_id: str = "") -> User:
        user = await self.store.get_user(chat_id)
        if user is not None:
            await self.store.update_user(chat_id, last_seen_at=time.time())
            user.last_seen_at = time.time()
            return user

        first_name = last_name = username = ""
        try:
            chat = await self.rubika.call("getChat", {"chat_id": chat_id})
            if isinstance(chat, dict):
                first_name = str(chat.get("first_name") or "")
                last_name = str(chat.get("last_name") or "")
                username = str(chat.get("username") or "")
                sender_id = sender_id or str(chat.get("user_id") or "")
        except Exception as exc:  # noqa: BLE001 - cosmetic only
            log.debug("getChat failed for %s: %s", chat_id, exc)

        user = User(
            chat_id=chat_id,
            user_id=sender_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
            language=self.settings.default_language,
            min_score=self.settings.min_score,
            max_per_batch=self.settings.batch_max,
        )
        await self.store.upsert_user(user)
        self.dispatcher.users[chat_id] = user
        log.info("new user: %s (%s)", user.display_name, chat_id)
        return user

    # ------------------------------------------------------------------
    # update dispatch
    # ------------------------------------------------------------------
    async def handle_update(self, update: dict) -> None:
        utype = str(update.get("type") or "")
        chat_id = str(update.get("chat_id") or "")
        if not chat_id:
            return

        message = update.get("new_message") or update.get("updated_message") or {}
        if not isinstance(message, dict):
            message = {}

        if utype == "StoppedBot":
            await self.store.update_user(chat_id, is_active=False)
            log.info("user %s stopped the bot", chat_id)
            return

        sender_id = str(message.get("sender_id") or "")
        user = await self.ensure_user(chat_id, sender_id)

        if utype == "StartedBot" or not utype:
            await self.cmd_start(user)
            return
        if utype not in {"NewMessage", "UpdatedMessage"}:
            return

        aux = message.get("aux_data") or {}
        button_id = str(aux.get("button_id") or "").strip()
        text = str(message.get("text") or "").strip()

        if button_id:
            await self.on_button(user, button_id)
            return
        if text:
            await self.on_text(user, text)
            return

    # ------------------------------------------------------------------
    # text
    # ------------------------------------------------------------------
    async def on_text(self, user: User, text: str) -> None:
        lowered = text.lower().split("@")[0].strip()

        if lowered.startswith("/start"):
            return await self.cmd_start(user)
        if lowered.startswith("/help"):
            return await self.cmd_help(user)
        if lowered.startswith(("/latest", "/now", "/get")):
            return await self.cmd_latest(user)
        if lowered.startswith("/stats"):
            return await self.cmd_stats(user)
        if lowered.startswith(("/filters", "/settings")):
            return await self.cmd_filters(user)
        if lowered.startswith("/live"):
            toggle = "0" if user.live_mode else "1"
            return await self.set_live(user, toggle)
        if lowered.startswith("/resume"):
            return await self.cmd_resume(user)
        if lowered.startswith("/pause"):
            match = _PAUSE_RE.match(text)
            hours = 1
            if match and match.group(1):
                hours = int(match.group(1))
                if (match.group(2) or "h").lower() == "m":
                    hours = max(1, hours // 60)
            return await self.cmd_pause(user, hours)
        if lowered.startswith("/lang"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1 and parts[1].strip().lower() in {"en", "fa"}:
                return await self.set_language(user, parts[1].strip().lower())
            return await self.cmd_language(user)
        if lowered.startswith(("/menu", "/home")):
            return await self.cmd_menu(user)
        if lowered.startswith("/top"):
            return await self.cmd_top(user)
        if lowered.startswith("/"):
            text, keypad, meta = M.render_message(user, "unknown")
            return await self._reply(user, text, keypad, meta)

        # Free text we do not understand: nudge towards the keypad.
        text_out, keypad, meta = M.render_message(user, "unknown")
        await self._reply(user, text_out, keypad, meta)

    async def _reply(self, user: User, text: str, keypad: dict | None = None,
                     meta: dict | None = None) -> None:
        await self.send(user, text, keypad, meta)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    async def cmd_start(self, user: User) -> None:
        await self.store.update_user(user.chat_id, is_active=True, is_paused=False, paused_until=0)
        user.is_active = True
        user.is_paused = False
        user.paused_until = 0.0
        text, keypad, meta = M.render_welcome(user, self.channel_count())
        await self.send(user, text, None, meta, with_menu=True)

    async def cmd_help(self, user: User) -> None:
        text, keypad, meta = M.render_help(user)
        await self.send(user, text, None, meta, with_menu=True)

    async def cmd_menu(self, user: User) -> None:
        text, keypad, meta = M.render_message(user, "welcome_hint")
        await self.send(user, text, None, meta, with_menu=True)

    async def cmd_latest(self, user: User) -> None:
        sent = await self.dispatcher.force_flush(user.chat_id)
        if sent == 0:
            text, keypad, meta = M.render_message(user, "nothing_to_send")
            await self.send(user, text, None, meta)
        return None

    async def cmd_stats(self, user: User) -> None:
        stats = await self.store.stats()
        last_hour = await self.store.recent_delivery_count(user.chat_id, time.time() - 3600)
        delivered = await self._delivered_total(user.chat_id)
        text, keypad, meta = M.render_stats(user, stats, self.dispatcher.counters, last_hour, delivered)
        await self.send(user, text, keypad, meta)

    async def cmd_filters(self, user: User) -> None:
        text, keypad, meta = M.render_filters(user)
        await self.send(user, text, keypad, meta)

    async def cmd_language(self, user: User) -> None:
        text = f"🌐 {t(user.language, 'lang_title')}"
        await self.send(user, text, K.language_keypad(), M.title_metadata(text))

    async def cmd_top(self, user: User) -> None:
        rows = await self.store.top_configs(limit=5)
        text, keypad, meta = M.render_top(user, rows)
        await self.send(user, text, keypad, meta)

    async def cmd_pause(self, user: User, hours: int) -> None:
        hours = max(1, min(720, hours))
        until = time.time() + hours * 3600
        await self.store.update_user(user.chat_id, is_paused=True, paused_until=until)
        user.is_paused = True
        user.paused_until = until
        self.dispatcher.users[user.chat_id] = user
        text, keypad, meta = M.render_message(user, "paused_for", hours=hours)
        await self.send(user, text, None, meta, with_menu=True)

    async def cmd_resume(self, user: User) -> None:
        await self.store.update_user(user.chat_id, is_paused=False, paused_until=0)
        user.is_paused = False
        user.paused_until = 0.0
        self.dispatcher.users[user.chat_id] = user
        text, keypad, meta = M.render_message(user, "resumed")
        await self.send(user, text, None, meta, with_menu=True)

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------
    async def on_button(self, user: User, button_id: str) -> None:
        parts = button_id.split(":")
        action = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""

        if action == K.CB_COPY:
            return await self.cb_copy(user, arg, arg2)
        if action == K.CB_ALL:
            return await self.cb_copy_all(user, arg)
        if action == K.CB_DEAD:
            return await self.cb_report(user, arg, arg2, "dead")
        if action == K.CB_WORKS:
            return await self.cb_report(user, arg, arg2, "live")
        if action == K.CB_NOW:
            return await self.cmd_latest(user)
        if action == K.CB_STATS:
            return await self.cmd_stats(user)
        if action == K.CB_FILTERS:
            sub = button_id.split(":", 1)
            sub = sub[1] if len(sub) > 1 else ""
            return await self.cb_filters(user, sub)
        if action == K.CB_PAUSE:
            return await self.cmd_pause(user, int(arg) if arg.isdigit() else 1)
        if action == K.CB_RESUME:
            return await self.cmd_resume(user)
        if action == K.CB_HELP:
            return await self.cmd_help(user)
        if action == K.CB_LANG:
            if arg in {"en", "fa"}:
                return await self.set_language(user, arg)
            return await self.cmd_language(user)
        if action == K.CB_MENU:
            return await self.cmd_menu(user)
        if action == K.CB_TOP:
            return await self.cmd_top(user)
        if action == K.CB_SCORE:
            return await self.set_score(user, arg)
        if action == K.CB_PROTO:
            return await self.toggle_protocol(user, arg)
        if action == K.CB_BATCHSIZE:
            return await self.set_batch_size(user, arg)
        if action == K.CB_LIVE:
            return await self.set_live(user, arg)

        text, keypad, meta = M.render_message(user, "unknown")
        await self.send(user, text, None, meta)

    # ------------------------------------------------------------------
    async def _fps_for_batch(self, chat_id: str, batch_id: str):
        if not batch_id:
            return []
        return await self.store.batch_fingerprints(chat_id, batch_id)

    async def _cfg_for(self, fingerprint: str) -> ProxyConfig | None:
        row = await self.store.get_config(fingerprint)
        if not row:
            return None
        return ProxyConfig(
            protocol=str(row.get("protocol") or ""),
            server=str(row.get("server") or ""),
            port=int(row.get("port") or 0),
            network=str(row.get("network") or ""),
            security=str(row.get("security") or ""),
            remark=str(row.get("remark") or ""),
            raw=str(row.get("raw") or ""),
            geo=str(row.get("geo") or ""),
        )

    async def cb_copy(self, user: User, batch_id: str, idx: str) -> None:
        fps = await self._fps_for_batch(user.chat_id, batch_id)
        index = int(idx) if idx.isdigit() else 0
        if not fps or index >= len(fps):
            text, keypad, meta = M.render_message(user, "error_generic")
            return await self.send(user, text, None, meta)

        fingerprint = fps[index]
        cfg = await self._cfg_for(fingerprint)
        if cfg is None:
            text, keypad, meta = M.render_message(user, "error_generic")
            return await self.send(user, text, None, meta)

        row = await self.store.get_config(fingerprint) or {}
        await self.store.mark_copied(user.chat_id, fingerprint)
        await self.dispatcher.note_interest(user, cfg.protocol, 1.0)
        text, keypad, meta = M.render_single(
            user, cfg, float(row.get("score") or 0), index, batch_id,
            seen=int(row.get("seen_count") or 1),
        )
        await self.send(user, text, keypad, meta)

    async def cb_copy_all(self, user: User, batch_id: str) -> None:
        fps = await self._fps_for_batch(user.chat_id, batch_id)
        cfgs = []
        for fp in fps:
            cfg = await self._cfg_for(fp)
            if cfg:
                cfgs.append(cfg)
                await self.store.mark_copied(user.chat_id, fp)
        if not cfgs:
            text, keypad, meta = M.render_message(user, "error_generic")
            return await self.send(user, text, None, meta)
        if cfgs:
            await self.dispatcher.note_interest(user, cfgs[0].protocol, 0.5)
        text, keypad, meta = M.render_copy_all(user, cfgs)
        await self.send(user, text, None, meta)

    async def cb_report(self, user: User, batch_id: str, idx: str, verdict: str) -> None:
        fps = await self._fps_for_batch(user.chat_id, batch_id)
        index = int(idx) if idx.isdigit() else 0
        if not fps or index >= len(fps):
            text, keypad, meta = M.render_message(user, "error_generic")
            return await self.send(user, text, None, meta)

        fingerprint = fps[index]
        await self.store.report(fingerprint, verdict, user.chat_id)
        cfg = await self._cfg_for(fingerprint)
        if cfg and verdict == "live":
            await self.dispatcher.note_interest(user, cfg.protocol, 1.0)
        self.dispatcher.scorer.invalidate()

        key = "dead_thanks" if verdict == "dead" else "live_thanks"
        text, keypad, meta = M.render_message(user, key)
        await self.send(user, text, None, meta)

    async def cb_filters(self, user: User, sub: str) -> None:
        if sub == "proto":
            text, keypad, meta = M.render_filters(user)
            await self.send(user, text, K.protocol_keypad(user.language, user.protocols), meta)
            return
        if sub == "score":
            text, keypad, meta = M.render_filters(user)
            await self.send(user, text, K.score_keypad(user.language, user.min_score), meta)
            return
        if sub == "batch":
            text, keypad, meta = M.render_filters(user)
            await self.send(user, text, K.batchsize_keypad(user.language, user.max_per_batch), meta)
            return
        await self.cmd_filters(user)

    # ------------------------------------------------------------------
    # settings mutations
    # ------------------------------------------------------------------
    async def set_score(self, user: User, value: str) -> None:
        if not value.isdigit():
            return
        score = max(0, min(99, int(value)))
        await self.store.update_user(user.chat_id, min_score=score)
        user.min_score = score
        self.dispatcher.users[user.chat_id] = user
        text, keypad, meta = M.render_filters(user)
        await self.send(user, text, K.filters_keypad(user.language, user), meta)

    async def set_batch_size(self, user: User, value: str) -> None:
        if not value.isdigit():
            return
        size = max(1, min(10, int(value)))
        await self.store.update_user(user.chat_id, max_per_batch=size)
        user.max_per_batch = size
        self.dispatcher.users[user.chat_id] = user
        text, keypad, meta = M.render_filters(user)
        await self.send(user, text, K.filters_keypad(user.language, user), meta)

    async def toggle_protocol(self, user: User, proto: str) -> None:
        if proto == "all":
            selected: list[str] = []
        else:
            selected = list(user.protocols)
            if proto in selected:
                selected.remove(proto)
            else:
                selected.append(proto)
        await self.store.update_user(user.chat_id, protocols=selected)
        user.protocols = selected
        self.dispatcher.users[user.chat_id] = user
        text, keypad, meta = M.render_filters(user)
        await self.send(user, text, K.protocol_keypad(user.language, selected), meta)

    async def set_live(self, user: User, value: str) -> None:
        live = value == "1"
        await self.store.update_user(user.chat_id, live_mode=live)
        user.live_mode = live
        self.dispatcher.users[user.chat_id] = user
        key = "live_on" if live else "live_off"
        text = t(user.language, key, score=self.settings.instant_score)
        await self.send(user, text, K.filters_keypad(user.language, user), M.title_metadata(text))

    async def set_language(self, user: User, code: str) -> None:
        await self.store.update_user(user.chat_id, language=code)
        user.language = code
        self.dispatcher.users[user.chat_id] = user
        text = t(code, "lang_changed")
        await self.send(user, text, None, M.title_metadata(text), with_menu=True)

    # ------------------------------------------------------------------
    async def _delivered_total(self, chat_id: str) -> int:
        rows = await self.store.list_queued(chat_id, limit=1)
        del rows
        return await self.store.recent_delivery_count(chat_id, 0)


__all__ = ["BotHandlers", "BOT_COMMANDS"]
