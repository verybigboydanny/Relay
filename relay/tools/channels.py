from __future__ import annotations

from typing import Literal

import discord
from pydantic import Field

from relay.services import permission_service as ps
from relay.services.member_resolver import norm_channel, norm_name
from relay.tools.base import Args, Ctx, Refs, Result, ref, tool
from relay.tools.util import (
    ch_name, label_of, need_channel, ow_snapshot, principal, require_actor_channel, require_bot_channel, require_view,
)
from relay.utils.errors import Denied, ExecError


def _view_ow(allow_view: bool | None, send: bool | None = None) -> discord.PermissionOverwrite:
    ow = discord.PermissionOverwrite()
    ow.view_channel = allow_view
    if send is not None:
        ow.send_messages = send
    return ow


async def _private_overwrites(ctx: Ctx, role_ids: list[int], member_ids: list[int], text: bool) -> dict:
    ow = {ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for rid in role_ids:
        r = ctx.guild.get_role(rid)
        if r and not r.is_default():
            ow[r] = discord.PermissionOverwrite(view_channel=True, **({"send_messages": True} if text else {}))
    for mid in member_ids:
        m = await ctx.member(mid)
        if m:
            ow[m] = discord.PermissionOverwrite(view_channel=True, **({"send_messages": True} if text else {}))
    ow[ctx.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True) if text \
        else discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
    if ctx.actor and ctx.actor not in ow and not ctx.is_admin():
        have = {t.id for t in ow}
        if not ({r.id for r in ctx.actor.roles} & have):
            ow[ctx.actor] = discord.PermissionOverwrite(view_channel=True)
    return ow


class CreateCategory(Args):
    name: str = Field(min_length=1, max_length=100)
    private: bool = False
    allow_roles: list[str] | None = ref("role", None, "roles that can see it when private")
    reuse_existing: bool = True


async def _guard_create_cat(ctx, a, r):
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_channels")
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_channels")
    if a.private:
        ctx.app.auth.check_can_grant(ctx.actor, {"view_channel"}, ctx.settings)


@tool("create_category", "create a channel category (reuses an existing one of the same name by default)", CreateCategory,
      risk="low", perm="manage_channels", tags=("channels",), creates="category", guard=_guard_create_cat, idempotent=True,
      describe=lambda a, r: f"create category {a.name}" + (" (private)" if a.private else ""))
async def create_category(ctx: Ctx, a: CreateCategory, r: Refs) -> Result:
    for c in ctx.guild.categories:
        if norm_name(c.name) == norm_name(a.name):
            if a.reuse_existing:
                return Result(f"Category {c.name} already exists, using it.", resource_id=c.id, resource_type="channel",
                              entities={"category": {"id": c.id, "name": c.name}})
            raise ExecError("exists", user_message=f"A category called {c.name} already exists.")
    ow = await _private_overwrites(ctx, r.many("allow_roles"), [], False) if a.private else {}
    cat = await ctx.guild.create_category(a.name, overwrites=ow, reason=ctx.reason("category created"))
    return Result(f"Created category {cat.name}.", resource_id=cat.id, resource_type="channel", touched=[cat.id],
                  entities={"category": {"id": cat.id, "name": cat.name}}, details={"name": cat.name, "private": a.private},
                  event=("channel.create", "channel"))


class CreateChannel(Args):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["text", "voice"] = "text"
    category: str | None = ref("category", None, "existing category, or one made by an earlier step")
    topic: str | None = Field(None, max_length=1024)
    private: bool = False
    allow_roles: list[str] | None = ref("role", None, "roles that can see it when private")
    allow_members: list[str] | None = ref("member", None, "members that can see it when private")
    user_limit: int | None = Field(None, ge=0, le=99)


async def _guard_create_ch(ctx, a, r):
    auth = ctx.app.auth
    auth.check_bot_perm(ctx.guild, "manage_channels")
    auth.check_actor_perm(ctx.actor, ctx.settings, "manage_channels")
    if a.private:
        auth.check_can_grant(ctx.actor, {"view_channel"}, ctx.settings)


@tool("create_channel", "create a text or voice channel, optionally inside a category and/or private", CreateChannel,
      risk="low", perm="manage_channels", tags=("channels",), creates="channel", guard=_guard_create_ch,
      describe=lambda a, r: f"create {a.kind} channel {a.name}" + (f" under {r.label('category')}" if a.category else "") + (" (private)" if a.private else ""))
async def create_channel(ctx: Ctx, a: CreateChannel, r: Refs) -> Result:
    cat = ctx.guild.get_channel(r.one("category")) if r.one("category") else None
    if r.one("category") and not isinstance(cat, discord.CategoryChannel):
        raise ExecError("bad category", user_message="That category doesn't exist (any more).")
    wanted = norm_channel(a.name) if a.kind == "text" else a.name.strip().lower()
    pool = cat.channels if cat else [c for c in ctx.guild.channels if getattr(c, "category", None) is None and not isinstance(c, discord.CategoryChannel)]
    for c in pool:
        same_type = isinstance(c, discord.TextChannel) == (a.kind == "text")
        if same_type and (norm_channel(c.name) == wanted or c.name.lower() == wanted):
            where = f" under {cat.name}" if cat else ""
            raise ExecError("exists", user_message=f"#{c.name} already exists{where}. I can reuse it or you can pick another name.")
    ow = await _private_overwrites(ctx, r.many("allow_roles"), r.many("allow_members"), a.kind == "text") if a.private else None
    kw = {"category": cat, "reason": ctx.reason("channel created")}
    if ow is not None:
        kw["overwrites"] = ow
    if a.kind == "text":
        ch = await ctx.guild.create_text_channel(a.name, topic=a.topic, **kw)
    else:
        ch = await ctx.guild.create_voice_channel(a.name, user_limit=a.user_limit or 0, **kw)
    under = f" under {cat.name}" if cat else ""
    return Result(f"Created {ch_name(ch)}{under}." + (" It's private." if a.private else ""), resource_id=ch.id,
                  resource_type="channel", touched=[ch.id], entities={"channel": {"id": ch.id, "name": ch.name}},
                  details={"name": ch.name, "kind": a.kind, "category": cat.name if cat else None, "private": a.private},
                  event=("channel.create", "channel"))


class EditChannel(Args):
    channel: str = ref("channel_or_category")
    name: str | None = Field(None, min_length=1, max_length=100)
    topic: str | None = Field(None, max_length=1024)
    category: str | None = ref("category", None, "move under this category")
    no_category: bool = False
    position: int | None = Field(None, ge=0, le=500)
    slowmode_seconds: int | None = Field(None, ge=0, le=21600)
    nsfw: bool | None = None
    user_limit: int | None = Field(None, ge=0, le=99)


async def _guard_edit(ctx, a, r):
    ch = need_channel(ctx, r.one("channel"))
    require_view(ctx, ch)
    require_actor_channel(ctx, ch, "manage_channels")
    require_bot_channel(ctx, ch, "manage_channels")


@tool("edit_channel", "rename, move (category/position), or change topic/slowmode/nsfw/user limit of a channel or category", EditChannel,
      risk="low", perm="manage_channels", tags=("channels",), guard=_guard_edit,
      describe=lambda a, r: f"edit {r.label('channel')}" + (f" -> rename to {a.name}" if a.name else "") + (f", move under {r.label('category')}" if a.category else ""))
async def edit_channel(ctx: Ctx, a: EditChannel, r: Refs) -> Result:
    ch = need_channel(ctx, r.one("channel"))
    cat = ctx.guild.get_channel(r.one("category")) if r.one("category") else None
    is_cat = isinstance(ch, discord.CategoryChannel)
    kw, notes, before = {}, [], {"name": ch.name, "position": ch.position}
    if a.name and a.name != ch.name:
        sibs = ctx.guild.categories if is_cat else [c for c in ctx.guild.channels if not isinstance(c, discord.CategoryChannel)]
        if any(c.id != ch.id and norm_channel(c.name) == norm_channel(a.name) and getattr(c, "category_id", None) == getattr(ch, "category_id", None) for c in sibs):
            raise ExecError("exists", user_message=f"There's already a channel called {a.name} there.")
        kw["name"] = a.name
        notes.append(f"renamed to {a.name}")
    if a.topic is not None and isinstance(ch, discord.TextChannel):
        before["topic"] = ch.topic
        kw["topic"] = a.topic
        notes.append("topic updated")
    if (cat or a.no_category) and not is_cat:
        before["category_id"] = ch.category_id
        kw["category"] = cat
        notes.append(f"moved under {cat.name}" if cat else "moved out of its category")
    if a.position is not None:
        kw["position"] = a.position
        notes.append(f"position set to {a.position}")
    if a.slowmode_seconds is not None and isinstance(ch, discord.TextChannel):
        before["slowmode_delay"] = ch.slowmode_delay
        kw["slowmode_delay"] = a.slowmode_seconds
        notes.append(f"slowmode {a.slowmode_seconds}s")
    if a.nsfw is not None and isinstance(ch, discord.TextChannel):
        before["nsfw"] = ch.nsfw
        kw["nsfw"] = a.nsfw
        notes.append("nsfw on" if a.nsfw else "nsfw off")
    if a.user_limit is not None and isinstance(ch, discord.VoiceChannel):
        before["user_limit"] = ch.user_limit
        kw["user_limit"] = a.user_limit
        notes.append(f"user limit {a.user_limit}")
    if not kw:
        raise ExecError("nothing", user_message="There was nothing to change.")
    old_name = ch_name(ch)
    sid = await ctx.app.memory.save_snapshot(ctx.guild.id, "channel_edit", ctx.actor_id, ch.id, before, ctx.plan_id)
    await ch.edit(reason=ctx.reason("channel edited"), **kw)
    return Result(f"Updated {old_name}: {', '.join(notes)}.", resource_id=ch.id, resource_type="channel", touched=[ch.id], undo_id=sid,
                  entities={("category" if is_cat else "channel"): {"id": ch.id, "name": kw.get("name", ch.name)}},
                  details={"changes": notes, "undo": sid}, event=("channel.update", "channel"))


class DeleteChannel(Args):
    channel: str = ref("channel_or_category")
    delete_children: bool = False


async def _guard_delete(ctx, a, r):
    ch = need_channel(ctx, r.one("channel"))
    require_view(ctx, ch)
    require_actor_channel(ctx, ch, "manage_channels")
    require_bot_channel(ctx, ch, "manage_channels")


async def _verify_deleted(ctx, a, r):
    try:
        await ctx.guild.fetch_channel(r.one("channel"))
        return False
    except discord.NotFound:
        return True


@tool("delete_channel", "delete a channel or category (a category with channels needs delete_children)", DeleteChannel,
      risk="high", perm="manage_channels", tags=("channels",), guard=_guard_delete, verify=_verify_deleted,
      describe=lambda a, r: f"DELETE {r.label('channel')}" + (" and everything inside it" if a.delete_children else ""))
async def delete_channel(ctx: Ctx, a: DeleteChannel, r: Refs) -> Result:
    ch = need_channel(ctx, r.one("channel"))
    kids = list(ch.channels) if isinstance(ch, discord.CategoryChannel) else []
    if kids and not a.delete_children:
        raise ExecError("not empty", user_message=f"{ch.name} still has {len(kids)} channels. Say so if you want those deleted too.")
    for k in kids:
        await k.delete(reason=ctx.reason("deleted with category"))
    info = {"name": ch.name, "type": str(ch.type), "category": getattr(getattr(ch, "category", None), "name", None),
            "overwrites": ow_snapshot(ch), "children": [k.name for k in kids]}
    await ch.delete(reason=ctx.reason("channel deleted"))
    extra = f" and {len(kids)} channel{'s' if len(kids) != 1 else ''} inside it" if kids else ""
    return Result(f"Deleted {ch_name(ch)}{extra}.", resource_id=ch.id, resource_type="channel", touched=[ch.id] + [k.id for k in kids],
                  details=info, event=("channel.delete", "staff"))


class SetPrivacy(Args):
    channel: str = ref("channel_or_category")
    private: bool
    allow_roles: list[str] | None = ref("role", None, "roles that keep access when making private")
    allow_members: list[str] | None = ref("member", None, "members that keep access when making private")


async def _guard_privacy(ctx, a, r):
    ch = need_channel(ctx, r.one("channel"))
    require_view(ctx, ch)
    require_actor_channel(ctx, ch, "manage_roles")
    require_bot_channel(ctx, ch, "manage_roles")
    ctx.app.auth.check_can_grant(ctx.actor, {"view_channel"}, ctx.settings, ch)


@tool("set_channel_privacy", "make a channel or category private (hidden from @everyone, visible to listed roles/members) or public again", SetPrivacy,
      risk="medium", perm="manage_roles", tags=("channels", "permissions"), guard=_guard_privacy,
      describe=lambda a, r: (f"make {r.label('channel')} private" + (f" (visible to {r.label('allow_roles')}{', ' if a.allow_roles and a.allow_members else ''}{r.label('allow_members')})" if a.allow_roles or a.allow_members else " (only existing access)")) if a.private else f"make {r.label('channel')} visible to @everyone again")
async def set_privacy(ctx: Ctx, a: SetPrivacy, r: Refs) -> Result:
    ch = need_channel(ctx, r.one("channel"))
    old = ps.overwrites_map(ch)
    new = dict(old)
    ev = ctx.guild.default_role.id
    if a.private:
        new[ev] = ps.apply_change(old.get(ev, (0, 0)), deny={"view_channel"})
        for rid in r.many("allow_roles"):
            new[rid] = ps.apply_change(old.get(rid, (0, 0)), allow={"view_channel"})
        for mid in r.many("allow_members"):
            new[mid] = ps.apply_change(old.get(mid, (0, 0)), allow={"view_channel"})
        new[ctx.me.id] = ps.apply_change(old.get(ctx.me.id, (0, 0)), allow={"view_channel", "send_messages"} if not isinstance(ch, discord.VoiceChannel) else {"view_channel"})
        kept = bool(ps.lockouts(ctx.guild, new, old, [("you", ctx.actor)]))
        if kept:
            new[ctx.actor_id] = ps.apply_change(new.get(ctx.actor_id, (0, 0)), allow={"view_channel"})
    else:
        kept = False
        new[ev] = ps.apply_change(old.get(ev, (0, 0)), reset={"view_channel"})
    if new == old:
        return Result(f"{ch_name(ch)} already has that access.", resource_id=ch.id, resource_type="channel")
    sid = await ctx.app.memory.save_snapshot(ctx.guild.id, "channel_overwrites", ctx.actor_id, ch.id, ow_snapshot(ch), ctx.plan_id)
    targets = {}
    for tid in new:
        targets[await principal(ctx, tid)] = ps.pair_to_ow(*new[tid])
    await ch.edit(overwrites=targets, reason=ctx.reason("privacy changed"))
    state = "private" if a.private else "visible to @everyone again"
    who = [label_of(ctx, i) for i in r.many("allow_roles") + r.many("allow_members")]
    note = f" Access: {', '.join(who)}." if a.private and who else ""
    if kept:
        note += " I also kept your own access so you aren't locked out."
    return Result(f"{ch_name(ch)} is now {state}.{note}", resource_id=ch.id, resource_type="channel", touched=[ch.id], undo_id=sid,
                  entities={"channel": {"id": ch.id, "name": ch.name}},
                  details={"private": a.private, "changes": ps.diff_lines(old, new, lambda i: label_of(ctx, i)), "undo": sid},
                  event=("channel.permissions", "staff"))


class SetPermissions(Args):
    channel: str = ref("channel_or_category")
    targets: list[str] | None = ref("principal", None, "roles or members the change applies to")
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    reset: list[str] = Field(default_factory=list, description="return these permissions to inherit")
    copy_from: str | None = ref("channel_or_category", None, "replace all overwrites with another channel's")


def _perm_sets(a):
    return ps.norm_perms(a.allow), ps.norm_perms(a.deny), ps.norm_perms(a.reset)


async def _guard_setperm(ctx, a, r):
    ch = need_channel(ctx, r.one("channel"))
    require_view(ctx, ch)
    require_actor_channel(ctx, ch, "manage_roles")
    require_bot_channel(ctx, ch, "manage_roles")
    allow, _, _ = _perm_sets(a)
    ctx.app.auth.check_can_grant(ctx.actor, allow, ctx.settings, ch)
    if a.copy_from:
        src = need_channel(ctx, r.one("copy_from"), "source channel")
        require_view(ctx, src)
    elif not a.targets or not (a.allow or a.deny or a.reset):
        raise Denied("empty", user_message="Tell me who to change and which permissions to allow, deny, or reset.")


def _confirm_setperm(a, r, guild):
    try:
        allow = ps.norm_perms(a.allow)
    except Exception:
        return False
    return bool(allow & ps.DANGEROUS) or bool(a.copy_from)


def _desc_setperm(a, r):
    if a.copy_from:
        return f"copy all permissions from {r.label('copy_from')} onto {r.label('channel')}"
    bits = [f"allow {', '.join(a.allow)}" if a.allow else "", f"deny {', '.join(a.deny)}" if a.deny else "", f"reset {', '.join(a.reset)}" if a.reset else ""]
    return f"on {r.label('channel')} for {r.label('targets')}: " + "; ".join(b for b in bits if b)


@tool("set_channel_permissions", "allow/deny/reset specific permissions for roles or members on a channel, or copy another channel's permissions", SetPermissions,
      risk="medium", perm="manage_roles", tags=("channels", "permissions"), guard=_guard_setperm, confirm_if=_confirm_setperm,
      describe=_desc_setperm)
async def set_permissions(ctx: Ctx, a: SetPermissions, r: Refs) -> Result:
    ch = need_channel(ctx, r.one("channel"))
    old = ps.overwrites_map(ch)
    if a.copy_from:
        src = need_channel(ctx, r.one("copy_from"), "source channel")
        new = ps.overwrites_map(src)
        new[ctx.me.id] = ps.apply_change(new.get(ctx.me.id, old.get(ctx.me.id, (0, 0))), allow={"view_channel"})
    else:
        allow, deny, reset = _perm_sets(a)
        if allow & deny:
            raise ExecError("conflict", user_message="The same permission can't be both allowed and denied.")
        new = dict(old)
        for tid in r.many("targets"):
            new[tid] = ps.apply_change(old.get(tid, (0, 0)), allow, deny, reset)
            if new[tid] == (0, 0):
                new.pop(tid)
    if new == old:
        return Result(f"No change needed on {ch_name(ch)}.", resource_id=ch.id, resource_type="channel")
    lost = ps.lockouts(ctx.guild, new, old, [("you", ctx.actor)])
    if lost:
        raise ExecError("lockout", user_message="That would remove your own access to the channel, so I stopped. Include yourself or a role you have.")
    sid = await ctx.app.memory.save_snapshot(ctx.guild.id, "channel_overwrites", ctx.actor_id, ch.id, ow_snapshot(ch), ctx.plan_id)
    targets = {}
    for tid in new:
        targets[await principal(ctx, tid)] = ps.pair_to_ow(*new[tid])
    await ch.edit(overwrites=targets, reason=ctx.reason("permissions changed"))
    lines = ps.diff_lines(old, new, lambda i: label_of(ctx, i))
    return Result(f"Updated permissions on {ch_name(ch)}:\n" + "\n".join(lines[:8]), resource_id=ch.id, resource_type="channel", touched=[ch.id],
                  undo_id=sid, entities={"channel": {"id": ch.id, "name": ch.name}}, details={"changes": lines[:30], "undo": sid},
                  event=("channel.permissions", "staff"))
