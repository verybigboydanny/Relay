from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta
from typing import Awaitable, Callable

import discord
from pydantic import Field, field_validator

from relay.tools.base import Args, Ctx, Item, Refs, Result, ref, tool
from relay.tools.util import NO_MENTIONS, esc, ow_snapshot, require_actor_channel, require_bot_channel, require_view
from relay.utils import time_parser as tp
from relay.utils.errors import Denied, ExecError, RelayError

log = logging.getLogger(__name__)


async def fresh_member(ctx: Ctx, uid: int):
    try:
        return await ctx.guild.fetch_member(uid)
    except discord.NotFound:
        return None


async def _each(ctx: Ctx, ids: list[int], fn: Callable[[int], Awaitable[Item]]) -> list[Item]:
    out = []
    for i in ids:
        try:
            out.append(await fn(i))
        except Denied as e:
            out.append(Item(i, False, e.user_message, "denied"))
        except discord.Forbidden:
            out.append(Item(i, False, "Discord refused (permissions or role hierarchy)", "forbidden"))
        except discord.NotFound:
            out.append(Item(i, False, "not found", "not_found"))
    return out


def _wrap(verb: str, items: list[Item], tail: str = "", reason: str | None = None, event: str | None = None) -> Result:
    ok = [i for i in items if i.ok]
    bad = [i for i in items if not i.ok]
    parts = []
    if ok:
        parts.append(f"{verb} " + ", ".join(i.text for i in ok) + (tail or "") + (f" ({esc(reason, 100)})" if reason else "") + ".")
    if bad:
        parts.append("Couldn't: " + "; ".join(f"<@{i.target_id}> {i.text}" for i in bad) + ".")
    return Result(" ".join(parts), ok=not bad, items=items, resource_type="member", touched=[i.target_id for i in items if i.target_id],
                  details={"reason": reason, "ok": [i.target_id for i in ok], "failed": [i.target_id for i in bad]},
                  event=(event, "staff") if ok and event else None,
                  entities={"member": {"id": items[-1].target_id, "name": items[-1].text}} if items and items[-1].ok and items[-1].target_id else {})


async def _guard_members(ctx: Ctx, r: Refs, field: str, perm: str, bot_acts: bool = True) -> None:
    if bot_acts:
        ctx.app.auth.check_bot_perm(ctx.guild, perm)
    for uid in r.many(field):
        m = await ctx.member(uid)
        if m is not None:
            ctx.app.auth.check_member(ctx.actor, m, bot_acts=bot_acts)


class Reasoned(Args):
    members: list[str] = ref("member")
    reason: str | None = Field(None, max_length=300)


class WarnMember(Args):
    members: list[str] = ref("member")
    reason: str = Field(min_length=1, max_length=300)


async def _guard_warn(ctx, a, r):
    for uid in r.many("members"):
        m = await ctx.member(uid)
        if m is not None:
            ctx.app.auth.check_member(ctx.actor, m, bot_acts=False)


@tool("warn_member", "record a warning for one or more members (stored in their moderation history)", WarnMember, risk="low",
      perm="moderate_members", tags=("moderation",), guard=_guard_warn,
      describe=lambda a, r: f"warn {r.label('members')} ({a.reason})")
async def warn_member(ctx: Ctx, a: WarnMember, r: Refs) -> Result:
    cfg = ctx.app.cfg.moderation
    extra = []

    async def one(uid: int) -> Item:
        m = await ctx.member(uid)
        if m is None:
            return Item(uid, False, "isn't in the server", "not_found")
        ctx.app.auth.check_member(ctx.actor, m, bot_acts=False)
        await ctx.app.memory.add_warning(ctx.guild.id, uid, ctx.actor_id, a.reason, ctx.plan_id)
        n = len(await ctx.app.memory.list_warnings(ctx.guild.id, uid))
        if cfg.dm_on_warn:
            try:
                await m.send(f"You received a warning in {ctx.guild.name}: {a.reason}")
            except (discord.Forbidden, discord.HTTPException):
                pass
        note = await _escalation(ctx, m, n)
        if note:
            extra.append(note)
        return Item(uid, True, f"<@{uid}> ({n} active)")

    items = await _each(ctx, r.many("members"), one)
    res = _wrap("Warned", items, reason=a.reason, event="moderation.warn")
    if extra:
        res.text += " " + " ".join(extra)
    return res


