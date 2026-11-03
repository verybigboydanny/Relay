from __future__ import annotations

from typing import Literal

import discord
from pydantic import Field

from relay.services import permission_service as ps
from relay.storage.base import new_rec
from relay.tools.base import Args, Ctx, Refs, Result, ref, tool
from relay.tools.util import ch_name, esc, label_of, need_channel, ow_snapshot, require_view
from relay.utils import time_parser as tp
from relay.utils.errors import Denied, ExecError


class InspectChannel(Args):
    channel: str = ref("channel_or_category")


@tool("inspect_channel", "show a channel's type, category, topic and permission overwrites", InspectChannel, risk="read",
      tags=("insight",), describe=lambda a, r: f"inspect {r.label('channel')}")
async def inspect_channel(ctx: Ctx, a: InspectChannel, r: Refs) -> Result:
    ch = need_channel(ctx, r.one("channel"))
    require_view(ctx, ch)
    lines = [f"{ch_name(ch)} ({str(ch.type).replace('_', ' ')})"]
    if getattr(ch, "category", None):
        lines.append(f"Category: {ch.category.name}")
    if getattr(ch, "topic", None):
        lines.append(f"Topic: {esc(ch.topic, 150)}")
    ow = ps.overwrites_map(ch)
    if ow:
        lines.append("Overwrites:")
        lines += [f"  {label_of(ctx, t)}: {ps.describe_pair(p)}" for t, p in ow.items()][:15]
    else:
        lines.append("No permission overwrites (inherits from the server).")
    return Result("\n".join(lines), entities={"channel": {"id": ch.id, "name": ch.name}})


class InspectRole(Args):
    role: str = ref("role")


@tool("inspect_role", "show a role's permissions, position and member count", InspectRole, risk="read", tags=("insight",),
      describe=lambda a, r: f"inspect {r.label('role')}")
async def inspect_role(ctx: Ctx, a: InspectRole, r: Refs) -> Result:
    role = ctx.role(r.one("role"))
    if role is None:
        raise ExecError("gone", user_message="That role doesn't exist.")
    perms = ps.names_of(role.permissions.value)
    text = (f"@{role.name}: position {role.position}, {len(role.members)} members, "
            f"{'shown separately, ' if role.hoist else ''}{'mentionable' if role.mentionable else 'not mentionable'}.\n"
            f"Permissions: {', '.join(perms) if perms else 'none beyond the defaults'}")
    return Result(text[:1900], entities={"role": {"id": role.id, "name": role.name}})


class InspectMember(Args):
    member: str = ref("member")


@tool("inspect_member", "show a member's roles, join date, timeout state and active warnings", InspectMember, risk="read",
      tags=("insight", "moderation"), describe=lambda a, r: f"inspect {r.label('member')}")
async def inspect_member(ctx: Ctx, a: InspectMember, r: Refs) -> Result:
    m = await ctx.member(r.one("member"))
    if m is None:
        raise ExecError("gone", user_message="That member isn't in the server.")
    warns = await ctx.app.memory.list_warnings(ctx.guild.id, m.id)
    tz = tp.tz_of(ctx.tz_name)
    roles = ", ".join(f"@{x.name}" for x in m.roles[1:][::-1]) or "none"
    lines = [f"{esc(m.display_name)} ({esc(m.name)}, id {m.id})",
             f"Joined: {tp.fmt_local(m.joined_at, tz) if m.joined_at else 'unknown'}. Roles: {roles}.",
             f"Timed out until: {tp.fmt_local(m.timed_out_until, tz)}." if m.timed_out_until else "Not timed out.",
             f"Active warnings: {len(warns)}."]
    for w in warns[:5]:
        lines.append(f"  {w['id']}: {esc(w['data']['reason'], 80)}")
    return Result("\n".join(lines), entities={"member": {"id": m.id, "name": m.display_name}})


class ExplainAccess(Args):
    member: str = ref("member")
    channel: str = ref("channel_or_category")


@tool("explain_access", "explain why a member can or can't see or post in a channel", ExplainAccess, risk="read", tags=("insight", "permissions"),
      describe=lambda a, r: f"explain {r.label('member')}'s access to {r.label('channel')}")
