"""Cheap, offline geo hints.

Config channels almost always prefix the remark with a flag emoji
("🇩🇪 Germany · Hetzner #12"). We exploit that instead of calling an external
geo-ip service: it costs nothing, never rate-limits, and works offline.
"""

from __future__ import annotations

import re

_FLAG_RE = re.compile("[\U0001f1e6-\U0001f1ff]{2}")

# Regional indicator letters -> ISO 3166-1 alpha-2 letters.
_ISO: dict[str, str] = {
    "\U0001f1e6": "A", "\U0001f1e7": "B", "\U0001f1e8": "C", "\U0001f1e9": "D",
    "\U0001f1ea": "E", "\U0001f1eb": "F", "\U0001f1ec": "G", "\U0001f1ed": "H",
    "\U0001f1ee": "I", "\U0001f1ef": "J", "\U0001f1f0": "K", "\U0001f1f1": "L",
    "\U0001f1f2": "M", "\U0001f1f3": "N", "\U0001f1f4": "O", "\U0001f1f5": "P",
    "\U0001f1f6": "Q", "\U0001f1f7": "R", "\U0001f1f8": "S", "\U0001f1f9": "T",
    "\U0001f1fa": "U", "\U0001f1fb": "V", "\U0001f1fc": "W", "\U0001f1fd": "X",
    "\U0001f1fe": "Y", "\U0001f1ff": "Z",
}

# iso2 -> (flag emoji, English name)
_COUNTRY_NAMES: dict[str, tuple[str, str]] = {
    "DE": ("\U0001f1e9\U0001f1ea", "Germany"),
    "NL": ("\U0001f1f3\U0001f1f1", "Netherlands"),
    "FR": ("\U0001f1eb\U0001f1f7", "France"),
    "GB": ("\U0001f1ec\U0001f1e7", "United Kingdom"),
    "US": ("\U0001f1fa\U0001f1f8", "United States"),
    "CA": ("\U0001f1e8\U0001f1e6", "Canada"),
    "FI": ("\U0001f1eb\U0001f1ee", "Finland"),
    "SE": ("\U0001f1f8\U0001f1ea", "Sweden"),
    "NO": ("\U0001f1f3\U0001f1f4", "Norway"),
    "CH": ("\U0001f1e8\U0001f1ed", "Switzerland"),
    "AT": ("\U0001f1e6\U0001f1f9", "Austria"),
    "PL": ("\U0001f1f5\U0001f1f1", "Poland"),
    "CZ": ("\U0001f1e8\U0001f1ff", "Czechia"),
    "ES": ("\U0001f1ea\U0001f1f8", "Spain"),
    "IT": ("\U0001f1ee\U0001f1f9", "Italy"),
    "TR": ("\U0001f1f9\U0001f1f7", "Türkiye"),
    "RU": ("\U0001f1f7\U0001f1fa", "Russia"),
    "IR": ("\U0001f1ee\U0001f1f7", "Iran"),
    "AE": ("\U0001f1e6\U0001f1ea", "United Arab Emirates"),
    "SA": ("\U0001f1f8\U0001f1e6", "Saudi Arabia"),
    "IN": ("\U0001f1ee\U0001f1f3", "India"),
    "SG": ("\U0001f1f8\U0001f1ec", "Singapore"),
    "JP": ("\U0001f1ef\U0001f1f5", "Japan"),
    "KR": ("\U0001f1f0\U0001f1f7", "South Korea"),
    "HK": ("\U0001f1ed\U0001f1f0", "Hong Kong, China"),
    "TW": ("\U0001f1f9\U0001f1fc", "Taiwan, China"),
    "CN": ("\U0001f1e8\U0001f1f3", "China"),
    "AU": ("\U0001f1e6\U0001f1fa", "Australia"),
    "BR": ("\U0001f1e7\U0001f1f7", "Brazil"),
    "UA": ("\U0001f1fa\U0001f1e6", "Ukraine"),
    "RO": ("\U0001f1f7\U0001f1f4", "Romania"),
    "HU": ("\U0001f1ed\U0001f1fa", "Hungary"),
    "BE": ("\U0001f1e7\U0001f1ea", "Belgium"),
    "DK": ("\U0001f1e9\U0001f1f0", "Denmark"),
    "IE": ("\U0001f1ee\U0001f1ea", "Ireland"),
    "IL": ("\U0001f1ee\U0001f1f1", "Israel"),
    "MY": ("\U0001f1f2\U0001f1fe", "Malaysia"),
    "TH": ("\U0001f1f9\U0001f1ed", "Thailand"),
    "VN": ("\U0001f1fb\U0001f1f3", "Vietnam"),
    "ID": ("\U0001f1ee\U0001f1e9", "Indonesia"),
    "ZA": ("\U0001f1ff\U0001f1e6", "South Africa"),
    "AM": ("\U0001f1e6\U0001f1f2", "Armenia"),
    "AZ": ("\U0001f1e6\U0001f1ff", "Azerbaijan"),
    "GE": ("\U0001f1ec\U0001f1ea", "Georgia"),
    "KZ": ("\U0001f1f0\U0001f1ff", "Kazakhstan"),
    "IQ": ("\U0001f1ee\U0001f1f6", "Iraq"),
    "AF": ("\U0001f1e6\U0001f1eb", "Afghanistan"),
    "PK": ("\U0001f1f5\U0001f1f0", "Pakistan"),
}

