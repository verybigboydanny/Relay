from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6,7}\.[A-Za-z\d_-]{27,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(authorization|token|api[_-]?key|secret|password|passwd)(['\"]?\s*[:=]\s*['\"]?)(?:bearer\s+)?[^\s'\",}]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}"),
]


def scrub(text: str) -> str:
    text = _PATTERNS[0].sub("[redacted]", text)
    text = _PATTERNS[1].sub("[redacted]", text)
    text = _PATTERNS[2].sub(lambda m: f"{m[1]}{m[2]}[redacted]", text)
    return _PATTERNS[3].sub("Bearer [redacted]", text)


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = ()
        return True


def setup_logging(level: str = "INFO", directory: str | Path = "logs") -> None:
    Path(directory).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    file_h = RotatingFileHandler(Path(directory) / "relay.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    console = logging.StreamHandler()
    for h in (file_h, console):
        h.setFormatter(fmt)
        h.addFilter(ScrubFilter())
        root.addHandler(h)
    for noisy in ("discord.http", "discord.gateway", "httpx", "httpcore", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
