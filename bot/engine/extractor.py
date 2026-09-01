"""Turn raw Telegram channel text into normalised :class:`ProxyConfig` objects.

Supports every scheme the public config channels actually use:

    vless://  vmess://  trojan://  ss://  ssr://
    hy2:// / hysteria2:// / hysteria://  tuic://  snell://  wireguard://

...plus Clash YAML/JSON documents, base64 subscription blobs and remote
subscription URLs.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Iterable
from urllib.parse import unquote

from .geo import guess_geo
from .models import ProxyConfig

log = logging.getLogger("engine.extractor")

# ---------------------------------------------------------------------------
# low level helpers
# ---------------------------------------------------------------------------

_SCHEMES = (
    "vless", "vmess", "trojan", "ssr", "ss",
    "hysteria2", "hy2", "hysteria", "tuic", "snell", "wireguard",
)

# Channels insert zero width characters inside URIs to defeat auto-linking.
_INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff"

_URI_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(_SCHEMES) + r")://([^\s<>\"'`\u0600-\u06ff]+)",
    re.IGNORECASE,
)

_TRAILING_JUNK = ".,;:!?)»”’'\"\\]}"

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

_SUB_HINTS = (
    "sub", "subscribe", "subscription", "clash", "profile",
    "api/v1/client", "getprofile", "token=", "config",
)


def _b64_decode(data: str) -> bytes | None:
    """Tolerant base64 decode (standard and url-safe, with/without padding)."""
    s = "".join(data.split())
    if not s:
        return None
    padded = s + "=" * (-len(s) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            out = decoder(padded)
        except Exception:  # noqa: BLE001 - tolerant by design
            continue
        if out:
            return out
    return None


def _b64_text(data: str) -> str | None:
    raw = _b64_decode(data)
    if raw is None:
        return None
    for encoding in ("utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _split_fragment(body: str) -> tuple[str, str]:
    """Split `main#remark` -> (main, remark)."""
    if "#" in body:
        main, _, remark = body.partition("#")
        return main, unquote(remark).strip()
    return body, ""


def _split_params(query: str) -> dict[str, str]:
    """Manual query parsing.

    ``urllib.parse.parse_qs`` turns '+' into a space, which silently corrupts
    base64 payloads embedded in query strings, so we do it by hand.
    """
    params: dict[str, str] = {}
    if not query:
        return params
    for chunk in query.split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        if not key:
            continue
        params[unquote(key).strip().lower()] = unquote(value).strip()
    return params


def _split_host_port(text: str) -> tuple[str, int]:
    text = text.strip().rstrip("/")
    if not text:
        return "", 0
    if text.startswith("["):  # IPv6 literal
        end = text.find("]")
        if end == -1:
            return "", 0
        host = text[1:end]
        rest = text[end + 1:]
        if not rest.startswith(":"):
            return "", 0
        try:
            return host, int(rest[1:])
        except ValueError:
            return "", 0
    if ":" not in text:
        return "", 0
    host, _, port_str = text.rpartition(":")
    if not host:
        return "", 0
    try:
        return host.strip(), int(port_str.strip())
    except ValueError:
        return "", 0


def _clean_uri(uri: str) -> str:
    return uri.strip().rstrip(_TRAILING_JUNK)


# ---------------------------------------------------------------------------
# per-scheme parsers
# ---------------------------------------------------------------------------


def _finalise(
    cfg: ProxyConfig,
    remark_from_fragment: str = "",
    fallback_geo: bool = True,
) -> ProxyConfig | None:
    """Attach remark/geo and reject anything structurally broken."""
    if not cfg.remark:
        cfg.remark = remark_from_fragment
    if fallback_geo and not cfg.geo:
        cfg.geo = guess_geo(cfg.remark, cfg.server)
    if not cfg.is_valid:
        return None
    return cfg


def parse_vless(uri: str) -> ProxyConfig | None:
    body = uri[len("vless://"):]
    main, remark = _split_fragment(body)
    main, _, query = main.partition("?")
    params = _split_params(query)

    if "@" not in main:
        return None
    identity, _, hostport = main.rpartition("@")
    server, port = _split_host_port(hostport)
    if not server or not port:
        return None

    security = (params.get("security") or "").lower()
    if not security and params.get("tls") == "tls":
        security = "tls"
    if security in {"", "none"} and params.get("encryption", "").lower() not in {"none", ""}:
        security = params.get("encryption", "").lower()

    extras = {
        "flow": params.get("flow", ""),
        "encryption": params.get("encryption", "none"),
        "fp": params.get("fp", ""),
        "pbk": params.get("pbk", ""),
        "sid": params.get("sid", ""),
    }
    cfg = ProxyConfig(
        protocol="vless",
        server=server,
        port=port,
        identity=unquote(identity),
        network=(params.get("type") or "tcp").lower(),
        security=security,
        sni=params.get("sni") or params.get("peer") or "",
        host=params.get("host") or "",
        path=params.get("path") or "",
        remark=remark,
        raw=uri,
        extras=extras,
    )
    return _finalise(cfg, remark)


def parse_vmess(uri: str) -> ProxyConfig | None:
    body = uri[len("vmess://"):]
    body = body.split("#")[0]
    decoded = _b64_text(body)
    if not decoded:
        return None
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    server = str(data.get("add") or "").strip()
    try:
        port = int(str(data.get("port") or 0))
    except ValueError:
        return None
    if not server or not port:
        return None

    tls_flag = str(data.get("tls") or "").lower()
    security = "tls" if tls_flag in {"tls", "true", "1"} else ""
    if data.get("sni"):
        security = security or "tls"

    cfg = ProxyConfig(
        protocol="vmess",
        server=server,
        port=port,
        identity=str(data.get("id") or ""),
        network=str(data.get("net") or "tcp").lower(),
        security=security,
        sni=str(data.get("sni") or data.get("peer") or ""),
        host=str(data.get("host") or ""),
        path=str(data.get("path") or ""),
        remark=str(data.get("ps") or ""),
        raw=uri,
        extras={"aid": str(data.get("aid") or 0), "scy": str(data.get("scy") or "auto")},
    )
    return _finalise(cfg, str(data.get("ps") or ""))


def parse_trojan(uri: str) -> ProxyConfig | None:
    body = uri[len("trojan://"):]
    main, remark = _split_fragment(body)
    main, _, query = main.partition("?")
    params = _split_params(query)

    if "@" not in main:
        return None
    identity, _, hostport = main.rpartition("@")
    server, port = _split_host_port(hostport)
    if not server or not port:
        return None

    security = (params.get("security") or "tls").lower()
    cfg = ProxyConfig(
        protocol="trojan",
        server=server,
        port=port,
        identity=unquote(identity),
        network=(params.get("type") or "tcp").lower(),
        security=security,
        sni=params.get("sni") or params.get("peer") or "",
        host=params.get("host") or "",
        path=params.get("path") or "",
        remark=remark,
        raw=uri,
        extras={"flow": params.get("flow", "")},
    )
    return _finalise(cfg, remark)


def parse_ss(uri: str) -> ProxyConfig | None:
    body = uri[len("ss://"):].strip()
    main, remark = _split_fragment(body)
    main, _, query = main.partition("?")
    params = _split_params(query)

    method = ""
    password = ""
    hostport = ""

    if "@" in main:
        userinfo, _, hostport = main.rpartition("@")
        # SIP002: userinfo is base64(method:password) - but many clients emit
        # it unencoded, so accept both.
        candidate = _b64_text(userinfo)
        if candidate and ":" in candidate:
            method, _, password = candidate.partition(":")
        elif ":" in userinfo:
            method, _, password = userinfo.partition(":")
        else:
            plain = _b64_text(userinfo)
            if plain and ":" in plain:
                method, _, password = plain.partition(":")
    else:
        # Legacy: the whole payload is base64(method:password@host:port).
        decoded = _b64_text(main)
        if not decoded:
            return None
        userinfo, _, hostport = decoded.rpartition("@")
        if ":" not in userinfo:
            return None
        method, _, password = userinfo.partition(":")

    server, port = _split_host_port(hostport)
    if not server or not port:
        # Last resort: the SIP002 userinfo itself may have been base64'd
        # together with the host ("ss://<b64>").
        decoded = _b64_text(main)
        if decoded and "@" in decoded:
            _, _, hostport = decoded.rpartition("@")
            server, port = _split_host_port(hostport)
    if not server or not port:
        return None

    plugin = params.get("plugin") or ""
    obfs = ""
    if plugin.startswith("obfs"):
        obfs = "obfs"

    cfg = ProxyConfig(
        protocol="ss",
        server=server,
        port=port,
        identity=password,
        network=obfs or "tcp",
        security="",
        sni="",
        host="",
        path="",
        remark=remark,
        raw=uri,
        extras={"method": (method or "unknown").lower(), "plugin": plugin},
    )
    return _finalise(cfg, remark)


def parse_ssr(uri: str) -> ProxyConfig | None:
    body = uri[len("ssr://"):].strip()
    decoded = _b64_text(body)
    if not decoded:
        return None

    main, _, query = decoded.partition("/?")
    params = _split_params(query)

    parts = main.split(":")
    if len(parts) < 6:
        return None
    server, port_str, protocol, method, obfs, password_b64 = parts[:6]
    try:
        port = int(port_str)
    except ValueError:
        return None
    password = _b64_text(password_b64) or password_b64

    cfg = ProxyConfig(
        protocol="ssr",
        server=server,
        port=port,
        identity=password,
        network=obfs,
        security="",
        sni="",
        host=params.get("obfsparam") or "",
        path=params.get("protoparam") or "",
        remark=params.get("remarks") or "",
        raw=uri,
        extras={"method": method.lower(), "protocol": protocol.lower(), "obfs": obfs.lower()},
    )
    return _finalise(cfg, params.get("remarks") or "")


def parse_hysteria2(uri: str, scheme: str = "hy2") -> ProxyConfig | None:
    prefix_len = len(scheme) + 3
    body = uri[prefix_len:]
    main, remark = _split_fragment(body)
    main, _, query = main.partition("?")
    params = _split_params(query)

    identity = ""
    if "@" in main:
        identity, _, hostport = main.rpartition("@")
    else:
        hostport = main
        identity = params.get("auth") or params.get("password") or ""
    server, port = _split_host_port(hostport)
    if not server or not port:
        return None

    security = (params.get("security") or "tls").lower()
    cfg = ProxyConfig(
        protocol="hysteria2",
        server=server,
        port=port,
        identity=unquote(identity),
        network="udp",
        security=security,
        sni=params.get("sni") or params.get("peer") or "",
        host="",
        path=params.get("obfs-param") or "",
        remark=remark,
        raw=uri,
        extras={
            "alpn": params.get("alpn") or "",
            "obfs": params.get("obfs") or "",
            "up": params.get("upmbps") or params.get("up") or "",
            "down": params.get("downmbps") or params.get("down") or "",
        },
    )
    return _finalise(cfg, remark)


def parse_tuic(uri: str) -> ProxyConfig | None:
    body = uri[len("tuic://"):]
    main, remark = _split_fragment(body)
    main, _, query = main.partition("?")
    params = _split_params(query)

    identity = ""
    if "@" in main:
        identity, _, hostport = main.rpartition("@")
    else:
        hostport = main
    server, port = _split_host_port(hostport)
    if not server or not port:
        return None

    cfg = ProxyConfig(
        protocol="tuic",
        server=server,
        port=port,
        identity=unquote(identity),
        network="udp",
        security=(params.get("congestion_control") and "tls") or "tls",
        sni=params.get("sni") or params.get("peer") or "",
        host="",
        path="",
        remark=remark,
        raw=uri,
        extras={"alpn": params.get("alpn") or "", "udp_relay_mode": params.get("udp_relay_mode", "")},
    )
    return _finalise(cfg, remark)


def parse_snell(uri: str) -> ProxyConfig | None:
    body = uri[len("snell://"):]
    main, remark = _split_fragment(body)
    main, _, query = main.partition("?")
    params = _split_params(query)
    if "@" not in main:
        return None
    identity, _, hostport = main.rpartition("@")
    server, port = _split_host_port(hostport)
    if not server or not port:
        return None
    cfg = ProxyConfig(
        protocol="snell",
        server=server,
        port=port,
        identity=unquote(identity),
        network=params.get("obfs") or "tcp",
        security="",
        sni="",
        host=params.get("obfs-host") or "",
        path="",
        remark=remark,
        raw=uri,
        extras={"version": params.get("version", "")},
    )
    return _finalise(cfg, remark)


def parse_wireguard(uri: str) -> ProxyConfig | None:
    body = uri[len("wireguard://"):]
    main, remark = _split_fragment(body)
    main, _, query = main.partition("?")
    params = _split_params(query)
    if "@" not in main:
        return None
    identity, _, hostport = main.rpartition("@")
    server, port = _split_host_port(hostport)
    if not server or not port:
        return None
    cfg = ProxyConfig(
        protocol="wireguard",
        server=server,
        port=port,
        identity=unquote(identity),
        network="udp",
        security="",
        sni="",
        host="",
        path="",
        remark=remark,
        raw=uri,
        extras={"publickey": params.get("publickey", ""), "address": params.get("address", "")},
    )
    return _finalise(cfg, remark)


_PARSERS = {
    "vless": parse_vless,
    "vmess": parse_vmess,
    "trojan": parse_trojan,
    "ss": parse_ss,
    "ssr": parse_ssr,
    "hy2": parse_hysteria2,
    "hy": parse_hysteria2,
    "hysteria2": parse_hysteria2,
    "hysteria": parse_hysteria2,
    "tuic": parse_tuic,
    "snell": parse_snell,
    "wireguard": parse_wireguard,
}


def parse_uri(uri: str) -> ProxyConfig | None:
    """Parse a single proxy URI. Returns None when it cannot be understood."""
    uri = _clean_uri(uri)
    if "://" not in uri:
        return None
    scheme = uri.split("://", 1)[0].lower()
    parser = _PARSERS.get(scheme)
    if parser is None:
        return None
    try:
        return parser(uri)
    except Exception as exc:  # noqa: BLE001 - never let one bad URI kill a scan
        log.debug("failed to parse %s uri: %s", scheme, exc)
        return None


# ---------------------------------------------------------------------------
# text / document level extraction
# ---------------------------------------------------------------------------


def extract_from_text(text: str) -> list[ProxyConfig]:
    """Pull every usable config out of a block of text.

    Handles the zero-width characters channels insert, de-duplicates within the
    message, and preserves the order in which configs appeared.
    """
    if not text:
        return []
    cleaned = text
    for ch in _INVISIBLE:
        cleaned = cleaned.replace(ch, "")

    out: list[ProxyConfig] = []
    seen: set[str] = set()
    for match in _URI_RE.finditer(cleaned):
        uri = _clean_uri(match.group(0))
        cfg = parse_uri(uri)
        if cfg is None:
            continue
        fp = cfg.fingerprint
        if fp in seen:
            continue
        seen.add(fp)
        out.append(cfg)
    return out


def looks_like_subscription(url: str) -> bool:
    """Heuristic: is this HTTP URL a subscription endpoint?"""
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    return any(hint in lowered for hint in _SUB_HINTS)


def subscription_urls(text: str) -> list[str]:
    return [u.rstrip(_TRAILING_JUNK) for u in _URL_RE.findall(text or "") if looks_like_subscription(u)]


def parse_clash(data: Any) -> list[ProxyConfig]:
    """Convert a Clash / Clash.Meta config document into configs."""
    proxies: list[Any] = []
    if isinstance(data, dict):
        value = data.get("proxies")
        if isinstance(value, list):
            proxies = value
    elif isinstance(data, list):
        proxies = data

    out: list[ProxyConfig] = []
    for item in proxies:
        if not isinstance(item, dict):
            continue
        cfg = _from_clash_node(item)
        if cfg:
            out.append(cfg)
    return out


def _from_clash_node(node: dict[str, Any]) -> ProxyConfig | None:
    ptype = str(node.get("type") or "").lower()
    server = str(node.get("server") or "")
    try:
        port = int(node.get("port") or 0)
    except (TypeError, ValueError):
        return None

    network = "tcp"
    path = ""
    host = ""
    ws_opts = node.get("ws-opts") or node.get("ws-path") or {}
    if isinstance(ws_opts, dict):
        path = str(ws_opts.get("path") or "")
        headers = ws_opts.get("headers") or {}
        if isinstance(headers, dict):
            host = str(headers.get("Host") or headers.get("host") or "")
    if node.get("network"):
        network = str(node["network"]).lower()
    elif node.get("grpc-opts"):
        network = "grpc"
    elif ws_opts:
        network = "ws"

    security = ""
    if node.get("reality-opts"):
        security = "reality"
    elif node.get("tls") is True:
        security = "tls"
    elif str(node.get("security") or "") in {"tls", "reality"}:
        security = str(node["security"]).lower()

    if ptype in {"vmess", "vless"}:
        identity = str(node.get("uuid") or "")
    elif ptype == "trojan":
        identity = str(node.get("password") or "")
    elif ptype in {"ss", "shadowsocks"}:
        identity = str(node.get("password") or "")
    elif ptype in {"hysteria", "hysteria2", "hy2"}:
        identity = str(node.get("password") or node.get("auth") or node.get("auth-str") or "")
    elif ptype == "tuic":
        identity = f"{node.get('uuid') or ''}:{node.get('password') or ''}"
    else:
        identity = str(node.get("password") or node.get("uuid") or "")

    remark = str(node.get("name") or "")
    raw = json.dumps(node, ensure_ascii=False, sort_keys=True)

    cfg = ProxyConfig(
        protocol={"shadowsocks": "ss", "hysteria": "hysteria2", "hy2": "hysteria2"}.get(ptype, ptype),
        server=server,
        port=port,
        identity=identity,
        network=network,
        security=security,
        sni=str(node.get("servername") or node.get("sni") or ""),
        host=host,
        path=path,
        remark=remark,
        raw=raw,
        extras={"method": str(node.get("cipher") or ""), "source": "clash"},
    )
    if not cfg.is_valid:
        return None
    if not cfg.geo:
        cfg.geo = guess_geo(remark, server)
    return cfg


def parse_subscription_body(body: str) -> list[ProxyConfig]:
    """Parse whatever a subscription endpoint returned.

    Order of attempts: base64 blob -> plain URI list -> Clash YAML -> JSON.
    """
    if not body or not body.strip():
        return []

    stripped = body.strip()

    # 1. base64 (with or without newlines) - the most common encoding.
    if not stripped.lstrip().startswith(("{", "[", "proxies", "#")):
        decoded = _b64_text(stripped)
        if decoded and any(f"{s}://" in decoded for s in _SCHEMES):
            return extract_from_text(decoded)

    # 2. plain newline separated URIs.
    found = extract_from_text(stripped)
    if found:
        return found

    # 3. Clash YAML.
    try:
        import yaml  # imported lazily: only documents need it

        data = yaml.safe_load(stripped)
        if isinstance(data, (dict, list)):
            parsed = parse_clash(data)
            if parsed:
                return parsed
    except Exception:  # noqa: BLE001
        pass

    # 4. JSON.
    try:
        parsed = parse_clash(json.loads(stripped))
    except Exception:  # noqa: BLE001
        parsed = []
    return parsed


def dedupe(configs: Iterable[ProxyConfig]) -> list[ProxyConfig]:
    """Order-preserving de-duplication by fingerprint."""
    seen: set[str] = set()
    out: list[ProxyConfig] = []
    for cfg in configs:
        fp = cfg.fingerprint
        if fp in seen:
            continue
        seen.add(fp)
        out.append(cfg)
    return out


__all__ = [
    "extract_from_text",
    "parse_uri",
    "parse_subscription_body",
    "parse_clash",
    "subscription_urls",
    "looks_like_subscription",
    "dedupe",
]
