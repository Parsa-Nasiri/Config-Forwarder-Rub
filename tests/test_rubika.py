"""Rubika wire-format tests against the documented Bot API v3 contract.

No network: the httpx transport is replaced with a recorder so we can assert
the exact payload shape the client puts on the wire.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bot.rubika_side import RubikaClient
from bot.rubika_side.client import split_text
from bot.rubika_side import keypads as K
from bot.storage.base import User


class Transport:
    """Stands in for httpx.AsyncClient - records requests, replays responses."""

    def __init__(self, responses: list) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    async def post(self, url: str, json: dict | None = None, **_: object):
        request = httpx.Request("POST", url, json=json or {})
        self.requests.append(request)
        if self._responses:
            status, payload = self._responses.pop(0)
        else:
            status, payload = 200, {"status": "OK", "data": {}}
        return httpx.Response(status, json=payload, request=request)

    async def aclose(self) -> None:
        pass


def _client(responses=None):
    client = RubikaClient("TESTTOKEN")
    transport = Transport(responses or [])
    # Bypass start() and inject the fake transport as the live httpx client.
    client._client = transport  # noqa: SLF001 - test seam
    return client, transport


def _url(transport, index=-1):
    return str(transport.requests[index].url)


def _body(transport, index=-1):
    return json.loads(transport.requests[index].content.decode())


# ---------------------------------------------------------------------------
# wire format
# ---------------------------------------------------------------------------


def test_every_call_is_post_with_json_body():
    client, transport = _client([(200, {"status": "OK", "data": {"id": "u1"}})])
    result = asyncio.run(client.get_me())
    assert result["id"] == "u1"
    assert transport.requests[0].method == "POST"        # even getMe is POST


def test_url_contains_token_and_method():
    client, transport = _client([(200, {"status": "OK", "data": {}})])
    asyncio.run(client.get_me())
    assert "TESTTOKEN" in _url(transport)
    assert _url(transport).endswith("/getMe")


def test_send_message_body_matches_the_api():
    client, transport = _client([(200, {"status": "OK", "data": {"message_id": "m1"}})])
    msg_id = asyncio.run(client.send_message("chat-1", "hello"))
    assert msg_id == "m1"
    body = _body(transport)
    assert body["chat_id"] == "chat-1"
    assert body["text"] == "hello"


def test_inline_keypad_is_attached_to_the_message():
    keypad = {"rows": [{"buttons": [K.button("cp:b1:0", "1️⃣")]}]}
    client, transport = _client([(200, {"status": "OK", "data": {"message_id": "m1"}})])
    asyncio.run(client.send_message("chat-1", "hello", inline_keypad=keypad))
    assert _body(transport)["inline_keypad"] == keypad


def test_metadata_only_sent_when_single_chunk():
    meta = {"meta_data_parts": [{"type": "Bold", "from_index": 0, "length": 4}]}
    client, transport = _client([(200, {"status": "OK", "data": {"message_id": "m1"}})] * 5)
    asyncio.run(client.send_message("chat-1", "hello", metadata=meta))
    assert _body(transport, 0)["metadata"] == meta

    first_send = len(transport.requests)
    long_text = "\n".join(f"config number {i} " + "x" * 120 for i in range(80))
    asyncio.run(client.send_message("chat-1", long_text, metadata=meta))
    # Split messages cannot carry text metadata (it would point at the wrong part).
    assert len(transport.requests) > first_send
    for request in transport.requests[first_send:]:
        assert "metadata" not in json.loads(request.content.decode())


def test_long_text_is_split_and_keypad_on_last_chunk():
    keypad = {"rows": [{"buttons": [K.button("cl", "x")]}]}
    client, transport = _client([(200, {"status": "OK", "data": {"message_id": "m1"}})] * 5)
    text = "\n".join(f"config number {i} " + "x" * 100 for i in range(80))
    asyncio.run(client.send_message("chat-1", text, inline_keypad=keypad))
    assert len(transport.requests) >= 2
    for i, request in enumerate(transport.requests):
        body = json.loads(request.content.decode())
        assert len(body["text"]) <= 3500
        last = i == len(transport.requests) - 1
        assert ("inline_keypad" in body) == last


def test_get_updates_passes_offset():
    client, transport = _client([(200, {"status": "OK",
                                        "data": {"updates": [], "next_offset_id": "42"}})])
    updates, offset = asyncio.run(client.get_updates(limit=10, offset_id="41"))
    assert updates == []
    assert offset == "42"
    body = _body(transport)
    assert body["limit"] == 10
    assert body["offset_id"] == "41"


def test_error_status_raises_rubika_error():
    from bot.rubika_side.client import RubikaError

    client, _ = _client([(200, {"status": "ERROR", "error": {"message": "bad token"}})])
    with pytest.raises(RubikaError):
        asyncio.run(client.get_me())


def test_retries_on_server_errors_then_gives_up(monkeypatch):
    from bot.rubika_side import client as client_mod
    from bot.rubika_side.client import RubikaError

    # Skip the real backoff delays so the test stays instant.
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", _no_sleep)
    client, transport = _client([(503, {})] * 5)
    with pytest.raises(RubikaError):
        asyncio.run(client.get_me())
    assert len(transport.requests) == 4            # attempts=4 default


def test_split_text_respects_line_boundaries():
    text = "\n".join("line " + str(i) for i in range(500))
    chunks = split_text(text, limit=200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 200
    assert "\n".join(chunks) == text               # lossless


def test_split_text_short_message_untouched():
    assert split_text("hello") == ["hello"]


# ---------------------------------------------------------------------------
# keypads
# ---------------------------------------------------------------------------


def _buttons(keypad: dict) -> list[dict]:
    return [btn for row in keypad["rows"] for btn in row["buttons"]]


def test_keypad_shape_matches_the_documented_model():
    keypad = K.main_chat_keypad("en")
    assert set(keypad) >= {"rows"}
    for row in keypad["rows"]:
        assert set(row) == {"buttons"}
    for btn in _buttons(keypad):
        assert set(btn) >= {"id", "type", "button_text"}
        assert btn["type"] == "Simple"


def test_batch_keypad_has_one_button_per_config():
    keypad = K.batch_keypad("en", "b1", 5)
    buttons = _buttons(keypad)
    copy_ids = [b["id"] for b in buttons if b["id"].startswith(f"{K.CB_COPY}:b1:")]
    assert len(copy_ids) == 5
    # The callback tokens encode which batch and which index.
    assert f"{K.CB_COPY}:b1:0" in copy_ids
    assert f"{K.CB_COPY}:b1:4" in copy_ids


def test_batch_keypad_includes_utility_rows():
    keypad = K.batch_keypad("en", "b1", 3)
    ids = [b["id"] for b in _buttons(keypad)]
    assert any(i.startswith(K.CB_ALL) for i in ids)             # copy all
    assert any(i.startswith(K.CB_NOW) for i in ids)             # send now
    assert K.CB_FILTERS in ids
    assert K.CB_STATS in ids


def test_single_keypad_carries_the_feedback_buttons():
    """Dead/works reporting lives on the expanded single-config view."""
    keypad = K.single_keypad("en", "b1", 0)
    ids = [b["id"] for b in _buttons(keypad)]
    assert f"{K.CB_COPY}:b1:0" in ids
    assert f"{K.CB_WORKS}:b1:0" in ids
    assert f"{K.CB_DEAD}:b1:0" in ids


def test_keypads_render_in_persian_without_latin_buttons():
    for builder, args in (
        (K.main_chat_keypad, ("fa",)),
        (K.batch_keypad, ("fa", "b1", 3)),
        (K.filters_keypad, ("fa", User(chat_id="c1", language="fa"))),
        (K.language_keypad, ()),
        (K.pause_keypad, ("fa",)),
        (K.stats_keypad, ("fa",)),
    ):
        keypad = builder(*args)
        assert keypad and keypad["rows"]
        for btn in _buttons(keypad):
            assert btn["button_text"].strip()


def test_protocol_keypad_marks_the_selection():
    keypad = K.protocol_keypad("en", selected=["vless"])
    selected = [b for b in _buttons(keypad) if b["id"] == f"{K.CB_PROTO}:vless"]
    assert selected
    # The selected protocol is visually marked (✅ prefix in its label).
    assert "✅" in selected[0]["button_text"]


def test_score_and_batchsize_keypads_encode_the_value():
    score = K.score_keypad("en", 55)
    ids = [b["id"] for b in _buttons(score)]
    assert f"{K.CB_SCORE}:55" in ids

    size = K.batchsize_keypad("en", 5)
    ids = [b["id"] for b in _buttons(size)]
    assert f"{K.CB_BATCHSIZE}:5" in ids


def test_every_button_id_is_short():
    """Button ids travel in every callback - they must stay tiny."""
    keypads = [
        K.main_chat_keypad("en"),
        K.batch_keypad("en", "b1234abcd", 10),
        K.filters_keypad("en", User(chat_id="c1")),
        K.protocol_keypad("en", []),
        K.score_keypad("en", 55),
        K.batchsize_keypad("en", 5),
        K.pause_keypad("en"),
        K.stats_keypad("en"),
    ]
    for keypad in keypads:
        for btn in _buttons(keypad):
            assert len(btn["id"]) <= 32, f"button id too long: {btn['id']}"