async def _escalation(ctx: Ctx, m: discord.Member, count: int) -> str | None:
    for e in ctx.app.cfg.moderation.escalation:
        if e.at != count:
            continue
        label = f"{e.action} ({e.duration})" if e.action == "timeout" else e.action
        if not e.auto:
            return f"{m.display_name} is now at {count} warnings; policy suggests {label}."
        tool_ = ctx.app.registry.get("timeout_member" if e.action == "timeout" else "kick_member")
        if not ctx.app.auth.decide(tool_, ctx.actor, ctx.settings).ok:
            return f"{m.display_name} is now at {count} warnings; policy calls for {label}, but you can't do that."
        try:
            ctx.app.auth.check_member(ctx.actor, m)
            reason = f"automatic escalation at {count} warnings"
            if e.action == "timeout":
                await m.timeout(tp.parse_duration(e.duration), reason=ctx.reason(reason))
            else:
                await ctx.guild.kick(m, reason=ctx.reason(reason))
            await ctx.app.audit.record(guild_id=ctx.guild.id, action="moderation.escalation", actor_id=ctx.actor_id,
                                       actor_type="policy", target_id=m.id, plan_id=ctx.plan_id, authorization_result="allowed",
                                       details={"policy": f"{e.action} at {e.at}", "warnings": count})
            return f"Policy escalation: {label} applied to {m.display_name}."
        except (Denied, discord.HTTPException) as ex:
            return f"{m.display_name} reached {count} warnings; {label} could not be applied ({getattr(ex, 'user_message', 'Discord refused')})."
    return None


class TimeoutMember(Args):
    members: list[str] = ref("member")
    duration: str = Field(description="e.g. 10 minutes, 2h, 1 day")
    reason: str | None = Field(None, max_length=300)

    @field_validator("duration")
    @classmethod
    def _dur(cls, v):
        d = tp.parse_duration(v)
        if d > timedelta(days=28):
            raise ValueError("Discord timeouts max out at 28 days")
        return v


async def _guard_timeout(ctx, a, r):
    await _guard_members(ctx, r, "members", "moderate_members")


async def _verify_timeout(ctx, a, r):
    ms = [await fresh_member(ctx, i) for i in r.many("members")]
    return all(m is not None and m.timed_out_until is not None for m in ms)


@tool("timeout_member", "mute/timeout members for a duration (Discord native timeout, max 28 days)", TimeoutMember, risk="medium",
      perm="moderate_members", tags=("moderation",), guard=_guard_timeout, verify=_verify_timeout, idempotent=True,
      describe=lambda a, r: f"timeout {r.label('members')} for {a.duration}" + (f" ({a.reason})" if a.reason else ""))
async def timeout_member(ctx: Ctx, a: TimeoutMember, r: Refs) -> Result:
    delta = tp.parse_duration(a.duration)
    if delta > timedelta(days=ctx.app.cfg.moderation.max_timeout_days):
        raise ExecError("too long", user_message=f"Timeouts are capped at {ctx.app.cfg.moderation.max_timeout_days} days.")

    async def one(uid: int) -> Item:
        m = await ctx.member(uid)
        if m is None:
            return Item(uid, False, "isn't in the server", "not_found")
        ctx.app.auth.check_member(ctx.actor, m)
        await m.timeout(delta, reason=ctx.reason(a.reason))
        return Item(uid, True, f"<@{uid}>")

    return _wrap("Timed out", await _each(ctx, r.many("members"), one), f" for {a.duration}", a.reason, "moderation.timeout")


async def _guard_untimeout(ctx, a, r):
    await _guard_members(ctx, r, "members", "moderate_members")


@tool("untimeout_member", "remove an active timeout (unmute) from members", Reasoned, risk="low", perm="moderate_members",
      tags=("moderation",), guard=_guard_untimeout, idempotent=True, describe=lambda a, r: f"remove the timeout from {r.label('members')}")
async def untimeout_member(ctx: Ctx, a: Reasoned, r: Refs) -> Result:
    async def one(uid: int) -> Item:
        m = await ctx.member(uid)
        if m is None:
            return Item(uid, False, "isn't in the server", "not_found")
        ctx.app.auth.check_member(ctx.actor, m)
        await m.timeout(None, reason=ctx.reason(a.reason))
        return Item(uid, True, f"<@{uid}>")

    return _wrap("Removed the timeout for", await _each(ctx, r.many("members"), one), "", a.reason, "moderation.untimeout")


async def _guard_kick(ctx, a, r):
    await _guard_members(ctx, r, "members", "kick_members")


async def _verify_kick(ctx, a, r):
    return all(await fresh_member(ctx, i) is None for i in r.many("members"))


def _confirm_bulk(a, r, guild):
    return len(r.many("members")) > 1


@tool("kick_member", "kick members from the server", Reasoned, risk="high", perm="kick_members", tags=("moderation",),
      guard=_guard_kick, verify=_verify_kick, confirm_if=_confirm_bulk,
      describe=lambda a, r: f"KICK {r.label('members')}" + (f" ({a.reason})" if a.reason else ""))
