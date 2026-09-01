"""Core data model for a proxy configuration."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

# Schemes we know how to understand.
KNOWN_PROTOCOLS = (
    "vless",
    "vmess",
    "trojan",
    "ss",
    "ssr",
    "hy",
    "hy2",
    "hysteria2",
    "hysteria",
    "tuic",
    "snell",
    "wireguard",
)

# Canonical display names.
PROTOCOL_LABEL = {
    "vless": "VLESS",
    "vmess": "VMess",
    "trojan": "Trojan",
    "ss": "Shadowsocks",
    "ssr": "ShadowsocksR",
    "hy": "Hysteria2",
    "hy2": "Hysteria2",
    "hysteria2": "Hysteria2",
    "hysteria": "Hysteria2",
    "tuic": "TUIC",
    "snell": "Snell",
    "wireguard": "WireGuard",
}

# Collapse the hysteria aliases onto one bucket. Channels are wildly
# inconsistent about which one they emit, and they are the same protocol.
PROTOCOL_ALIAS = {
    "hy": "hysteria2",
    "hy2": "hysteria2",
    "hysteria": "hysteria2",
}

_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)

_EMOJI_STRIP = re.compile(
    "[" "\U0001f300-\U0001faff" "\U0001f1e6-\U0001f1ff" "\u2600-\u27bf" "\ufe0f" "]+"
)

# Channels paste these between characters to break copy/paste. They are
# invisible in a chat client but corrupt every URI they touch.
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\u2061\ufeff\u00ad\u180e"

# Standard dashed UUID, or the same thing without dashes. Anything else is a
# truncated paste and no client will accept it.
_UUID_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{32})$",
    re.IGNORECASE,
)


@dataclass
class ProxyConfig:
    """A single, normalised proxy configuration."""

    protocol: str
    server: str
    port: int
    identity: str = ""
    network: str = ""
    security: str = ""
    sni: str = ""
    host: str = ""
    path: str = ""
    remark: str = ""
    raw: str = ""
    geo: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        self.protocol = PROTOCOL_ALIAS.get(self.protocol.strip().lower(), self.protocol.strip().lower())

    @property
    def label(self) -> str:
        return PROTOCOL_LABEL.get(self.protocol, self.protocol.upper())

    @property
    def clean_remark(self) -> str:
        """Remark with zero-width characters and control noise removed."""
        text = self.remark or ""
        for char in _ZERO_WIDTH:
            text = text.replace(char, "")
        text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
        return " ".join(text.split())

    @property
    def short_remark(self) -> str:
        text = _EMOJI_STRIP.sub("", self.clean_remark).strip(" -|@.,")
        return text[:42] if text else ""

    @property
    def is_private(self) -> bool:
        try:
            addr = ipaddress.ip_address(self.server)
        except ValueError:
            return False
        return any(addr in net for net in _PRIVATE_NETS)

    @property
    def is_valid(self) -> bool:
        """Hard sanity gate - anything failing this is never delivered."""
        if not self.protocol or not self.server:
            return False
        if not (0 < self.port < 65536):
            return False
        if self.is_private:
            return False
        # A malformed server name is almost always a truncated paste.
        if "." not in self.server and ":" not in self.server:
            return False
        # VLESS/VMess are useless without a real UUID - clients reject them.
        if self.protocol in {"vless", "vmess"} and not _UUID_RE.match(self.identity.strip()):
            return False
        return True

    # ------------------------------------------------------------------
    @property
    def fingerprint(self) -> str:
        """Content hash used for de-duplication.

        Deliberately excludes the remark and the raw text: the same server
        reposted by ten different channels with ten different names is still
        one config, and the user only ever wants to see it once.
        """
        parts = [
            self.protocol,
            self.server.strip().lower(),
            str(self.port),
            (self.identity or "").strip(),
            (self.network or "").strip().lower(),
            (self.security or "").strip().lower(),
            (self.sni or "").strip().lower(),
            (self.host or "").strip().lower(),
            (self.path or "").strip().rstrip("/").lower(),
            (self.extras.get("flow") or "").strip().lower(),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "server": self.server,
            "port": self.port,
            "identity": self.identity,
            "network": self.network,
            "security": self.security,
            "sni": self.sni,
            "host": self.host,
            "path": self.path,
            "remark": self.remark,
            "raw": self.raw,
            "geo": self.geo,
            "fingerprint": self.fingerprint,
        }


__all__ = ["ProxyConfig", "KNOWN_PROTOCOLS", "PROTOCOL_LABEL"]
