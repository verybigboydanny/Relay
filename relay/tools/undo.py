from __future__ import annotations

import discord
from pydantic import Field

from relay.services import permission_service as ps
from relay.tools.base import Args, Ctx, Refs, Result, tool
from relay.tools.util import ow_snapshot, principal
from relay.utils.errors import ExecError

UNDOABLE = {"channel_overwrites", "channel_edit", "role_edit", "member_roles", "nickname"}


class Undo(Args):
    change: str = Field("last", max_length=40, description="'last' or an undo id from an earlier result")


@tool("undo_change", "revert my most recent undoable change (permissions, channel/role edits, role assignments, nicknames) or a specific one by id", Undo,
      risk="medium", tags=("undo",), schedulable=False, describe=lambda a, r: f"undo {'the last change' if a.change == 'last' else 'change ' + a.change}")
async def undo_change(ctx: Ctx, a: Undo, r: Refs) -> Result:
    mem, auth, g = ctx.app.memory, ctx.app.auth, ctx.guild
    snap = None
    if a.change == "last":
        rows = await ctx.app.store.find("snapshots", guild_id=g.id, where={"status": "available"}, desc=True, limit=50)
        for s in rows:
            if s["kind"] in UNDOABLE and (ctx.is_admin() or s["actor_id"] == ctx.actor_id):
                snap = s
                break
    else:
        snap = await mem.get_snapshot(g.id, a.change)
        if snap and (snap["status"] != "available" or snap["kind"] not in UNDOABLE or not (ctx.is_admin() or snap["actor_id"] == ctx.actor_id)):
            snap = None
    if snap is None:
        raise ExecError("none", user_message="I don't have an undoable change of yours to revert.")
    kind, before, rid = snap["kind"], snap["data"]["before"], int(snap["data"]["resource_id"])
    if not await ctx.app.store.update_if("snapshots", snap["id"], {"status": "available"}, {"status": "used"}, g.id):
        raise ExecError("race", user_message="That change was already undone.")
    try:
        if kind == "channel_overwrites":
            ch = g.get_channel(rid)
            if ch is None:
                raise ExecError("gone", user_message="That channel no longer exists.")
            auth.check_actor_perm(ctx.actor, ctx.settings, "manage_roles", ch)
            auth.check_bot_perm(g, "manage_roles", ch)
            await mem.save_snapshot(g.id, "channel_overwrites", ctx.actor_id, ch.id, ow_snapshot(ch), ctx.plan_id)
            targets, grant = {}, set()
            for tid, (_, al, dn) in before.items():
                try:
                    p = await principal(ctx, int(tid))
                except ExecError:
                    continue
                targets[p] = ps.pair_to_ow(al, dn)
                grant |= {n for n in ps.names_of(al)}
            auth.check_can_grant(ctx.actor, grant, ctx.settings, ch)
            await ch.edit(overwrites=targets, reason=ctx.reason("permissions restored"))
            text = f"Restored the previous permissions on #{ch.name}."
        elif kind == "channel_edit":
            ch = g.get_channel(rid)
            if ch is None:
                raise ExecError("gone", user_message="That channel no longer exists.")
            auth.check_actor_perm(ctx.actor, ctx.settings, "manage_channels", ch)
            auth.check_bot_perm(g, "manage_channels", ch)
            kw = {k: v for k, v in before.items() if k in ("name", "topic", "position", "slowmode_delay", "nsfw", "user_limit")}
            if "category_id" in before:
                kw["category"] = g.get_channel(before["category_id"]) if before["category_id"] else None
            await ch.edit(reason=ctx.reason("channel edit undone"), **kw)
            text = f"Restored #{kw.get('name', ch.name)} to how it was before the edit."
        elif kind == "role_edit":
            role = g.get_role(rid)
            if role is None:
                raise ExecError("gone", user_message="That role no longer exists.")
            auth.check_actor_perm(ctx.actor, ctx.settings, "manage_roles")
            auth.check_role(ctx.actor, role)
            added = ps.names_of(before["permissions"] & ~role.permissions.value)
            auth.check_can_grant(ctx.actor, set(added), ctx.settings)
            await role.edit(name=before["name"], colour=discord.Colour(before["colour"]), permissions=discord.Permissions(before["permissions"]),
                            hoist=before["hoist"], mentionable=before["mentionable"], reason=ctx.reason("role edit undone"))
            text = f"Restored role {before['name']} to its previous settings."
        elif kind == "member_roles":
            role = g.get_role(before["role_id"])
            m = await ctx.member(rid)
            if role is None or m is None:
                raise ExecError("gone", user_message="The role or member no longer exists.")
            auth.check_actor_perm(ctx.actor, ctx.settings, "manage_roles")
            auth.check_role(ctx.actor, role)
            if before["had"]:
                await m.add_roles(role, reason=ctx.reason("role removal undone"))
                text = f"Gave {role.name} back to {m.display_name}."
            else:
                await m.remove_roles(role, reason=ctx.reason("role assignment undone"))
                text = f"Took {role.name} away from {m.display_name} again."
        else:
            m = await ctx.member(rid)
            if m is None:
                raise ExecError("gone", user_message="That member isn't in the server.")
            auth.check_actor_perm(ctx.actor, ctx.settings, "manage_nicknames")
            auth.check_member(ctx.actor, m, allow_self=True)
            await m.edit(nick=before["nick"], reason=ctx.reason("nickname change undone"))
            text = f"Restored {m.display_name}'s previous nickname."
    except Exception:
        await ctx.app.store.update_if("snapshots", snap["id"], {"status": "used"}, {"status": "available"}, g.id)
        raise
    return Result(text, resource_id=rid, resource_type=kind, touched=[rid], details={"undone": snap["id"], "kind": kind},
                  event=("change.undo", "staff"))