async def kick_member(ctx: Ctx, a: Reasoned, r: Refs) -> Result:
    async def one(uid: int) -> Item:
        m = await ctx.member(uid)
        if m is None:
            return Item(uid, False, "isn't in the server", "not_found")
        ctx.app.auth.check_member(ctx.actor, m)
        await ctx.guild.kick(m, reason=ctx.reason(a.reason))
        return Item(uid, True, f"<@{uid}>")

    return _wrap("Kicked", await _each(ctx, r.many("members"), one), "", a.reason, "moderation.kick")


class BanMember(Args):
    members: list[str] = ref("user")
    reason: str | None = Field(None, max_length=300)
    delete_message_seconds: int = Field(0, ge=0, le=604800)


async def _guard_ban(ctx, a, r):
    await _guard_members(ctx, r, "members", "ban_members")
    for uid in r.many("members"):
        if uid in (ctx.actor_id, ctx.me.id, ctx.guild.owner_id):
            raise Denied("bad ban target", user_message="I won't ban you, myself, or the server owner.")


async def _verify_ban(ctx, a, r):
    for i in r.many("members"):
        try:
            await ctx.guild.fetch_ban(discord.Object(i))
        except discord.NotFound:
            return False
    return True


@tool("ban_member", "ban members (or users by ID) from the server", BanMember, risk="high", perm="ban_members", tags=("moderation",),
      guard=_guard_ban, verify=_verify_ban, confirm_if=_confirm_bulk,
      describe=lambda a, r: f"BAN {r.label('members')}" + (f" ({a.reason})" if a.reason else ""))
async def ban_member(ctx: Ctx, a: BanMember, r: Refs) -> Result:
    async def one(uid: int) -> Item:
        m = await ctx.member(uid)
        if m is not None:
            ctx.app.auth.check_member(ctx.actor, m)
        await ctx.guild.ban(discord.Object(uid), reason=ctx.reason(a.reason), delete_message_seconds=a.delete_message_seconds)
        return Item(uid, True, f"<@{uid}>")

    return _wrap("Banned", await _each(ctx, r.many("members"), one), "", a.reason, "moderation.ban")


class UnbanUser(Args):
    users: list[str] = ref("banned")
    reason: str | None = Field(None, max_length=300)


async def _guard_unban(ctx, a, r):
    ctx.app.auth.check_bot_perm(ctx.guild, "ban_members")


@tool("unban_user", "unban users who are currently banned", UnbanUser, risk="medium", perm="ban_members", tags=("moderation",),
      guard=_guard_unban, idempotent=True, describe=lambda a, r: f"unban {r.label('users')}")
async def unban_user(ctx: Ctx, a: UnbanUser, r: Refs) -> Result:
    async def one(uid: int) -> Item:
        await ctx.guild.unban(discord.Object(uid), reason=ctx.reason(a.reason))
        return Item(uid, True, f"<@{uid}>")

    return _wrap("Unbanned", await _each(ctx, r.many("users"), one), "", a.reason, "moderation.unban")


class SetNickname(Args):
    member: str = ref("member")
    nickname: str | None = Field(None, max_length=32, description="null clears it")


async def _guard_nick(ctx, a, r):
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_nicknames")
    m = await ctx.member(r.one("member"))
    if m is not None:
        ctx.app.auth.check_member(ctx.actor, m, allow_self=True)


@tool("set_nickname", "change or clear a member's nickname", SetNickname, risk="low", perm="manage_nicknames", tags=("moderation",),
      guard=_guard_nick, describe=lambda a, r: f"set {r.label('member')}'s nickname to {a.nickname or '(none)'}")
async def set_nickname(ctx: Ctx, a: SetNickname, r: Refs) -> Result:
    m = await ctx.member(r.one("member"))
    if m is None:
        raise ExecError("gone", user_message="That member isn't in the server.")
    sid = await ctx.app.memory.save_snapshot(ctx.guild.id, "nickname", ctx.actor_id, m.id, {"nick": m.nick}, ctx.plan_id)
    old = m.display_name
    await m.edit(nick=a.nickname, reason=ctx.reason("nickname changed"))
    return Result(f"Changed {esc(old)}'s nickname to {esc(a.nickname)}." if a.nickname else f"Cleared {esc(old)}'s nickname.",
                  resource_id=m.id, resource_type="member", touched=[m.id], undo_id=sid, details={"nick": a.nickname, "undo": sid},
                  items=[Item(m.id, True, old)], entities={"member": {"id": m.id, "name": m.display_name}}, event=("member.nickname", "staff"))


class RemoveWarning(Args):
    warning: str = Field(min_length=4, max_length=40, description="warning id from member history")


@tool("remove_warning", "remove (void) a recorded warning by id", RemoveWarning, risk="low", perm="moderate_members",
      tags=("moderation",), describe=lambda a, r: f"remove warning {a.warning}")
