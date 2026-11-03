from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import time
from pathlib import Path
from typing import AsyncIterator

from relay.storage.base import (
    GENESIS, INDEX_FIELDS, ORDER_FIELDS, SCHEMA_VERSION, Store, check_collection, matches, merge_patch, seal, verify_rows,
)
from relay.utils.errors import StorageCorruption, StorageError

log = logging.getLogger(__name__)

MIGRATIONS: dict[int, callable] = {}


class JsonStore(Store):
    mode = "local"

    def __init__(self, directory: str | Path, hmac_key: bytes | None = None):
        super().__init__(hmac_key)
        self.root = Path(directory)
        self._lock = asyncio.Lock()
        self._alock = asyncio.Lock()
        self._cache: dict[Path, dict] = {}
        self._audit_tail: dict[int, tuple[str, int]] = {}

    async def open(self) -> None:
        await asyncio.to_thread(self._init_dirs)

    def _init_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        (self.root / "audit").mkdir(exist_ok=True)
        meta = self.root / "_schema.json"
        if not meta.exists():
            self._write_atomic(meta, {"version": SCHEMA_VERSION})
            return
        try:
            ver = int(json.loads(meta.read_text())["version"])
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            raise StorageCorruption(f"{meta} is unreadable: {e}")
        if ver > SCHEMA_VERSION:
            raise StorageError(f"data directory is schema v{ver}, this build only understands v{SCHEMA_VERSION}")
        while ver < SCHEMA_VERSION:
            MIGRATIONS[ver + 1](self.root)
            ver += 1
            self._write_atomic(meta, {"version": ver})

    async def close(self) -> None:
        self._cache.clear()

    async def health(self) -> dict:
        def probe():
            t = self.root / ".probe"
            t.write_text("ok")
            t.unlink()
        try:
            await asyncio.to_thread(probe)
            return {"mode": "local", "ok": True, "directory": str(self.root.resolve())}
        except OSError as e:
            return {"mode": "local", "ok": False, "error": str(e)}

    def _path(self, coll: str, gid: int) -> Path:
        return self.root / check_collection(coll) / f"{int(gid)}.json"

    def _write_atomic(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        shutil.copy2(path, path.with_name(path.name + ".bak"))
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @staticmethod
    def _valid(payload) -> dict:
        if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION or not isinstance(payload.get("records"), dict):
            raise ValueError("bad envelope")
        for k, r in payload["records"].items():
            if not isinstance(r, dict) or r.get("id") != k or "data" not in r or "ts" not in r:
                raise ValueError(f"bad record '{k}'")
        return payload

    def _load_sync(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return self._valid(json.loads(path.read_text(encoding="utf-8")))["records"]
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            bak = path.with_name(path.name + ".bak")
            stamp = time.strftime("%Y%m%d%H%M%S")
            aside = path.with_name(f"{path.name}.corrupt-{stamp}")
            shutil.copy2(path, aside)
            if bak.exists():
                try:
                    recs = self._valid(json.loads(bak.read_text(encoding="utf-8")))["records"]
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    recs = None
                if recs is not None:
                    log.error("%s was corrupt (%s); restored from backup, bad copy kept at %s", path, e, aside)
                    shutil.copy2(bak, path)
                    return recs
            raise StorageCorruption(f"{path} is corrupt ({e}) and no usable backup exists; copy kept at {aside}")

    async def _shard(self, coll: str, gid: int) -> dict:
        path = self._path(coll, gid)
        if path not in self._cache:
            self._cache[path] = await asyncio.to_thread(self._load_sync, path)
        return self._cache[path]

    async def _flush(self, coll: str, gid: int) -> None:
        path = self._path(coll, gid)
        await asyncio.to_thread(self._write_atomic, path, {"version": SCHEMA_VERSION, "records": self._cache[path]})

    def _shard_guilds(self, coll: str) -> list[int]:
        d = self.root / check_collection(coll)
        if not d.exists():
            return []
        return sorted(int(p.stem) for p in d.glob("*.json") if p.stem.isdigit())

    async def guild_ids(self) -> list[int]:
        found = set()
        for c in ("settings", "tasks", "events", "memories"):
            found.update(self._shard_guilds(c))
        a = self.root / "audit"
        if a.exists():
            found.update(int(p.name) for p in a.iterdir() if p.name.isdigit())
        found.discard(0)
        return sorted(found)

    async def put(self, coll: str, rec: dict) -> None:
        for k in ("id", "guild_id", "ts", "data"):
            if k not in rec:
                raise StorageError(f"record missing '{k}'")
        gid = int(rec["guild_id"])
        async with self._lock:
            shard = await self._shard(coll, gid)
            old = shard.get(rec["id"])
            shard[rec["id"]] = {**{k: rec.get(k) for k in INDEX_FIELDS}, "id": rec["id"], "ts": rec["ts"],
                                "guild_id": gid, "data": rec["data"]}
            try:
                await self._flush(coll, gid)
            except Exception:
                if old is None:
                    shard.pop(rec["id"], None)
                else:
                    shard[rec["id"]] = old
                raise

    async def get(self, coll, id, guild_id=None):
        async with self._lock:
            gids = [guild_id] if guild_id is not None else self._shard_guilds(coll)
            for g in gids:
                r = (await self._shard(coll, g)).get(id)
                if r is not None:
                    return json.loads(json.dumps(r))
        return None

    async def delete(self, coll, id, guild_id=None):
        async with self._lock:
            gids = [guild_id] if guild_id is not None else self._shard_guilds(coll)
            for g in gids:
                shard = await self._shard(coll, g)
                if id in shard:
                    old = shard.pop(id)
                    try:
                        await self._flush(coll, g)
                    except Exception:
                        shard[id] = old
                        raise
                    return True
        return False

    async def find(self, coll, *, guild_id=None, where=None, since=None, until=None, due_before=None,
                   order="ts", desc=False, limit=100, offset=0):
        if order not in ORDER_FIELDS:
            raise StorageError(f"can't order by '{order}'")
        out = []
        async with self._lock:
            gids = [guild_id] if guild_id is not None else self._shard_guilds(coll)
            for g in gids:
                for r in (await self._shard(coll, g)).values():
                    if matches(r, where, since, until, due_before):
                        out.append(r)
        out.sort(key=lambda r: (r.get(order) or "", r["id"]), reverse=desc)
        return [json.loads(json.dumps(r)) for r in out[offset:offset + limit]]

    async def count(self, coll, *, guild_id=None, where=None):
        n = 0
        async with self._lock:
            gids = [guild_id] if guild_id is not None else self._shard_guilds(coll)
            for g in gids:
                n += sum(1 for r in (await self._shard(coll, g)).values() if matches(r, where, None, None, None))
        return n

    async def update_if(self, coll, id, expect, patch, guild_id=None):
        async with self._lock:
            gids = [guild_id] if guild_id is not None else self._shard_guilds(coll)
            for g in gids:
                shard = await self._shard(coll, g)
                r = shard.get(id)
                if r is None:
                    continue
                if any(r.get(k) != v for k, v in expect.items()):
                    return False
                new = json.loads(json.dumps(r))
                for k, v in patch.items():
                    if k == "data":
                        new["data"] = merge_patch(new["data"], v)
                    elif k in INDEX_FIELDS or k == "ts":
                        new[k] = v
                    else:
                        raise StorageError(f"can't patch '{k}'")
                shard[id] = new
                try:
                    await self._flush(coll, g)
                except Exception:
                    shard[id] = r
                    raise
                return True
        return False

    async def purge(self, coll, *, before, field="ts", guild_id=None, where=None):
        if field not in ORDER_FIELDS:
            raise StorageError(f"can't purge by '{field}'")
        removed = 0
        async with self._lock:
            gids = [guild_id] if guild_id is not None else self._shard_guilds(coll)
            for g in gids:
                shard = await self._shard(coll, g)
                dead = [k for k, r in shard.items()
                        if r.get(field) and r[field] < before and matches(r, where, None, None, None)]
                if not dead:
                    continue
                saved = {k: shard[k] for k in dead}
                for k in dead:
                    del shard[k]
                try:
                    await self._flush(coll, g)
                except Exception:
                    shard.update(saved)
                    raise
                removed += len(dead)
        return removed

    def _audit_dir(self, gid: int) -> Path:
        return self.root / "audit" / str(int(gid))

    def _audit_files(self, gid: int) -> list[Path]:
        d = self._audit_dir(gid)
        return sorted(d.glob("*.jsonl")) if d.exists() else []

    def _load_tail(self, gid: int) -> tuple[str, int]:
        files = self._audit_files(gid)
        for path in reversed(files):
            data = path.read_bytes()
            if not data:
                continue
            lines = data.split(b"\n")
            complete = data.endswith(b"\n")
            last = lines[-2] if complete else lines[-1]
            if not complete:
                keep = b"\n".join(lines[:-1]) + (b"\n" if len(lines) > 1 else b"")
                try:
                    json.loads(last)
                    complete = True
                    with open(path, "ab") as f:
                        f.write(b"\n")
                except json.JSONDecodeError:
                    log.error("truncating torn audit line at end of %s", path)
                    with open(path, "wb") as f:
                        f.write(keep)
                    last = lines[-2] if len(lines) > 1 and lines[-2] else None
                    if last is None:
                        continue
            try:
                row = json.loads(last)
                return row["hash"], int(row.get("seq", 0))
            except (json.JSONDecodeError, KeyError) as e:
                raise StorageCorruption(f"audit file {path} has an unreadable last record: {e}")
        return GENESIS, 0

    def _append_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        if new:
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    async def append_audit(self, entry: dict) -> dict:
        gid = int(entry["guild_id"])
        async with self._alock:
            try:
                if gid not in self._audit_tail:
                    self._audit_tail[gid] = await asyncio.to_thread(self._load_tail, gid)
                prev, seq = self._audit_tail[gid]
                row = seal(entry, prev, self.hmac_key)
                row["seq"] = seq + 1
                month = row["ts"][:7]
                path = self._audit_dir(gid) / f"{month}.jsonl"
                await asyncio.to_thread(self._append_line, path, json.dumps(row, sort_keys=True, separators=(",", ":")))
            except StorageError:
                raise
            except OSError as e:
                self._audit_tail.pop(gid, None)
                raise StorageError(f"audit write failed: {e}")
            self._audit_tail[gid] = (row["hash"], row["seq"])
            return row

    def _read_audit(self, path: Path) -> list[dict]:
        out = []
        with open(path, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    raise StorageCorruption(f"{path} line {n} is not valid JSON")
        return out

    async def query_audit(self, *, guild_id, actor_id=None, target_id=None, action=None, status=None, since=None,
                          until=None, request_id=None, plan_id=None, task_id=None, resource_id=None, limit=50,
                          offset=0, desc=True):
        files = self._audit_files(guild_id)
        if since:
            files = [f for f in files if f.stem >= since[:7]]
        if until:
            files = [f for f in files if f.stem <= until[:7]]
        if desc:
            files.reverse()
        want = {"actor_id": actor_id, "target_id": target_id, "action": action, "status": status,
                "request_id": request_id, "plan_id": plan_id, "task_id": task_id, "resource_id": resource_id}
        want = {k: v for k, v in want.items() if v is not None}
        out = []
        for path in files:
            rows = await asyncio.to_thread(self._read_audit, path)
            if desc:
                rows.reverse()
            for r in rows:
                if any(r.get(k) != v for k, v in want.items()):
                    continue
                if since and r["ts"] < since or until and r["ts"] >= until:
                    continue
                out.append(r)
            if len(out) >= offset + limit:
                break
        return out[offset:offset + limit]

    async def audit_rows(self, guild_id: int) -> AsyncIterator[dict]:
        for path in self._audit_files(guild_id):
            for r in await asyncio.to_thread(self._read_audit, path):
                yield r

    async def prune_audit(self, guild_id: int, before: str) -> int:
        async with self._alock:
            n = 0
            for path in self._audit_files(guild_id):
                if path.stem < before[:7]:
                    rows = await asyncio.to_thread(self._read_audit, path)
                    n += len(rows)
                    await asyncio.to_thread(path.unlink)
            return n

    async def import_audit(self, guild_id: int, rows: list[dict]) -> int:
        if not rows:
            return 0
        async with self._alock:
            tail = await asyncio.to_thread(self._load_tail, guild_id)
            if tail[0] != GENESIS:
                raise StorageError(f"destination already has audit entries for guild {guild_id}")
            check = verify_rows(rows, self.hmac_key)
            if not check["ok"]:
                raise StorageError(f"source audit chain for guild {guild_id} doesn't verify; refusing to import it")
            for i, r in enumerate(rows, 1):
                row = dict(r)
                row["seq"] = i
                path = self._audit_dir(guild_id) / f"{row['ts'][:7]}.jsonl"
                await asyncio.to_thread(self._append_line, path, json.dumps(row, sort_keys=True, separators=(",", ":")))
            self._audit_tail[guild_id] = (rows[-1]["hash"], len(rows))
            return len(rows)
