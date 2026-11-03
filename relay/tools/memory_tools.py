from __future__ import annotations

from typing import Literal

from pydantic import Field

from relay.tools.base import Args, Ctx, Refs, Result, ref, tool
from relay.tools.util import esc
from relay.utils import time_parser as tp
from relay.utils.errors import Denied, ExecError, RelayError


def _need_guild_scope(ctx: Ctx) -> None:
    if ctx.is_admin() or ctx.app.auth.is_staff(ctx.actor, ctx.settings) or ctx.actor.guild_permissions.manage_guild:
        return
    raise Denied("guild memory", user_message="Only moderators and administrators can change what I remember about the server.")


class Remember(Args):
    text: str = Field(min_length=1, max_length=400)
    scope: Literal["user", "guild"] = "user"
    key: str | None = Field(None, max_length=40, description="short label, e.g. timezone, reply_style")


async def _guard_remember(ctx, a, r):
    if a.scope == "guild":
        _need_guild_scope(ctx)
    if a.key == "timezone":
        tp.tz_of(a.text.strip())


@tool("remember", "store a preference or fact (scope user = just for the requester, guild = about the server)", Remember,
      risk="low", access="open", tags=("memory",), guard=_guard_remember, idempotent=True,
      describe=lambda a, r: f"remember ({a.scope}): {esc(a.text, 80)}")
async def remember(ctx: Ctx, a: Remember, r: Refs) -> Result:
    kind = "fact" if a.scope == "guild" else "preference"
    rec = await ctx.app.memory.add_memory(ctx.guild.id, a.scope, ctx.actor_id, a.text, key=a.key, kind=kind, created_by=ctx.actor_id)
    await ctx.app.audit.record(guild_id=ctx.guild.id, action="memory.save", actor_id=ctx.actor_id, request_id=ctx.request_id,
                               plan_id=ctx.plan_id, resource_type="memory", resource_id=rec["id"], authorization_result="allowed",
                               details={"scope": a.scope, "key": a.key, "length": len(a.text)})
    return Result(f"Noted ({a.scope}{', ' + a.key if a.key else ''}).", resource_type="memory", details={"id": rec["id"], "scope": a.scope})


class Forget(Args):
    memory: str = Field(min_length=1, max_length=60, description="memory id or key")
    scope: Literal["user", "guild"] = "user"


async def _guard_forget(ctx, a, r):
    if a.scope == "guild":
        _need_guild_scope(ctx)


@tool("forget", "delete a stored memory by id or key", Forget, risk="low", access="open", tags=("memory",), guard=_guard_forget,
      describe=lambda a, r: f"forget {a.memory} ({a.scope})")
async def forget(ctx: Ctx, a: Forget, r: Refs) -> Result:
    m = await ctx.app.memory.delete_memory(ctx.guild.id, a.memory, a.scope, ctx.actor_id)
    if not m:
        raise ExecError("none", user_message="I don't have a memory matching that.")
    await ctx.app.audit.record(guild_id=ctx.guild.id, action="memory.delete", actor_id=ctx.actor_id, request_id=ctx.request_id,
                               resource_type="memory", resource_id=m["id"], authorization_result="allowed",
                               details={"scope": a.scope, "key": m["data"].get("key")})
    return Result("Forgotten. The stored record is deleted.", resource_type="memory")


class Recall(Args):
    scope: Literal["all", "user", "guild"] = "all"


@tool("recall", "show what Relay remembers (user preferences, server notes, and live configuration facts)", Recall, risk="read",
      access="open", tags=("memory",), describe=lambda a, r: "show stored memories")