async def explain_access(ctx: Ctx, a: ExplainAccess, r: Refs) -> Result:
    ch = need_channel(ctx, r.one("channel"))
    m = await ctx.member(r.one("member"))
    if m is None:
        raise ExecError("gone", user_message="That member isn't in the server.")
    ow = ps.overwrites_map(ch)
    view, send = ps.FLAGS["view_channel"], ps.FLAGS["send_messages"]
    eff = ps.member_effective(m, ow)
    name = esc(m.display_name)
    if m.guild_permissions.administrator:
        why = f"{name} has Administrator, which overrides channel overwrites."
        return Result(why, entities={"member": {"id": m.id, "name": m.display_name}})
    reasons = []
    base = m.guild_permissions.value
    reasons.append(f"Server-wide, their roles {'grant' if base & view else 'do not grant'} View Channel.")
    ev = ow.get(ctx.guild.id)
    if ev and (ev[0] & view or ev[1] & view):
        reasons.append(f"@everyone is {'allowed' if ev[0] & view else 'denied'} View Channel on {ch_name(ch)}.")
    for role in m.roles[1:]:
        p = ow.get(role.id)
        if p and (p[0] & view or p[1] & view):
            reasons.append(f"Their role @{role.name} is {'allowed' if p[0] & view else 'denied'} View Channel here.")
    p = ow.get(m.id)
    if p and (p[0] & view or p[1] & view):
        reasons.append(f"They personally are {'allowed' if p[0] & view else 'denied'} View Channel here.")
    verdict = f"{name} {'can' if eff & view else 'cannot'} see {ch_name(ch)}"
    if eff & view and isinstance(ch, discord.TextChannel):
        verdict += f" and {'can' if eff & send else 'cannot'} send messages there"
    fix = "" if eff & view else " To fix it, allow View Channel for one of their roles or for them on that channel."
    return Result(verdict + ".\n" + "\n".join(reasons) + fix, entities={"member": {"id": m.id, "name": m.display_name}, "channel": {"id": ch.id, "name": ch.name}})


class CompareChannels(Args):
    first: str = ref("channel_or_category")
    second: str = ref("channel_or_category")


@tool("compare_channels", "show how two channels' permission overwrites differ", CompareChannels, risk="read", tags=("insight", "permissions"),
      describe=lambda a, r: f"compare {r.label('first')} with {r.label('second')}")
async def compare_channels(ctx: Ctx, a: CompareChannels, r: Refs) -> Result:
    c1, c2 = need_channel(ctx, r.one("first")), need_channel(ctx, r.one("second"))
    require_view(ctx, c1)
    require_view(ctx, c2)
    lines = ps.diff_lines(ps.overwrites_map(c1), ps.overwrites_map(c2), lambda i: label_of(ctx, i))
    if not lines:
        return Result(f"{ch_name(c1)} and {ch_name(c2)} have identical permission overwrites.")
    return Result(f"Differences ({ch_name(c1)} -> {ch_name(c2)}):\n" + "\n".join(lines[:20]))


class Overview(Args):
    pass


@tool("server_overview", "list the categories, channels and roles I can see", Overview, risk="read", tags=("insight",),
      describe=lambda a, r: "show the server layout")
async def server_overview(ctx: Ctx, a: Overview, r: Refs) -> Result:
    g = ctx.guild
    lines = []
    for cat in g.categories:
        kids = [c for c in cat.channels if ctx.is_admin() or c.permissions_for(ctx.actor).view_channel]
        if kids or ctx.is_admin():
            lines.append(f"{esc(cat.name)}: " + ", ".join(f"#{esc(c.name, 30)}" for c in kids))
    loose = [c for c in g.channels if not isinstance(c, discord.CategoryChannel) and c.category is None and (ctx.is_admin() or c.permissions_for(ctx.actor).view_channel)]
    if loose:
        lines.append("(no category): " + ", ".join(f"#{esc(c.name, 30)}" for c in loose))
    lines.append("Roles: " + ", ".join(f"@{esc(x.name, 30)}" for x in reversed(g.roles[1:])))
    return Result("\n".join(lines)[:1900], data={"lines": lines})


