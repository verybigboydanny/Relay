from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable

from relay.config import AuditCfg
from relay.storage.base import Store, verify_rows
from relay.utils.errors import AuditFailure, StorageError
from relay.utils.redact import redact
from relay.utils.time_parser import iso, now

log = logging.getLogger(__name__)

ALERT_STATUS = {"failure", "denied", "uncertain", "partial"}
ALERT_ACTIONS = {"ban_member", "kick_member", "delete_channel", "delete_role", "configure", "audit.prune"}


class AuditService:
    def __init__(self, store: Store, cfg: AuditCfg):
        self.store = store
        self.cfg = cfg
        self.notifier: Callable[[int, dict], Awaitable[None]] | None = None
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    async def record(self, *, guild_id: int, action: str, status: str = "success", strict: bool = False,
                     actor_id: int | None = None, actor_type: str = "user", target_id: int | None = None,
                     resource_type: str | None = None, resource_id=None, request_id: str | None = None,
                     plan_id: str | None = None, task_id: str | None = None,
                     authorization_result: str = "not_applicable", confirmation_status: str = "not_required",
                     reason: str | None = None, details: dict | None = None, error_code: str | None = None,
                     duration_ms: int | None = None) -> str | None:
        if not self.cfg.enabled:
            return None
        event_id = self.new_id()
        entry = {
            "event_id": event_id, "ts": iso(now()), "guild_id": int(guild_id), "actor_id": actor_id,
            "actor_type": actor_type, "action": action, "target_id": target_id, "resource_type": resource_type,
            "resource_id": None if resource_id is None else str(resource_id), "request_id": request_id,
            "plan_id": plan_id, "task_id": task_id, "authorization_result": authorization_result,
            "confirmation_status": confirmation_status, "status": status,
            "reason": redact(reason, 200) if reason else None, "details": redact(details or {}),
            "error_code": error_code, "duration_ms": duration_ms,
        }
        try:
            row = await self.store.append_audit(entry)
        except StorageError as e:
            log.error("audit write failed for %s: %s", action, e)
            if strict:
                raise AuditFailure(f"audit write failed: {e}", user_message="I couldn't write to the audit log, so I didn't do that.")
            return None
        if self.notifier and (status in ALERT_STATUS or action in ALERT_ACTIONS):
            t = asyncio.create_task(self._notify(guild_id, row))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return event_id

    async def _notify(self, guild_id: int, row: dict) -> None:
        try:
            await self.notifier(guild_id, row)
        except Exception:
            log.warning("security channel notification failed", exc_info=True)

    async def search(self, guild_id: int, **kw) -> list[dict]:
        kw["limit"] = max(1, min(int(kw.get("limit", 50)), 500))
        return await self.store.query_audit(guild_id=guild_id, **kw)

    async def export_jsonl(self, guild_id: int, since: str | None = None, until: str | None = None) -> tuple[bytes, int]:
        lines, n = [], 0
        async for r in self.store.audit_rows(guild_id):
            if since and r["ts"] < since or until and r["ts"] >= until:
                continue
            lines.append(json.dumps(r, sort_keys=True, separators=(",", ":")))
            n += 1
            if n >= self.cfg.export_max_rows:
                break
        return ("\n".join(lines) + ("\n" if lines else "")).encode(), n

    async def verify(self, guild_id: int) -> dict:
        rows = [r async for r in self.store.audit_rows(guild_id)]
        res = verify_rows(rows, self.store.hmac_key)
        res["keyed"] = bool(self.store.hmac_key)
        return res

    async def prune(self, guild_id: int, actor_id: int) -> int:
        if not self.cfg.retention_days:
            return 0
        from datetime import timedelta
        cutoff = iso(now() - timedelta(days=self.cfg.retention_days))
        n = await self.store.prune_audit(guild_id, cutoff)
        if n:
            await self.record(guild_id=guild_id, action="audit.prune", actor_id=actor_id, actor_type="relay",
                              details={"removed": n, "before": cutoff})
        return n
