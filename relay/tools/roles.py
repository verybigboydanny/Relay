from __future__ import annotations

import discord
from pydantic import Field

from relay.services import permission_service as ps
from relay.services.member_resolver import norm_name
from relay.tools.base import Args, Ctx, Refs, Result, Item, ref, tool
from relay.tools.util import parse_color
from relay.utils.errors import Denied, ExecError


def _perms_or_empty(names) -> set[str]:
    return ps.norm_perms(names) if names else set()


def _safe_pretty(names) -> str:
    try:
        return ps.pretty(sorted(ps.norm_perms(names)))
    except Exception:
        return ", ".join(names)


class CreateRole(Args):
    name: str = Field(min_length=1, max_length=100)
    color: str | None = None
    permissions: list[str] = Field(default_factory=list)
    hoist: bool = Field(False, description="true = list members of this role separately from online members")
    mentionable: bool = False
    give_to: list[str] | None = ref("member", None, "members to give the new role to (e.g. 'me')")
    reuse_existing: bool = False


async def _guard_create_role(ctx, a, r):
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_roles")
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_roles")
    ctx.app.auth.check_can_grant(ctx.actor, ps.norm_perms(a.permissions), ctx.settings)
    parse_color(a.color)


def _confirm_create_role(a, r, guild):
    try:
        p = ps.norm_perms(a.permissions)
    except Exception:
        return False
    return bool(p & ps.DANGEROUS)


@tool("create_role", "create a role with optional colour and permissions (e.g. manage_events)", CreateRole, risk="medium",
      perm="manage_roles", tags=("roles",), creates="role", guard=_guard_create_role, confirm_if=_confirm_create_role,
      describe=lambda a, r: f"create the {a.name} role" + (f" in {a.color}" if a.color else "") + (", shown separately from online members" if a.hoist else "") + (f" with {_safe_pretty(a.permissions)}" if a.permissions else "") + (f" and give it to {r.label('give_to')}" if a.give_to else ""))
async def create_role(ctx: Ctx, a: CreateRole, r: Refs) -> Result:
    for role in ctx.guild.roles:
        if norm_name(role.name) == norm_name(a.name):
            if a.reuse_existing:
                return Result(f"Role {role.name} already exists, using it.", resource_id=role.id, resource_type="role",
                              entities={"role": {"id": role.id, "name": role.name}})
            raise ExecError("exists", user_message=f"A role called {role.name} already exists. Reuse it or pick another name.")
    perms = discord.Permissions(ps.value_of(a.permissions))
    role = await ctx.guild.create_role(name=a.name, permissions=perms, colour=parse_color(a.color) or discord.Colour.default(),
                                       hoist=a.hoist, mentionable=a.mentionable, reason=ctx.reason("role created"))
    given = ps.pretty(ps.names_of(perms.value))
    shade = f", colour {a.color} ({role.colour})" if a.color and role.colour.value else ""
    sep = ", shown separately from online members" if a.hoist else ""
    gave, failed = [], []
    for mid in r.many("give_to"):
        m = await ctx.member(mid)
        try:
            if m is None:
                raise ExecError("gone")
            await m.add_roles(role, reason=ctx.reason("role assigned on creation"))
            gave.append(m.display_name)
        except (discord.HTTPException, ExecError):
            failed.append(str(mid))
    tail = (f" Gave it to {', '.join(gave)}." if gave else "") + (f" Couldn't give it to {len(failed)} member(s)." if failed else "")
    extra = f" ({given})" if given else ""
    return Result(f"Created the {role.name} role{shade}{sep}{extra}.{tail}", resource_id=role.id, resource_type="role",
                  touched=[role.id] + r.many("give_to"),
                  entities={"role": {"id": role.id, "name": role.name}}, details={"name": role.name, "permissions": ps.names_of(perms.value), "hoist": a.hoist, "given_to": gave},
                  event=("role.create", "staff"))


class EditRole(Args):
    role: str = ref("role")
    name: str | None = Field(None, min_length=1, max_length=100)
    color: str | None = None
    add_permissions: list[str] = Field(default_factory=list)
    remove_permissions: list[str] = Field(default_factory=list)
    hoist: bool | None = None
    mentionable: bool | None = None
    position: int | None = Field(None, ge=1, le=250)


