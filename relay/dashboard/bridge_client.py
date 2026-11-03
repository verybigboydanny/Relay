"""The app's side of the mailbox to the running bot (see relay/bridge.py)."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

STALE = 20.0


def _dir(root) -> Path:
    return Path(root) / "data" / "bridge"


def read_state(root, max_age: float = STALE) -> dict | None:
    """What the live bot last published, or None if it isn't running / is stale."""
    try:
        st = json.loads((_dir(root) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or time.time() - float(st.get("ts", 0)) > max_age:
        return None
    return st


async def call(root, payload: dict, timeout: float = 120.0) -> dict:
    d = _dir(root)
    inbox, outbox = d / "inbox", d / "outbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    mid = uuid.uuid4().hex[:16]
    req = inbox / f"{mid}.json"
    tmp = inbox / f"{mid}.json.tmp"
    tmp.write_text(json.dumps({**payload, "id": mid, "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, req)
    out = outbox / f"{mid}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if out.exists():
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
                out.unlink()
                return data if isinstance(data, dict) else {"ok": False, "error": "Got a garbled answer from the bot."}
            except (OSError, ValueError):
                pass
        await asyncio.sleep(0.12)
    try:
        req.unlink()
    except OSError:
        pass
    return {"ok": False, "error": "Relay didn't answer in time. Is it still connected to Discord?"}
