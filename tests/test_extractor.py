"""Parser tests: every real-world scheme must survive the round trip."""

from __future__ import annotations

from bot.engine.extractor import (
    dedupe,
    extract_from_text,
    parse_subscription_body,
    parse_uri,
    subscription_urls,
)
from bot.engine.models import ProxyConfig

# ---------------------------------------------------------------------------
# per-scheme parsing
# ---------------------------------------------------------------------------


def test_vless_reality_extracts_every_field():
    from .conftest import VLESS_REALITY

    cfg = parse_uri(VLESS_REALITY)
    assert cfg is not None
    assert cfg.protocol == "vless"
    assert cfg.server == "de-fra01.example.net"
    assert cfg.port == 443
    assert cfg.network == "grpc"
    assert cfg.security == "reality"
    assert cfg.sni == "www.microsoft.com"
    assert cfg.identity == "7a2b3c4d-1111-2222-3333-444455556666"
    assert cfg.extras.get("flow") == "xtls-rprx-vision"
    assert cfg.is_valid
    assert cfg.geo.startswith("🇩🇪")  # flag in the remark


def test_vmess_base64_payload():
    from .conftest import VMESS_WS

    cfg = parse_uri(VMESS_WS)
    assert cfg is not None
    assert cfg.protocol == "vmess"
    assert cfg.server == "nl-ams.example.net"
    assert cfg.port == 443
    assert cfg.network == "ws"
    assert cfg.security == "tls"
    assert cfg.host == "cloud.example.com"
    assert cfg.path == "/ws"
    assert cfg.is_valid


def test_trojan_and_ss():
    from .conftest import SS_2022, TROJAN_WS

    trojan = parse_uri(TROJAN_WS)
    assert trojan and trojan.protocol == "trojan"
    assert trojan.server == "tr-ist01.example.org"
    assert trojan.port == 8443
    assert trojan.network == "ws"

    ss = parse_uri(SS_2022)
    assert ss and ss.protocol == "ss"
    assert ss.server == "fi-hel01.example.net"
    assert ss.port == 443


def test_hysteria2_and_tuic_and_ssr():
    from .conftest import HY2, SSR, TUIC

    hy2 = parse_uri(HY2)
    assert hy2 and hy2.protocol == "hysteria2"
    assert hy2.server == "nl-ams01.example.io"

    tuic = parse_uri(TUIC)
    assert tuic and tuic.protocol == "tuic"
    assert tuic.server == "jp-tyo01.example.net"

    ssr = parse_uri(SSR)
    assert ssr and ssr.protocol == "ssr"
    assert ssr.port == 1234


def test_protocol_aliases_collapse():
    """hy:// / hysteria:// must land on the same protocol as hy2://."""
    a = parse_uri("hy://pw@a.example.net:443?sni=x#a")
    b = parse_uri("hysteria://pw@a.example.net:443?sni=x#a")
    c = parse_uri("hy2://pw@a.example.net:443?sni=x#a")
    assert a and b and c
    assert a.protocol == b.protocol == c.protocol == "hysteria2"


def test_garbage_is_rejected():
    assert parse_uri("not-a-config") is None
    assert parse_uri("") is None
    assert parse_uri("https://example.com/subscription") is None


# ---------------------------------------------------------------------------
# text level extraction
# ---------------------------------------------------------------------------


def test_extract_from_noisy_channel_post():
    text = (
        "سلام دوستان 🌹\n"
        "کانال جدید ما رو join کنید 👇\n"
        "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@x1.example.net:443"
        "?type=ws&security=tls&sni=a.com#Node-1\n"
        "\n"
        "trojan://pw@y2.example.net:443?security=tls&sni=b.com#Node-2\n"
        "\n"
        "لینک سابسکریپشن:\n"
        "https://sub.example.net/api/v1/client/subscribe?token=abc123\n"
    )
    configs = extract_from_text(text)
    assert len(configs) == 2
    assert {c.server for c in configs} == {"x1.example.net", "y2.example.net"}

    subs = subscription_urls(text)
    assert subs == ["https://sub.example.net/api/v1/client/subscribe?token=abc123"]


def test_zero_width_characters_are_stripped():
    """Channels paste zero-width junk to break copy/paste. We undo it."""
    clean = "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@zw.example.net:443?type=ws#n"
    dirty = clean.replace("vless", "vle\u200bss").replace(".net", ".\u200cnet")
    configs = extract_from_text(dirty)
    assert len(configs) == 1
    assert configs[0].server == "zw.example.net"


def test_extract_preserves_order_and_dedupes_within_message():
    a = "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@a.example.net:443#one"
    b = "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@b.example.net:443#two"
    configs = dedupe(extract_from_text(f"{a}\n{b}\n{a}"))
    assert [c.server for c in configs] == ["a.example.net", "b.example.net"]