def _role_or_fail(ctx, rid) -> discord.Role:
    role = ctx.guild.get_role(rid)
    if role is None:
        raise ExecError("role gone", user_message="That role doesn't exist (any more).")
    return role


async def _guard_edit_role(ctx, a, r):
    role = _role_or_fail(ctx, r.one("role"))
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_roles")
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_roles")
    ctx.app.auth.check_role(ctx.actor, role)
    ctx.app.auth.check_can_grant(ctx.actor, ps.norm_perms(a.add_permissions), ctx.settings)
    if a.position is not None and not ctx.is_admin() and ctx.actor.id != ctx.guild.owner_id and a.position >= ctx.actor.top_role.position:
        raise Denied("position", user_message="You can't move a role to or above your own highest role.")
    if a.position is not None and a.position >= ctx.me.top_role.position:
        raise Denied("bot position", user_message="I can't move a role to or above my own highest role.")
    parse_color(a.color)


def _confirm_edit_role(a, r, guild):
    try:
        add = ps.norm_perms(a.add_permissions)
    except Exception:
        return False
    return bool(add & ps.DANGEROUS)


@tool("edit_role", "rename a role, change colour/hoist/mentionable/position, or add/remove permissions", EditRole, risk="medium",
      perm="manage_roles", tags=("roles",), guard=_guard_edit_role, confirm_if=_confirm_edit_role,
      describe=lambda a, r: f"edit role {r.label('role')}" + (f": add {', '.join(a.add_permissions)}" if a.add_permissions else "") + (f", remove {', '.join(a.remove_permissions)}" if a.remove_permissions else ""))
async def edit_role(ctx: Ctx, a: EditRole, r: Refs) -> Result:
    role = _role_or_fail(ctx, r.one("role"))
    before = {"name": role.name, "colour": role.colour.value, "permissions": role.permissions.value, "hoist": role.hoist,
              "mentionable": role.mentionable, "position": role.position}
    kw, notes = {}, []
    if a.name and a.name != role.name:
        kw["name"] = a.name
        notes.append(f"renamed to {a.name}")
    if a.color:
        kw["colour"] = parse_color(a.color)
        notes.append(f"colour {a.color}")
    if a.add_permissions or a.remove_permissions:
        val = role.permissions.value
        add, rem = ps.norm_perms(a.add_permissions), ps.norm_perms(a.remove_permissions)
        for n in add:
            val |= ps.FLAGS[n]
        for n in rem:
            val &= ~ps.FLAGS[n]
        if val != role.permissions.value:
            kw["permissions"] = discord.Permissions(val)
            notes.append("permissions " + "; ".join(([f"added {ps.pretty(sorted(add))}"] if add else []) + ([f"removed {ps.pretty(sorted(rem))}"] if rem else [])))
    if a.hoist is not None and a.hoist != role.hoist:
        kw["hoist"] = a.hoist
        notes.append("shown separately" if a.hoist else "no longer shown separately")
    if a.mentionable is not None and a.mentionable != role.mentionable:
        kw["mentionable"] = a.mentionable
        notes.append("mentionable" if a.mentionable else "not mentionable")
    if a.position is not None and a.position != role.position:
        kw["position"] = a.position
        notes.append(f"moved to position {a.position}")
    if not kw:
        return Result(f"{role.name} already matches that.", resource_id=role.id, resource_type="role")
    sid = await ctx.app.memory.save_snapshot(ctx.guild.id, "role_edit", ctx.actor_id, role.id, before, ctx.plan_id)
    await role.edit(reason=ctx.reason("role edited"), **kw)
    return Result(f"Updated role {before['name']}: {'; '.join(notes)}.", resource_id=role.id, resource_type="role", touched=[role.id],
                  undo_id=sid, entities={"role": {"id": role.id, "name": kw.get("name", role.name)}}, details={"changes": notes, "undo": sid},
                  event=("role.update", "staff"))


class DeleteRole(Args):
    role: str = ref("role")


async def _guard_delete_role(ctx, a, r):
    role = _role_or_fail(ctx, r.one("role"))
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_roles")
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_roles")
    ctx.app.auth.check_role(ctx.actor, role)


