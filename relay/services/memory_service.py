from __future__ import annotations

import uuid
from datetime import timedelta

from pydantic import BaseModel, Field

from relay.config import Config
from relay.storage.base import Store, new_rec
from relay.utils.errors import RelayError
from relay.utils.redact import has_secret, scrub_text
from relay.utils.time_parser import iso, now

TERMINAL_TASK = ("done", "failed", "cancelled", "missed", "disabled", "uncertain")


class GuildSettings(BaseModel):
    roles: dict[str, list[int]] = Field(default_factory=dict)
    announcement_channel: int | None = None
    security_channel: int | None = None
    timezone: str | None = None


def new_id(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


class MemoryService:
    def __init__(self, store: Store, cfg: Config):
        self.store = store
        self.cfg = cfg

    async def settings(self, guild_id: int) -> GuildSettings:
        base = self.cfg.guilds.get(guild_id)
        merged = GuildSettings.model_validate(base.model_dump()) if base else GuildSettings()
        rec = await self.store.get("settings", str(guild_id), guild_id)
        if rec:
            d = rec["data"]
            roles = {**merged.roles, **{k: [int(x) for x in v] for k, v in d.get("roles", {}).items()}}
            merged.roles = roles
            for k in ("announcement_channel", "security_channel", "timezone"):
                if d.get(k) is not None:
                    setattr(merged, k, d[k])
        return merged

    async def update_settings(self, guild_id: int, patch: dict) -> None:
        rec = await self.store.get("settings", str(guild_id), guild_id)
        data = rec["data"] if rec else {}
        if "roles" in patch:
            data["roles"] = {**data.get("roles", {}), **{k: list(v) for k, v in patch["roles"].items()}}
        for k in ("announcement_channel", "security_channel", "timezone"):
            if k in patch:
                data[k] = patch[k]
        await self.store.put("settings", new_rec(str(guild_id), guild_id, data, ts=iso(now()), kind="settings"))

    async def timezone_for(self, guild_id: int, user_id: int | None = None) -> str:
        if user_id:
            for m in await self.list_memories(guild_id, "user", user_id):
                if m["data"].get("key") == "timezone":
                    return m["data"]["text"]
        s = await self.settings(guild_id)
        return s.timezone or self.cfg.timezone

    @staticmethod
    def _ctx_id(g, c, u) -> str:
        return f"{g}:{c}:{u}"

    async def context(self, guild_id: int, channel_id: int, user_id: int) -> dict:
        rec = await self.store.get("context", self._ctx_id(guild_id, channel_id, user_id), guild_id)
        if rec and rec["expires_at"] and rec["expires_at"] > iso(now()):
            return rec["data"]
        if rec:
            await self.store.delete("context", rec["id"], guild_id)
        return {"turns": [], "entities": {}}

    async def save_context(self, guild_id: int, channel_id: int, user_id: int, ctx: dict) -> None:
        ctx["turns"] = ctx["turns"][-self.cfg.context.max_turns * 2:]
        t = now()
        await self.store.put("context", new_rec(
            self._ctx_id(guild_id, channel_id, user_id), guild_id, ctx, ts=iso(t), actor_id=user_id, kind="context",
            expires_at=iso(t + timedelta(minutes=self.cfg.context.ttl_minutes))))

    async def add_turn(self, guild_id, channel_id, user_id, user_text: str, reply: str, entities: dict | None = None):
        ctx = await self.context(guild_id, channel_id, user_id)
        ctx["turns"] += [{"r": "user", "t": scrub_text(user_text, 300)}, {"r": "relay", "t": scrub_text(reply, 300)}]
        if entities:
            ctx["entities"].update(entities)
        await self.save_context(guild_id, channel_id, user_id, ctx)

    async def clear_context(self, guild_id, channel_id, user_id) -> None:
        await self.store.delete("context", self._ctx_id(guild_id, channel_id, user_id), guild_id)

    async def add_memory(self, guild_id: int, scope: str, owner_id: int | None, text: str, *, key: str | None = None,
                         kind: str = "preference", source: str = "user", created_by: int | None = None) -> dict:
        text = text.strip()
        if not text:
            raise RelayError("nothing to remember")
        if len(text) > self.cfg.memory.max_chars:
            raise RelayError("too long", user_message=f"That's too long to store (max {self.cfg.memory.max_chars} characters).")
        if has_secret(text):
            raise RelayError("sensitive", user_message="That looks like a credential or secret, so I won't store it.")
        owner = owner_id if scope == "user" else None
        existing = await self.list_memories(guild_id, scope, owner)
        if key:
            for m in existing:
                if m["data"].get("key") == key:
                    await self.store.update_if("memories", m["id"], {}, {"ts": iso(now()), "data": {"text": text, "source": source, "updated_by": created_by}}, guild_id)
                    return await self.store.get("memories", m["id"], guild_id)
        cap = self.cfg.memory.max_per_user if scope == "user" else self.cfg.memory.max_per_guild
        if len(existing) >= cap:
            raise RelayError("full", user_message=f"The memory limit ({cap}) is reached. Forget something first.")
        rec = new_rec(new_id(), guild_id, {"scope": scope, "key": key, "text": text, "source": source, "created_by": created_by},
                      ts=iso(now()), actor_id=owner, kind=kind, status=scope)
        await self.store.put("memories", rec)
        return rec

    async def list_memories(self, guild_id: int, scope: str | None = None, owner_id: int | None = None) -> list[dict]:
        where = {}
        if scope:
            where["status"] = scope
        if scope == "user" and owner_id is not None:
            where["actor_id"] = owner_id
        return await self.store.find("memories", guild_id=guild_id, where=where, limit=500)

    async def delete_memory(self, guild_id: int, ref: str, scope: str, owner_id: int | None) -> dict | None:
        for m in await self.list_memories(guild_id, scope, owner_id):
            if m["id"] == ref or m["id"].startswith(ref) and len(ref) >= 4 or m["data"].get("key") == ref:
                await self.store.delete("memories", m["id"], guild_id)
                return m
        return None

    async def record_event(self, guild_id: int, type: str, *, actor_id=None, target_id=None, resource_type=None,
                           resource_id=None, channel_id=None, visibility: str = "staff", details: dict | None = None,
                           reason: str | None = None, result: str = "success", source: str = "relay",
                           plan_id: str | None = None) -> str:
        from relay.utils.redact import redact
        eid = new_id(12)
        data = {"resource_type": resource_type, "resource_id": None if resource_id is None else str(resource_id),
                "channel_id": channel_id, "visibility": visibility, "details": redact(details or {}),
                "reason": scrub_text(reason, 200) if reason else None, "result": result, "source": source}
        await self.store.put("events", new_rec(eid, guild_id, data, ts=iso(now()), actor_id=actor_id,
                                               target_id=target_id, kind=type, ref_id=plan_id, status=result))
        return eid

    async def query_events(self, guild_id: int, *, since=None, until=None, kind=None, actor_id=None, target_id=None,
                           limit: int = 50) -> list[dict]:
        where = {k: v for k, v in (("kind", kind), ("actor_id", actor_id), ("target_id", target_id)) if v is not None}
        return await self.store.find("events", guild_id=guild_id, where=where, since=since, until=until, desc=True, limit=limit)

    async def add_warning(self, guild_id: int, target_id: int, actor_id: int, reason: str, plan_id: str | None) -> dict:
        rec = new_rec(new_id(), guild_id, {"reason": scrub_text(reason, 300), "plan_id": plan_id},
                      ts=iso(now()), actor_id=actor_id, target_id=target_id, kind="warning", status="active")
        await self.store.put("warnings", rec)
        return rec

    async def list_warnings(self, guild_id: int, target_id: int | None = None, *, active_only: bool = True,
                            since: str | None = None, limit: int = 200) -> list[dict]:
        where = {}
        if target_id is not None:
            where["target_id"] = target_id
        if active_only:
            where["status"] = "active"
        return await self.store.find("warnings", guild_id=guild_id, where=where, since=since, desc=True, limit=limit)

    async def remove_warning(self, guild_id: int, ref: str, by: int) -> dict | None:
        for w in await self.list_warnings(guild_id):
            if w["id"] == ref or w["id"].startswith(ref) and len(ref) >= 4:
                ok = await self.store.update_if("warnings", w["id"], {"status": "active"},
                                                {"status": "removed", "data": {"removed_by": by, "removed_at": iso(now())}}, guild_id)
                return w if ok else None
        return None

    async def save_snapshot(self, guild_id: int, kind: str, actor_id: int, resource_id, before: dict, plan_id=None) -> str:
        sid = new_id()
        await self.store.put("snapshots", new_rec(sid, guild_id, {"before": before, "resource_id": str(resource_id)},
                                                  ts=iso(now()), actor_id=actor_id, kind=kind, status="available",
                                                  ref_id=plan_id, target_id=int(resource_id) if str(resource_id).isdigit() else None))
        return sid

    async def get_snapshot(self, guild_id: int, ref: str) -> dict | None:
        if ref == "last":
            rows = await self.store.find("snapshots", guild_id=guild_id, where={"status": "available"}, desc=True, limit=1)
            return rows[0] if rows else None
        rec = await self.store.get("snapshots", ref, guild_id)
        if rec is None and len(ref) >= 4:
            for r in await self.store.find("snapshots", guild_id=guild_id, where={"status": "available"}, desc=True, limit=200):
                if r["id"].startswith(ref):
                    return r
        return rec

    async def purge_expired(self) -> dict:
        t = now()
        c = self.cfg.memory
        out = {
            "context": await self.store.purge("context", before=iso(t), field="expires_at"),
            "events": await self.store.purge("events", before=iso(t - timedelta(days=c.event_retention_days))),
            "snapshots": await self.store.purge("snapshots", before=iso(t - timedelta(days=c.snapshot_retention_days))),
            "confirmations": await self.store.purge(
                "confirmations", before=iso(t - timedelta(days=c.confirmation_retention_days)),
                where={"status": ["approved", "rejected", "expired", "executed", "cancelled"]}),
            "tasks": await self.store.purge("tasks", before=iso(t - timedelta(days=c.task_retention_days)),
                                            where={"status": list(TERMINAL_TASK)}),
        }
        return out