def test_fingerprint_ignores_remark_and_casing():
    """The same server reposted under a new name must collapse to one row."""
    a = parse_uri("vless://11111111-1111-1111-1111-111111111111@dup.example.net:443?type=ws#Channel-A")
    b = parse_uri("vless://11111111-1111-1111-1111-111111111111@DUP.example.net:443?type=ws#PROMO!!!")
    assert a and b
    assert a.fingerprint == b.fingerprint

    c = parse_uri("vless://22222222-1111-1111-1111-111111111111@dup.example.net:443?type=ws#other")
    assert c and c.fingerprint != a.fingerprint


# ---------------------------------------------------------------------------
# validity gate
# ---------------------------------------------------------------------------


def test_is_valid_rejects_broken_configs():
    cases = [
        "vless://no-uuid@bad.example.net:443#x",            # missing uuid
        "vless://11111111-1111-1111-1111-111111111111@127.0.0.1:443#x",   # loopback
        "vless://11111111-1111-1111-1111-111111111111@10.0.0.5:443#x",    # private
        "vless://11111111-1111-1111-1111-111111111111@ok.example.net:0#x",  # bad port
        "vless://11111111-1111-1111-1111-111111111111@:443#x",            # no host
    ]
    for uri in cases:
        cfg = parse_uri(uri)
        if cfg is not None:
            assert not cfg.is_valid, f"{uri} should have been rejected"


def test_valid_public_config_passes():
    cfg = parse_uri("vless://11111111-1111-1111-1111-111111111111@ok.example.net:443#x")
    assert cfg and cfg.is_valid


# ---------------------------------------------------------------------------
# subscription bodies
# ---------------------------------------------------------------------------


def test_subscription_plain_uri_list():
    body = "\n".join(
        [
            "vless://aaaaaaaa-0000-0000-0000-000000000000@s1.example.net:443#a",
            "trojan://pw@s2.example.net:443#b",
        ]
    )
    configs = dedupe(parse_subscription_body(body))
    assert len(configs) == 2


def test_subscription_base64_blob():
    import base64

    inner = "\n".join(
        [
            "vless://bbbbbbbb-0000-0000-0000-000000000000@b1.example.net:443#a",
            "trojan://pw@b2.example.net:443#b",
        ]
    )
    blob = base64.b64encode(inner.encode()).decode()
    configs = dedupe(parse_subscription_body(blob))
    assert len(configs) == 2


def test_subscription_clash_yaml():
    yaml_text = """
proxies:
  - name: "clash-vless"
    type: vless
    server: c1.example.net
    port: 443
    uuid: cccccccc-0000-0000-0000-000000000000
    network: ws
    tls: true
    servername: sni.example.net
    ws-opts:
      path: /cw
  - name: "clash-ss"
    type: ss
    server: c2.example.net
    port: 8388
    cipher: aes-256-gcm
    password: "secret"
"""
    configs = dedupe(parse_subscription_body(yaml_text))
    servers = {c.server for c in configs}
    assert "c1.example.net" in servers
    assert "c2.example.net" in servers


def test_subscription_json_array():
    import json

    payload = [
        {"type": "trojan", "name": "json-1", "server": "j1.example.net", "port": 443, "password": "pw"},
        {"type": "trojan", "name": "json-2", "server": "j2.example.net", "port": 443, "password": "pw"},
    ]
    configs = dedupe(parse_subscription_body(json.dumps(payload)))
    assert len(configs) == 2


def test_subscription_garbage_yields_nothing():
    assert parse_subscription_body("hello world, no configs here") == []


# ---------------------------------------------------------------------------
# model helpers
# ---------------------------------------------------------------------------


def test_clean_remark_strips_noise():
    cfg = ProxyConfig(
        protocol="vless",
        server="s.example.net",
        port=443,
        remark="\u200bChannel @promo \u200b  Node-1  ",
    )
    assert "promo" in cfg.clean_remark or "Node-1" in cfg.clean_remark
    assert "\u200b" not in cfg.clean_remark


def test_to_dict_round_trip():
    from .conftest import VLESS_REALITY

    cfg = parse_uri(VLESS_REALITY)
    assert cfg
    data = cfg.to_dict()
    assert data["protocol"] == "vless"
    assert data["server"] == "de-fra01.example.net"
    assert data["fingerprint"] == cfg.fingerprint


def test_parsers_are_pure_and_synchronous():
    """The parsers must stay sync - they run inside Telethon's event loop."""
    import inspect

    for fn in (parse_uri, extract_from_text, parse_subscription_body, subscription_urls, dedupe):
        assert not inspect.iscoroutinefunction(fn), f"{fn.__name__} became async"