async def _verify_role_deleted(ctx, a, r):
    await ctx.guild.fetch_roles()
    return ctx.guild.get_role(r.one("role")) is None


@tool("delete_role", "delete a role", DeleteRole, risk="high", perm="manage_roles", tags=("roles",), guard=_guard_delete_role,
      verify=_verify_role_deleted, describe=lambda a, r: f"DELETE role {r.label('role')}")
async def delete_role(ctx: Ctx, a: DeleteRole, r: Refs) -> Result:
    role = _role_or_fail(ctx, r.one("role"))
    info = {"name": role.name, "permissions": ps.names_of(role.permissions.value), "members": len(role.members)}
    await role.delete(reason=ctx.reason("role deleted"))
    return Result(f"Deleted role {role.name} ({info['members']} members had it).", resource_id=role.id, resource_type="role",
                  touched=[role.id], details=info, event=("role.delete", "staff"))


class RoleMembers(Args):
    members: list[str] = ref("member")
    role: str = ref("role")


async def _guard_role_members(ctx, a, r):
    role = _role_or_fail(ctx, r.one("role"))
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_roles")
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_roles")
    ctx.app.auth.check_role(ctx.actor, role)


def _confirm_assign(a, r, guild):
    role = guild.get_role(r.one("role")) if guild and isinstance(r.one("role"), int) else None
    if role is None:
        return False
    danger = sum(ps.FLAGS[n] for n in ps.DANGEROUS)
    return bool(role.permissions.value & danger)


async def _change_roles(ctx: Ctx, r: Refs, add: bool) -> Result:
    role = _role_or_fail(ctx, r.one("role"))
    items, done, last, skipped = [], [], None, []
    for mid in r.many("members"):
        m = await ctx.member(mid)
        if m is None:
            items.append(Item(mid, False, "isn't in the server", "not_found"))
            continue
        has = role in m.roles
        if has == add:
            note = f"{m.display_name} {'already has' if add else 'does not have'} {role.name}"
            items.append(Item(mid, True, note))
            skipped.append(note)
            continue
        try:
            sid = await ctx.app.memory.save_snapshot(ctx.guild.id, "member_roles", ctx.actor_id, mid, {"role_id": role.id, "had": has}, ctx.plan_id)
            if add:
                await m.add_roles(role, reason=ctx.reason("role assigned"))
            else:
                await m.remove_roles(role, reason=ctx.reason("role removed"))
            items.append(Item(mid, True, f"{m.display_name}"))
            done.append(m.display_name)
            last = sid
        except discord.Forbidden:
            items.append(Item(mid, False, f"Discord refused for {m.display_name}", "forbidden"))
    bad = [i for i in items if not i.ok]
    verb = "Gave" if add else "Removed"
    prep = "to" if add else "from"
    text = f"{verb} {role.name} {prep} {', '.join(done)}." if done else ("; ".join(skipped) + "." if skipped else "Nothing changed.")
    if done and skipped:
        text += " " + "; ".join(skipped) + "."
    if bad:
        text += " Couldn't: " + "; ".join(i.text for i in bad) + "."
    return Result(text, ok=not bad, items=items, resource_id=role.id, resource_type="role", touched=[role.id] + [i.target_id for i in items],
                  undo_id=last, entities={"role": {"id": role.id, "name": role.name}},
                  details={"role": role.name, "members": done, "undo": last}, event=("member.roles", "staff") if done else None)


@tool("assign_role", "give a role to one or more members", RoleMembers, risk="medium", perm="manage_roles", tags=("roles",),
      guard=_guard_role_members, confirm_if=_confirm_assign, idempotent=True,
      describe=lambda a, r: f"give {r.label('role')} to {r.label('members')}")
async def assign_role(ctx: Ctx, a: RoleMembers, r: Refs) -> Result:
    return await _change_roles(ctx, r, True)


@tool("remove_role", "take a role away from one or more members", RoleMembers, risk="medium", perm="manage_roles", tags=("roles",),
      guard=_guard_role_members, idempotent=True, describe=lambda a, r: f"remove {r.label('role')} from {r.label('members')}")
async def remove_role(ctx: Ctx, a: RoleMembers, r: Refs) -> Result:
    return await _change_roles(ctx, r, False)