# Fallback: plain-text country / city hints commonly found in remarks.
_NAME_LOOKUP: dict[str, str] = {}
for _iso2, (_flag, _name) in _COUNTRY_NAMES.items():
    _NAME_LOOKUP[_name.lower()] = _iso2
_NAME_LOOKUP.update(
    {
        "germany": "DE", "deutschland": "DE", "netherlands": "NL", "holland": "NL",
        "france": "FR", "frankfurt": "DE", "falkenstein": "DE", "hetzner": "DE",
        "london": "GB", "uk": "GB", "united kingdom": "GB", "england": "GB",
        "usa": "US", "united states": "US", "america": "US", "los angeles": "US",
        "canada": "CA", "finland": "FI", "sweden": "SE", "norway": "NO",
        "switzerland": "CH", "austria": "AT", "poland": "PL", "czech": "CZ",
        "spain": "ES", "italy": "IT", "turkey": "TR", "russia": "RU",
        "iran": "IR", "emirates": "AE", "dubai": "AE", "india": "IN",
        "singapore": "SG", "japan": "JP", "korea": "KR", "hongkong": "HK",
        "taiwan": "TW", "china": "CN", "australia": "AU", "brazil": "BR",
        "amsterdam": "NL", "paris": "FR", "warsaw": "PL", "moscow": "RU",
        "istanbul": "TR", "tehran": "IR", "stockholm": "SE", "helsinki": "FI",
    }
)


def flag_to_iso(flag: str) -> str:
    """Convert a two-letter regional indicator pair into an ISO code."""
    return "".join(_ISO.get(ch, "") for ch in flag)


def iso_to_geo(iso2: str) -> str:
    """Return a ready-to-display '🇩🇪 Germany' string for an ISO code."""
    entry = _COUNTRY_NAMES.get(iso2.upper())
    return f"{entry[0]} {entry[1]}" if entry else ""


def guess_geo(remark: str, server: str = "") -> str:
    """Best-effort location string for a config, or '' when unknown.

    Strategy: flag emoji in the remark first (most reliable), then an
    English/Persian-ish keyword match, then nothing.
    """
    text = (remark or "").strip()

    for match in _FLAG_RE.findall(text):
        iso2 = flag_to_iso(match)
        geo = iso_to_geo(iso2)
        if geo:
            return geo

    lowered = text.lower()
    # Longest key first so "united states" wins over "states".
    for key in sorted(_NAME_LOOKUP, key=len, reverse=True):
        if key in lowered:
            return iso_to_geo(_NAME_LOOKUP[key])

    return ""


__all__ = ["guess_geo", "iso_to_geo", "flag_to_iso"]
