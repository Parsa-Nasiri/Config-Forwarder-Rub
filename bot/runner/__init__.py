"""Process supervision: leadership, handoff and uptime reporting."""

from .github_handoff import dispatch_handoff, set_github_output
from .heartbeat import build_payload, write_heartbeat
from .leader import LOCK_NAME, LeaderLock

__all__ = [
    "LeaderLock", "LOCK_NAME",
    "dispatch_handoff", "set_github_output",
    "write_heartbeat", "build_payload",
]