async def remove_warning(ctx: Ctx, a: RemoveWarning, r: Refs) -> Result:
    w = await ctx.app.memory.remove_warning(ctx.guild.id, a.warning, ctx.actor_id)
    if not w:
        raise ExecError("none", user_message="I couldn't find an active warning with that id.")
    return Result(f"Removed warning {w['id']} for <@{w['target_id']}>.", resource_type="member", items=[Item(w["target_id"], True, "")],
                  details={"warning": w["id"]}, event=("moderation.warning_removed", "staff"))


class QueryWarnings(Args):
    member: str | None = ref("member", None)
    min_count: int = Field(1, ge=1, le=100)
    reason_contains: str | None = Field(None, max_length=60)
    period: str | None = Field(None, description="today|yesterday|last 7 days|this week|last week ...")


@tool("query_warnings", "list members with recorded warnings (filter by count, reason text, period, or one member)", QueryWarnings,
      risk="read", tags=("moderation",), describe=lambda a, r: "look up warnings")
async def query_warnings(ctx: Ctx, a: QueryWarnings, r: Refs) -> Result:
    since = until = None
    if a.period:
        s, u = tp.parse_period(a.period, tp.tz_of(ctx.tz_name))
        since, until = tp.iso(s), tp.iso(u)
    rows = await ctx.app.memory.list_warnings(ctx.guild.id, r.one("member"), since=since)
    if until:
        rows = [w for w in rows if w["ts"] < until]
    if a.reason_contains:
        rows = [w for w in rows if a.reason_contains.lower() in w["data"]["reason"].lower()]
    by: dict[int, list] = {}
    for w in rows:
        by.setdefault(w["target_id"], []).append(w)
    hits = sorted(((t, ws) for t, ws in by.items() if len(ws) >= a.min_count), key=lambda x: -len(x[1]))
    if not hits:
        return Result("No warnings match that.")
    lines = []
    for t, ws in hits[:25]:
        latest = ws[0]
        lines.append(f"<@{t}>: {len(ws)} warning{'s' if len(ws) != 1 else ''}; latest ({latest['id']}): {esc(latest['data']['reason'], 80)}")
    more = f"\n…and {len(hits) - 25} more." if len(hits) > 25 else ""
    return Result("\n".join(lines) + more, data={"lines": lines})


class ModSummary(Args):
    period: str = "last 7 days"
    channel: str | None = ref("channel", None, "post the summary here; omit to just show it")


async def _guard_summary(ctx, a, r):
    if r.one("channel"):
        ch = ctx.guild.get_channel(r.one("channel"))
        if ch is None:
            raise Denied("gone", user_message="That channel doesn't exist.")
        require_view(ctx, ch)
        require_actor_channel(ctx, ch, "send_messages")
        require_bot_channel(ctx, ch, "send_messages")


@tool("moderation_summary", "summarize moderation activity (warnings, timeouts, kicks, bans) over a period, optionally posting it in a channel",
      ModSummary, risk="low", tags=("moderation",), guard=_guard_summary,
      describe=lambda a, r: f"post a moderation summary ({a.period}) in {r.label('channel')}" if a.channel else f"summarize moderation ({a.period})")
async def moderation_summary(ctx: Ctx, a: ModSummary, r: Refs) -> Result:
    tz = tp.tz_of(ctx.tz_name)
    s, u = tp.parse_period(a.period, tz)
    si, ui = tp.iso(s), tp.iso(u)
    warns = [w for w in await ctx.app.memory.list_warnings(ctx.guild.id, active_only=False, since=si, limit=500) if w["ts"] < ui]
    counts = Counter()
    for act in ("timeout_member", "kick_member", "ban_member", "unban_user"):
        rows = await ctx.app.audit.search(ctx.guild.id, action=act, status="success", since=si, until=ui, limit=500)
        counts[act] = sum(len(x["details"].get("ok", [])) or 1 for x in rows)
    top = Counter(w["target_id"] for w in warns).most_common(3)
    lines = [f"Moderation summary, {tp.fmt_local(s, tz)} to {tp.fmt_local(u, tz)}:",
             f"Warnings: {len(warns)}. Timeouts: {counts['timeout_member']}. Kicks: {counts['kick_member']}. "
             f"Bans: {counts['ban_member']}. Unbans: {counts['unban_user']}."]
    if top:
        lines.append("Most warned: " + ", ".join(f"<@{t}> ({n})" for t, n in top) + ".")
    text = "\n".join(lines)
    if r.one("channel"):
        ch = ctx.guild.get_channel(r.one("channel"))
        await ch.send(text, allowed_mentions=NO_MENTIONS)
        return Result(f"Posted the moderation summary in #{ch.name}.", resource_id=ch.id, resource_type="channel", details={"period": a.period})
    return Result(text)