class QueryEvents(Args):
    period: str | None = Field(None, description="today|yesterday|last 24 hours|last 7 days|this week|last week")
    kind: str | None = Field(None, description="e.g. channel.create, channel.permissions, role.update, moderation.ban")
    member: str | None = ref("member", None)
    channel: str | None = ref("channel", None)
    limit: int = Field(25, ge=1, le=60)
    summarize: bool = True


def _visible(ctx: Ctx, ev: dict) -> bool:
    if ctx.is_admin():
        return True
    d = ev["data"]
    vis = d.get("visibility", "staff")
    staff = ctx.app.auth.is_staff(ctx.actor, ctx.settings)
    if vis == "public":
        return True
    if vis == "channel":
        cid = d.get("channel_id") or (int(d["resource_id"]) if d.get("resource_id") and d.get("resource_type") == "channel" else None)
        ch = ctx.guild.get_channel(cid) if cid else None
        if ch is None:
            return staff
        return ch.permissions_for(ctx.actor).view_channel
    return staff


@tool("query_events", "look up what happened in the server (channel/role/permission/moderation/config events I recorded or observed)", QueryEvents,
      risk="read", access="open", tags=("insight", "history"), describe=lambda a, r: "look up recorded events")
async def query_events(ctx: Ctx, a: QueryEvents, r: Refs) -> Result:
    since = until = None
    if a.period:
        s, u = tp.parse_period(a.period, tp.tz_of(ctx.tz_name))
        since, until = tp.iso(s), tp.iso(u)
    rows = await ctx.app.memory.query_events(ctx.guild.id, since=since, until=until, kind=a.kind, target_id=r.one("member"), limit=200)
    if r.one("channel"):
        cid = str(r.one("channel"))
        rows = [e for e in rows if e["data"].get("resource_id") == cid or str(e["data"].get("channel_id")) == cid]
    rows = [e for e in rows if _visible(ctx, e)][:a.limit]
    if not rows:
        return Result("I have no recorded events matching that" + (f" for {a.period}" if a.period else "") +
                      ". That only covers what I did or observed while I could see it.")
    tz = tp.tz_of(ctx.tz_name)
    lines = []
    for e in rows:
        d = e["data"]
        det = "; ".join(f"{k}={esc(v, 60)}" for k, v in (d.get("details") or {}).items() if k not in ("undo",))[:200]
        who = f"<@{e['actor_id']}>" if e["actor_id"] else "unknown actor"
        tgt = f" target <@{e['target_id']}>" if e["target_id"] else ""
        lines.append(f"{tp.fmt_local(tp.from_iso(e['ts']), tz)} {e['kind']} by {who}{tgt} ({d.get('source')}, {d.get('result')}) {det}")
    q = "What happened" + (f" ({a.period})" if a.period else "") + (f" - type {a.kind}" if a.kind else "")
    if a.summarize:
        return Result(f"{len(lines)} recorded events.", summarize=True, data={"records": lines, "question": q, "lines": lines})
    return Result("\n".join(lines)[:1900], data={"lines": lines})


async def _structure(ctx: Ctx) -> dict:
    g = ctx.guild
    return {
        "channels": {str(c.id): {"name": c.name, "type": str(c.type), "category": getattr(c, "category_id", None),
                                  "position": c.position, "overwrites": ow_snapshot(c)}
                     for c in g.channels},
        "roles": {str(x.id): {"name": x.name, "permissions": x.permissions.value, "position": x.position} for x in g.roles},
    }


class SnapshotArgs(Args):
    action: Literal["save", "compare"]
    name: str = Field("default", min_length=1, max_length=40)


async def _guard_snapshot(ctx, a, r):
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_guild")


@tool("server_snapshot", "save a named snapshot of the server's channels/roles/permissions, or compare the current state against a saved one",
      SnapshotArgs, risk="low", perm="manage_guild", tags=("insight",), guard=_guard_snapshot, schedulable=True,
      describe=lambda a, r: f"{a.action} server snapshot '{a.name}'")
