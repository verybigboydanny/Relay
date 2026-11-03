from __future__ import annotations

import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from relay.utils.errors import StorageError

SCHEMA_VERSION = 1
AUDIT_SCHEMA = 1
GENESIS = "0" * 64

COLLECTIONS = ("settings", "context", "memories", "events", "tasks", "warnings", "confirmations", "snapshots", "meta")
INDEX_FIELDS = ("guild_id", "actor_id", "target_id", "kind", "status", "ref_id", "run_at", "expires_at")
ORDER_FIELDS = ("ts", "run_at", "expires_at", "id")

AUDIT_FIELDS = (
    "event_id", "ts", "guild_id", "actor_id", "actor_type", "action", "target_id", "resource_type",
    "resource_id", "request_id", "plan_id", "task_id", "authorization_result", "confirmation_status",
    "status", "reason", "details", "error_code", "duration_ms", "schema_version",
)


def merge_patch(target: dict, patch: dict) -> dict:
    out = dict(target)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_patch(out[k], v)
        elif isinstance(v, dict):
            out[k] = merge_patch({}, v)
        else:
            out[k] = v
    return out


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def audit_hash(entry: dict, prev_hash: str, key: bytes | None) -> str:
    body = {k: entry.get(k) for k in AUDIT_FIELDS}
    msg = (prev_hash + canonical(body)).encode()
    if key:
        return hmac.new(key, msg, hashlib.sha256).hexdigest()
    return hashlib.sha256(msg).hexdigest()


def seal(entry: dict, prev_hash: str, key: bytes | None) -> dict:
    out = {k: entry.get(k) for k in AUDIT_FIELDS}
    out["schema_version"] = AUDIT_SCHEMA
    out["prev_hash"] = prev_hash
    out["hash"] = audit_hash(out, prev_hash, key)
    return out


def verify_rows(rows: list[dict], key: bytes | None) -> dict:
    problems = []
    prev = None
    for i, r in enumerate(rows):
        if prev is not None and r["prev_hash"] != prev:
            problems.append({"index": i, "event_id": r["event_id"], "problem": "broken chain"})
        if audit_hash(r, r["prev_hash"], key) != r["hash"]:
            problems.append({"index": i, "event_id": r["event_id"], "problem": "hash mismatch"})
        prev = r["hash"]
    return {"checked": len(rows), "problems": problems, "ok": not problems}


def check_collection(coll: str) -> str:
    if coll not in COLLECTIONS:
        raise StorageError(f"unknown collection '{coll}'")
    return coll


class Store(ABC):
    mode = "base"

    def __init__(self, hmac_key: bytes | None = None):
        self.hmac_key = hmac_key

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def health(self) -> dict: ...

    @abstractmethod
    async def put(self, coll: str, rec: dict) -> None: ...

    @abstractmethod
    async def get(self, coll: str, id: str, guild_id: int | None = None) -> dict | None: ...

    @abstractmethod
    async def delete(self, coll: str, id: str, guild_id: int | None = None) -> bool: ...

    @abstractmethod
    async def find(self, coll: str, *, guild_id: int | None = None, where: dict | None = None,
                   since: str | None = None, until: str | None = None, due_before: str | None = None,
                   order: str = "ts", desc: bool = False, limit: int = 100, offset: int = 0) -> list[dict]: ...

    @abstractmethod
    async def count(self, coll: str, *, guild_id: int | None = None, where: dict | None = None) -> int: ...

    @abstractmethod
    async def update_if(self, coll: str, id: str, expect: dict, patch: dict, guild_id: int | None = None) -> bool: ...

    @abstractmethod
    async def purge(self, coll: str, *, before: str, field: str = "ts", guild_id: int | None = None,
                    where: dict | None = None) -> int: ...

    @abstractmethod
    async def append_audit(self, entry: dict) -> dict: ...

    @abstractmethod
    async def query_audit(self, *, guild_id: int, actor_id: int | None = None, target_id: int | None = None,
                          action: str | None = None, status: str | None = None, since: str | None = None,
                          until: str | None = None, request_id: str | None = None, plan_id: str | None = None,
                          task_id: str | None = None, resource_id: str | None = None, limit: int = 50,
                          offset: int = 0, desc: bool = True) -> list[dict]: ...

    @abstractmethod
    async def audit_rows(self, guild_id: int) -> AsyncIterator[dict]: ...

    @abstractmethod
    async def prune_audit(self, guild_id: int, before: str) -> int: ...

    @abstractmethod
    async def import_audit(self, guild_id: int, rows: list[dict]) -> int: ...

    @abstractmethod
    async def guild_ids(self) -> list[int]: ...


def new_rec(id: str, guild_id: int, data: dict, *, ts: str, **fields) -> dict:
    rec = {"id": id, "guild_id": guild_id, "ts": ts, "data": data}
    for k in INDEX_FIELDS:
        if k != "guild_id":
            rec[k] = fields.get(k)
    return rec


def matches(rec: dict, where: dict | None, since, until, due_before) -> bool:
    if where:
        for k, v in where.items():
            cur = rec.get(k)
            if isinstance(v, (list, tuple, set)):
                if cur not in v:
                    return False
            elif cur != v:
                return False
    if since is not None and rec["ts"] < since:
        return False
    if until is not None and rec["ts"] >= until:
        return False
    if due_before is not None and not (rec.get("run_at") and rec["run_at"] <= due_before):
        return False
    return True
