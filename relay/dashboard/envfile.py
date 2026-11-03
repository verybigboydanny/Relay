from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from dotenv import dotenv_values

KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def mask(value: str) -> str:
    return "••••" if len(value) <= 8 else "••••" + value[-4:]


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env(path: Path, updates: dict[str, str | None]) -> None:
    for k, v in updates.items():
        if v is not None and ("\n" in v or "\r" in v or "\x00" in v):
            raise ValueError(f"{k} can't contain line breaks")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], set()
    for line in lines:
        m = KEY_RE.match(line)
        if m and m[1] in updates:
            if m[1] in done:
                continue
            done.add(m[1])
            if updates[m[1]] is not None:
                out.append(f"{m[1]}={_quote(updates[m[1]])}")
            continue
        out.append(line)
    for k, v in updates.items():
        if k not in done and v is not None:
            out.append(f"{k}={_quote(v)}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp, path)