async def server_snapshot(ctx: Ctx, a: SnapshotArgs, r: Refs) -> Result:
    cur = await _structure(ctx)
    if a.action == "save":
        await ctx.app.store.put("snapshots", new_rec(f"srv-{ctx.guild.id}-{a.name.lower()}", ctx.guild.id, cur, ts=tp.iso(tp.now()),
                                                     actor_id=ctx.actor_id, kind="server", status="saved"))
        return Result(f"Saved snapshot '{a.name}' ({len(cur['channels'])} channels, {len(cur['roles'])} roles).", details={"name": a.name})
    rec = await ctx.app.store.get("snapshots", f"srv-{ctx.guild.id}-{a.name.lower()}", ctx.guild.id)
    if not rec:
        raise ExecError("none", user_message=f"There's no snapshot called '{a.name}'.")
    old = rec["data"]
    out = []
    for kind in ("channels", "roles"):
        o, n = old[kind], cur[kind]
        out += [f"+ {kind[:-1]} {n[i]['name']}" for i in n.keys() - o.keys()]
        out += [f"- {kind[:-1]} {o[i]['name']}" for i in o.keys() - n.keys()]
        for i in o.keys() & n.keys():
            if o[i]["name"] != n[i]["name"]:
                out.append(f"~ {kind[:-1]} renamed {o[i]['name']} -> {n[i]['name']}")
            if kind == "roles" and o[i]["permissions"] != n[i]["permissions"]:
                out.append(f"~ role {n[i]['name']} permissions changed")
            if kind == "channels":
                if o[i]["overwrites"] != n[i]["overwrites"]:
                    out.append(f"~ channel {n[i]['name']} overwrites changed")
                if o[i]["category"] != n[i]["category"]:
                    out.append(f"~ channel {n[i]['name']} moved category")
    when = tp.fmt_local(tp.from_iso(rec["ts"]), tp.tz_of(ctx.tz_name))
    return Result(f"Changes since '{a.name}' ({when}):\n" + ("\n".join(out[:30]) if out else "none"), data={"lines": out})


class CheckServer(Args):
    pass


@tool("check_server", "scan the server for configuration problems and suggest improvements (read-only)", CheckServer, risk="read",
      tags=("insight",), describe=lambda a, r: "check the server configuration")
async def check_server(ctx: Ctx, a: CheckServer, r: Refs) -> Result:
    g, me = ctx.guild, ctx.me
    found = []
    ev = g.default_role.permissions
    bad = [n for n in ("administrator", "manage_guild", "manage_roles", "manage_channels", "mention_everyone", "manage_webhooks", "kick_members", "ban_members") if getattr(ev, n)]
    if bad:
        found.append(f"@everyone has powerful server-wide permissions ({', '.join(bad)}). Remove them.")
    admins = [x.name for x in g.roles if x.permissions.administrator and not x.managed and x != g.default_role]
    if len(admins) > 2:
        found.append(f"{len(admins)} roles have Administrator ({', '.join(admins[:5])}). Prefer narrower permissions.")
    s = ctx.settings
    for grp in ("admin", "moderator"):
        ids = s.roles.get(grp, [])
        if not ids:
            found.append(f"No role is mapped to my '{grp}' group; only server-level permissions apply. Use configure to map one.")
        elif not any(g.get_role(i) for i in ids):
            found.append(f"The roles mapped to the '{grp}' group no longer exist.")
    if s.announcement_channel and not g.get_channel(s.announcement_channel):
        found.append("The configured announcements channel was deleted.")
    missing = [n for n in ("manage_channels", "manage_roles", "moderate_members", "ban_members", "kick_members", "view_audit_log") if not getattr(me.guild_permissions, n)]
    if missing:
        found.append(f"I'm missing permissions for some features: {', '.join(missing)}.")
    empty = [c.name for c in g.categories if not c.channels]
    if empty:
        found.append(f"Empty categories: {', '.join(empty[:6])}.")
    hidden = [c.name for c in g.channels if not isinstance(c, discord.CategoryChannel) and not c.permissions_for(me).view_channel]
    if hidden:
        found.append(f"I can't see {len(hidden)} channels ({', '.join(hidden[:4])}); I can't manage or summarize those.")
    above = [x.name for x in g.roles if x > me.top_role and x.permissions.manage_roles]
    if above:
        found.append(f"Roles above mine can manage roles ({', '.join(above[:4])}); that's normal for admins but worth knowing.")
    return Result("Nothing concerning found." if not found else "Findings:\n" + "\n".join(f"- {f}" for f in found), data={"lines": found})
