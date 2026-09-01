"""Logging configuration shared by every module."""

from __future__ import annotations

import logging
import sys

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


class _ColorFormatter(logging.Formatter):
    """Compact coloured output - plain when the stream is not a TTY."""

    COLORS = {
        "DEBUG": "\033[38;5;244m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[48;5;196m\033[38;5;231m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool) -> None:
        super().__init__(
            fmt="%(asctime)s │ %(levelname)-7s │ %(name)-22s │ %(message)s",
            datefmt="%H:%M:%S",
        )
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        text = super().format(record)
        if not self.use_color:
            return text
        color = self.COLORS.get(record.levelname)
        return f"{color}{text}{self.RESET}" if color else text


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter(use_color))

    root.addHandler(handler)
    root.setLevel(_LEVELS.get(level.upper(), logging.INFO))

    # Third party libraries are very chatty at INFO.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
