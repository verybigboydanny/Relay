from __future__ import annotations

import re
from typing import Any

_KEYS = re.compile(r"(token|secret|password|passwd|authorization|api[_-]?key|cookie|credential)", re.I)
_VALUES = [
    re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6,7}\.[A-Za-z\d_-]{27,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(?:password|passwd|pwd|api[_ -]?key|secret|token)\s*(?:is|:|=)\s*\S{6,}"),
    re.compile(r"\b(?:sk|pk|rk|ghp|xox[bap])[-_][A-Za-z0-9_-]{16,}"),
]


def has_secret(text: str) -> bool:
    return any(p.search(text) for p in _VALUES)


def scrub_text(text: str, limit: int = 300) -> str:
    for p in _VALUES:
        text = p.sub("[redacted]", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def redact(obj: Any, limit: int = 300, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        return {str(k): ("[redacted]" if _KEYS.search(str(k)) else redact(v, limit, depth + 1)) for k, v in list(obj.items())[:60]}
    if isinstance(obj, (list, tuple, set)):
        return [redact(v, limit, depth + 1) for v in list(obj)[:60]]
    if isinstance(obj, str):
        return scrub_text(obj, limit)
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    return scrub_text(str(obj), limit)
