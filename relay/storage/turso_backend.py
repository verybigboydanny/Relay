from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from relay.storage.base import (
    GENESIS, INDEX_FIELDS, ORDER_FIELDS, Store, check_collection, seal, verify_rows,
)
from relay.storage.migrations import LATEST, MIGRATIONS
from relay.utils.errors import StorageError

log = logging.getLogger(__name__)

AUDIT_COLS = ("event_id", "ts", "guild_id", "actor_id", "actor_type", "action", "target_id", "resource_type",
              "resource_id", "request_id", "plan_id", "task_id", "authorization_result", "confirmation_status",
              "status", "reason", "details", "error_code", "duration_ms", "schema_version", "prev_hash", "hash")
SELECT_COLS = ("id", "guild_id", "ts", "actor_id", "target_id", "kind", "status", "ref_id", "run_at", "expires_at", "data")
QUERY_TIMEOUT = 20


class TursoStore(Store):
    mode = "turso"

    def __init__(self, client=None, *, url: str | None = None, token: str | None = None, hmac_key: bytes | None = None):
        super().__init__(hmac_key)
        self._client = client
        self._url = url
        self._token = token
        self._alock = asyncio.Lock()

    async def open(self) -> None:
        if self._client is None:
            import libsql_client
            self._client = libsql_client.create_client(self._url, auth_token=self._token)
        await self._migrate()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                log.exception("closing turso client")

    async def _exec(self, sql: str, args: list | None = None):
        try:
            return await asyncio.wait_for(self._client.execute(sql, args or []), QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            raise StorageError("database request timed out")
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"database error: {e}")

    async def _batch(self, stmts: list[tuple[str, list]]):
        try:
            return await asyncio.wait_for(self._client.batch(stmts), QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            raise StorageError("database request timed out")
        except Exception as e:
            raise StorageError(f"database error: {e}")

    @staticmethod
    def _dicts(rs, cols=None) -> list[dict]:
        names = list(cols or rs.columns)
        return [dict(zip(names, list(r))) for r in rs.rows]

    async def _migrate(self) -> None:
        await self._exec("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        rs = await self._exec("SELECT version FROM schema_version LIMIT 1")
        cur = rs.rows[0][0] if rs.rows else 0
        if cur > LATEST:
            raise StorageError(f"database is schema v{cur}, this build only understands v{LATEST}")
        for ver, build in MIGRATIONS:
            if ver <= cur:
                continue
            stmts = [(s, []) for s in build()]
            stmts.append(("DELETE FROM schema_version", []))
            stmts.append(("INSERT INTO schema_version (version) VALUES (?)", [ver]))
            await self._batch(stmts)
            log.info("database migrated to schema v%s", ver)

    async def health(self) -> dict:
        try:
            rs = await self._exec("SELECT version FROM schema_version LIMIT 1")
            return {"mode": "turso", "ok": True, "schema_version": rs.rows[0][0] if rs.rows else 0}
        except StorageError as e:
            return {"mode": "turso", "ok": False, "error": str(e)}

    async def guild_ids(self) -> list[int]:
        found = set()
        for c in ("settings", "tasks", "events"):
            rs = await self._exec(f"SELECT DISTINCT guild_id FROM {check_collection(c)}")
            found.update(int(r[0]) for r in rs.rows)
        rs = await self._exec("SELECT DISTINCT guild_id FROM audit")
        found.update(int(r[0]) for r in rs.rows)
        found.discard(0)
        return sorted(found)

    def _row(self, d: dict) -> dict:
        d["data"] = json.loads(d["data"])
        return d

    async def put(self, coll: str, rec: dict) -> None:
        c = check_collection(coll)
        for k in ("id", "guild_id", "ts", "data"):
            if k not in rec:
                raise StorageError(f"record missing '{k}'")
        vals = [rec["id"], int(rec["guild_id"]), rec["ts"]] + [rec.get(k) for k in INDEX_FIELDS if k != "guild_id"]
        vals.append(json.dumps(rec["data"], separators=(",", ":")))
        sets = ", ".join(f"{k}=excluded.{k}" for k in SELECT_COLS if k not in ("id", "guild_id"))
        await self._exec(
            f"INSERT INTO {c} (id, guild_id, ts, actor_id, target_id, kind, status, ref_id, run_at, expires_at, data) "
            f"VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET {sets} WHERE {c}.guild_id = excluded.guild_id",
            vals,
        )

    async def get(self, coll, id, guild_id=None):
        c = check_collection(coll)
        sql, args = f"SELECT {', '.join(SELECT_COLS)} FROM {c} WHERE id=?", [id]
        if guild_id is not None:
            sql += " AND guild_id=?"
            args.append(int(guild_id))
        rows = self._dicts(await self._exec(sql, args), SELECT_COLS)
        return self._row(rows[0]) if rows else None

    async def delete(self, coll, id, guild_id=None):
        c = check_collection(coll)
        sql, args = f"DELETE FROM {c} WHERE id=?", [id]
        if guild_id is not None:
            sql += " AND guild_id=?"
            args.append(int(guild_id))
        return (await self._exec(sql, args)).rows_affected > 0

    @staticmethod
    def _where(guild_id, where, since, until, due_before, field_before=None):
        parts, args = [], []
        if guild_id is not None:
            parts.append("guild_id=?")
            args.append(int(guild_id))
        for k, v in (where or {}).items():
            if k not in INDEX_FIELDS:
                raise StorageError(f"can't filter on '{k}'")
            if isinstance(v, (list, tuple, set)):
                v = list(v)
                if not v:
                    parts.append("0")
                    continue
                parts.append(f"{k} IN ({','.join('?' * len(v))})")
                args.extend(v)
            elif v is None:
                parts.append(f"{k} IS NULL")
            else:
                parts.append(f"{k}=?")
                args.append(v)
        if since is not None:
            parts.append("ts>=?")
            args.append(since)
        if until is not None:
            parts.append("ts<?")
            args.append(until)
        if due_before is not None:
            parts.append("run_at IS NOT NULL AND run_at<=?")
            args.append(due_before)
        return (" WHERE " + " AND ".join(parts)) if parts else "", args

    async def find(self, coll, *, guild_id=None, where=None, since=None, until=None, due_before=None,
                   order="ts", desc=False, limit=100, offset=0):
        c = check_collection(coll)
        if order not in ORDER_FIELDS:
            raise StorageError(f"can't order by '{order}'")
        w, args = self._where(guild_id, where, since, until, due_before)
        sql = (f"SELECT {', '.join(SELECT_COLS)} FROM {c}{w} ORDER BY {order} {'DESC' if desc else 'ASC'}, id "
               f"LIMIT ? OFFSET ?")
        rs = await self._exec(sql, args + [int(limit), int(offset)])
        return [self._row(d) for d in self._dicts(rs, SELECT_COLS)]

    async def count(self, coll, *, guild_id=None, where=None):
        c = check_collection(coll)
        w, args = self._where(guild_id, where, None, None, None)
        rs = await self._exec(f"SELECT COUNT(*) FROM {c}{w}", args)
        return int(rs.rows[0][0])

    async def update_if(self, coll, id, expect, patch, guild_id=None):
        c = check_collection(coll)
        sets, args = [], []
        for k, v in patch.items():
            if k == "data":
                sets.append("data=json_patch(data, ?)")
                args.append(json.dumps(v, separators=(",", ":")))
            elif k in INDEX_FIELDS or k == "ts":
                sets.append(f"{k}=?")
                args.append(v)
            else:
                raise StorageError(f"can't patch '{k}'")
        if not sets:
            return True
        conds, cargs = ["id=?"], [id]
        if guild_id is not None:
            conds.append("guild_id=?")
            cargs.append(int(guild_id))
        for k, v in expect.items():
            if k not in INDEX_FIELDS and k != "ts":
                raise StorageError(f"can't expect '{k}'")
            if v is None:
                conds.append(f"{k} IS NULL")
            else:
                conds.append(f"{k}=?")
                cargs.append(v)
        rs = await self._exec(f"UPDATE {c} SET {', '.join(sets)} WHERE {' AND '.join(conds)}", args + cargs)
        return rs.rows_affected == 1

    async def purge(self, coll, *, before, field="ts", guild_id=None, where=None):
        c = check_collection(coll)
        if field not in ORDER_FIELDS:
            raise StorageError(f"can't purge by '{field}'")
        w, args = self._where(guild_id, where, None, None, None)
        cond = f"{field} IS NOT NULL AND {field}<?"
        sql = f"DELETE FROM {c}{w + ' AND ' + cond if w else ' WHERE ' + cond}"
        return (await self._exec(sql, args + [before])).rows_affected

    async def append_audit(self, entry: dict) -> dict:
        gid = int(entry["guild_id"])
        async with self._alock:
            last_err = None
            for _ in range(3):
                rs = await self._exec("SELECT hash FROM audit WHERE guild_id=? ORDER BY seq DESC LIMIT 1", [gid])
                prev = rs.rows[0][0] if rs.rows else GENESIS
                row = seal(entry, prev, self.hmac_key)
                vals = [json.dumps(row[c], sort_keys=True, separators=(",", ":")) if c == "details" else row[c]
                        for c in AUDIT_COLS]
                try:
                    await self._exec(
                        f"INSERT INTO audit ({', '.join(AUDIT_COLS)}) VALUES ({','.join('?' * len(AUDIT_COLS))})", vals)
                    return row
                except StorageError as e:
                    last_err = e
                    if "UNIQUE" not in str(e).upper():
                        raise
            raise StorageError(f"audit append kept conflicting: {last_err}")

    def _audit_row(self, d: dict) -> dict:
        d["details"] = json.loads(d["details"])
        return d

    async def query_audit(self, *, guild_id, actor_id=None, target_id=None, action=None, status=None, since=None,
                          until=None, request_id=None, plan_id=None, task_id=None, resource_id=None, limit=50,
                          offset=0, desc=True):
        parts, args = ["guild_id=?"], [int(guild_id)]
        for k, v in (("actor_id", actor_id), ("target_id", target_id), ("action", action), ("status", status),
                     ("request_id", request_id), ("plan_id", plan_id), ("task_id", task_id), ("resource_id", resource_id)):
            if v is not None:
                parts.append(f"{k}=?")
                args.append(v)
        if since:
            parts.append("ts>=?")
            args.append(since)
        if until:
            parts.append("ts<?")
            args.append(until)
        sql = (f"SELECT seq, {', '.join(AUDIT_COLS)} FROM audit WHERE {' AND '.join(parts)} "
               f"ORDER BY seq {'DESC' if desc else 'ASC'} LIMIT ? OFFSET ?")
        rs = await self._exec(sql, args + [int(limit), int(offset)])
        return [self._audit_row(d) for d in self._dicts(rs)]

    async def audit_rows(self, guild_id: int) -> AsyncIterator[dict]:
        offset = 0
        while True:
            rows = await self.query_audit(guild_id=guild_id, limit=500, offset=offset, desc=False)
            if not rows:
                return
            for r in rows:
                yield r
            offset += len(rows)

    async def prune_audit(self, guild_id: int, before: str) -> int:
        async with self._alock:
            res = await self._batch([
                ("UPDATE audit_gate SET open=1", []),
                ("DELETE FROM audit WHERE guild_id=? AND ts<?", [int(guild_id), before]),
                ("UPDATE audit_gate SET open=0", []),
            ])
            return res[1].rows_affected

    async def import_audit(self, guild_id: int, rows: list[dict]) -> int:
        if not rows:
            return 0
        async with self._alock:
            rs = await self._exec("SELECT COUNT(*) FROM audit WHERE guild_id=?", [int(guild_id)])
            if rs.rows[0][0]:
                raise StorageError(f"destination already has audit entries for guild {guild_id}")
            if not verify_rows(rows, self.hmac_key)["ok"]:
                raise StorageError(f"source audit chain for guild {guild_id} doesn't verify; refusing to import it")
            sql = f"INSERT INTO audit ({', '.join(AUDIT_COLS)}) VALUES ({','.join('?' * len(AUDIT_COLS))})"
            for i in range(0, len(rows), 200):
                batch = []
                for r in rows[i:i + 200]:
                    vals = [json.dumps(r[c], sort_keys=True, separators=(",", ":")) if c == "details" else r[c] for c in AUDIT_COLS]
                    batch.append((sql, vals))
                await self._batch(batch)
            return len(rows)
