"""Extraction, scoring and delivery engine."""

from .dispatcher import Dispatcher, QueuedItem
from .extractor import dedupe, extract_from_text, parse_subscription_body, parse_uri
from .health import probe
from .models import ProxyConfig
from .ratelimit import RateLimiter, TokenBucket
from .scorer import ScoreResult, Scorer

__all__ = [
    "Dispatcher", "QueuedItem", "ProxyConfig",
    "Scorer", "ScoreResult",
    "extract_from_text", "parse_uri", "parse_subscription_body", "dedupe",
    "RateLimiter", "TokenBucket", "probe",
]