async def recall(ctx: Ctx, a: Recall, r: Refs) -> Result:
    lines = []
    if a.scope in ("all", "user"):
        for m in await ctx.app.memory.list_memories(ctx.guild.id, "user", ctx.actor_id):
            d = m["data"]
            lines.append(f"You [{m['id']}]{' ' + d['key'] if d.get('key') else ''}: {esc(d['text'], 160)}")
    if a.scope in ("all", "guild"):
        for m in await ctx.app.memory.list_memories(ctx.guild.id, "guild"):
            d = m["data"]
            lines.append(f"Server note [{m['id']}]{' ' + d['key'] if d.get('key') else ''}: {esc(d['text'], 160)} (stated by <@{d.get('created_by')}>)")
        s = ctx.settings
        facts = []
        for g, ids in s.roles.items():
            live = [ctx.guild.get_role(i) for i in ids]
            facts.append(f"{g} roles: " + (", ".join(f"@{x.name}" for x in live if x) or "none found"))
        for key, label in (("announcement_channel", "announcements"), ("security_channel", "security log")):
            cid = getattr(s, key)
            ch = ctx.guild.get_channel(cid) if cid else None
            facts.append(f"{label} channel: " + (f"#{ch.name}" if ch else "not set"))
        facts.append(f"timezone: {s.timezone or ctx.app.cfg.timezone}")
        lines += [f"Verified from current server state: {f}" for f in facts]
    return Result("\n".join(lines) if lines else "I don't have anything stored.", data={"lines": lines})


class Configure(Args):
    setting: Literal["announcement_channel", "security_channel", "timezone", "role_group"]
    channel: str | None = ref("channel", None)
    role: str | None = ref("role", None)
    group: str | None = Field(None, max_length=30, description="admin, moderator, trusted, restricted, or a custom group")
    action: Literal["add", "remove"] = "add"
    timezone: str | None = None


async def _guard_configure(ctx, a, r):
    if a.setting in ("announcement_channel", "security_channel") and not r.one("channel"):
        raise Denied("missing", user_message="Which channel?")
    if a.setting == "timezone":
        tp.tz_of(a.timezone or "")
    if a.setting == "role_group":
        if not (a.group and r.one("role")):
            raise Denied("missing", user_message="Which group and which role?")
        g = a.group.lower()
        if g not in ctx.app.cfg.policy.groups:
            raise Denied("unknown group", user_message=f"There's no permission group called {a.group}. Known groups: {', '.join(ctx.app.cfg.policy.groups)}.")
        if g == "admin" and not (ctx.app.auth.is_owner(ctx.actor) or ctx.actor.guild_permissions.administrator):
            raise Denied("admin group", user_message="Only the owner or someone with Administrator can change the admin group.")
        if r.one("role") == ctx.guild.default_role.id:
            raise Denied("everyone", user_message="@everyone can't be a permission group.")


def _desc_conf(a, r):
    if a.setting == "role_group":
        return f"{'add' if a.action == 'add' else 'remove'} {r.label('role')} {'to' if a.action == 'add' else 'from'} the {a.group} group"
    return f"set {a.setting.replace('_', ' ')} to {r.label('channel') or a.timezone}"


@tool("configure", "set announcements/security channels, server timezone, or which roles belong to a permission group", Configure,
      risk="high", access="admin", tags=("config",), guard=_guard_configure, schedulable=False, describe=_desc_conf,
      confirm_if=lambda a, r, g: a.setting == "role_group")
async def configure(ctx: Ctx, a: Configure, r: Refs) -> Result:
    mem = ctx.app.memory
    if a.setting in ("announcement_channel", "security_channel"):
        ch = ctx.guild.get_channel(r.one("channel"))
        await mem.update_settings(ctx.guild.id, {a.setting: ch.id})
        return Result(f"Set the {a.setting.replace('_', ' ')} to #{ch.name}.", details={a.setting: ch.id}, event=("config.update", "staff"))
    if a.setting == "timezone":
        await mem.update_settings(ctx.guild.id, {"timezone": a.timezone})
        return Result(f"Server timezone is now {a.timezone}.", details={"timezone": a.timezone}, event=("config.update", "staff"))
    g = a.group.lower()
    cur = list(ctx.settings.roles.get(g, []))
    rid = r.one("role")
    if a.action == "add" and rid not in cur:
        cur.append(rid)
    elif a.action == "remove":
        cur = [x for x in cur if x != rid]
    await mem.update_settings(ctx.guild.id, {"roles": {g: cur}})
    role = ctx.guild.get_role(rid)
    return Result(f"{'Added' if a.action == 'add' else 'Removed'} @{role.name} {'to' if a.action == 'add' else 'from'} the {g} group.",
                  details={"group": g, "role": rid, "action": a.action}, event=("config.update", "staff"))
