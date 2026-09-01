"""Minimal, resilient client for the Rubika Bot API (v3).

Endpoint shape::

    POST https://botapi.rubika.ir/v3/{token}/{method}

Every method is a POST with a JSON body - including ``getMe`` and
``getUpdates``. Responses arrive wrapped in ``{"data": ...}``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

import httpx

log = logging.getLogger("rubika.client")

MAX_TEXT_LENGTH = 3500          # stay comfortably under Rubika's hard limit
_RETRY_STATUS = {429, 500, 502, 503, 504}


class RubikaError(RuntimeError):
    """Raised when Rubika rejects a request in a way we cannot recover from."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class RubikaClient:
    def __init__(self, token: str, base_url: str = "https://botapi.rubika.ir/v3",
                 timeout: float = 25.0) -> None:
        self.token = token
        self.base = f"{base_url.rstrip('/')}/{token}"
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._me: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def c(self) -> httpx.AsyncClient:
        if self._client is None:  # pragma: no cover
            raise RuntimeError("RubikaClient.start() was never awaited")
        return self._client

    # ------------------------------------------------------------------
    def _unwrap(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        status = payload.get("status")
        if status and str(status).lower() not in {"ok", "success", "true"}:
            detail = payload.get("message") or payload.get("error") or payload
            raise RubikaError(f"Rubika returned status={status}: {detail}")
        return payload.get("data", payload)

    async def call(self, method: str, body: dict[str, Any] | None = None,
                   *, attempts: int = 4) -> Any:
        """POST a method with bounded exponential-backoff retries."""
        url = f"{self.base}/{method}"
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                resp = await self.c.post(url, json=body or {})
            except httpx.RequestError as exc:
                last_error = exc
                wait = min(20.0, 1.5 * (2 ** attempt))
                log.warning("network error calling %s (attempt %d): %s", method, attempt + 1, exc)
                await asyncio.sleep(wait)
                continue

            if resp.status_code in _RETRY_STATUS:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(
                    20.0, 1.5 * (2 ** attempt)
                )
                log.warning("%s -> HTTP %d, retrying in %.1fs", method, resp.status_code, wait)
                await asyncio.sleep(wait)
                last_error = RubikaError(f"HTTP {resp.status_code}", retryable=True)
                continue

            if resp.status_code >= 400:
                raise RubikaError(f"{method} -> HTTP {resp.status_code}: {resp.text[:200]}")

            try:
                payload = resp.json()
            except ValueError:
                raise RubikaError(f"{method} -> non-JSON response: {resp.text[:200]}") from None
            return self._unwrap(payload)

        raise RubikaError(f"{method} failed after {attempts} attempts: {last_error}", retryable=True)

    # ------------------------------------------------------------------
    # core methods
    # ------------------------------------------------------------------
    async def get_me(self) -> dict[str, Any]:
        """Validate the token and cache the bot identity."""
        me = await self.call("getMe")
        self._me = me if isinstance(me, dict) else {}
        log.info(
            "rubika bot online: %s",
            self._me.get("username") or self._me.get("first_name") or self._me.get("id", "?"),
        )
        return self._me or {}

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        inline_keypad: dict[str, Any] | None = None,
        chat_keypad: dict[str, Any] | None = None,
        chat_keypad_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        disable_notification: bool = False,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send one message. Long text is split transparently."""
        chunks = split_text(text)
        last_id: str | None = None
        for index, chunk in enumerate(chunks):
            body: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_notification": disable_notification,
            }
            if reply_to_message_id and index == 0:
                body["reply_to_message_id"] = reply_to_message_id
            # Keypads only make sense on the final chunk.
            if index == len(chunks) - 1:
                if inline_keypad:
                    body["inline_keypad"] = inline_keypad
                if chat_keypad:
                    body["chat_keypad"] = chat_keypad
                    body["chat_keypad_type"] = chat_keypad_type or "New"
                if metadata and len(chunks) == 1:
                    body["metadata"] = metadata
            data = await self.call("sendMessage", body)
            if isinstance(data, dict):
                last_id = data.get("message_id") or last_id
        return last_id

    async def get_updates(self, limit: int = 50, offset_id: str | None = None
                          ) -> tuple[list[dict[str, Any]], str | None]:
        body: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if offset_id:
            body["offset_id"] = offset_id
        data = await self.call("getUpdates", body)
        if not isinstance(data, dict):
            return [], None
        updates = data.get("updates") or []
        next_offset = data.get("next_offset_id")
        return list(updates), (str(next_offset) if next_offset else None)

    async def edit_message_text(self, chat_id: str, message_id: str, text: str) -> None:
        await self.call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_TEXT_LENGTH]},
        )

    async def edit_inline_keypad(self, chat_id: str, message_id: str,
                                 inline_keypad: dict[str, Any]) -> None:
        await self.call(
            "editInlineKeypad",
            {"chat_id": chat_id, "message_id": message_id, "inline_keypad": inline_keypad},
        )

    async def edit_chat_keypad(self, chat_id: str, keypad: dict[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"chat_id": chat_id}
        if keypad is None:
            body["chat_keypad_type"] = "Remove"
        else:
            body["chat_keypad_type"] = "New"
            body["chat_keypad"] = keypad
        await self.call("editChatKeypad", body)

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def set_commands(self, commands: Iterable[dict[str, str]]) -> None:
        await self.call("setCommands", {"bot_commands": list(commands)})

    async def send_location(self, chat_id: str, latitude: str, longitude: str) -> None:
        await self.call(
            "sendLocation", {"chat_id": chat_id, "latitude": latitude, "longitude": longitude}
        )

    # ------------------------------------------------------------------
    @property
    def me(self) -> dict[str, Any]:
        return self._me or {}


def split_text(text: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Split on paragraph/line boundaries so messages never break mid-config."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [text[:limit]]


__all__ = ["RubikaClient", "RubikaError", "split_text"]
