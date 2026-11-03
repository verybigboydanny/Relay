from __future__ import annotations

import asyncio
from typing import Literal

import discord
from pydantic import Field

from relay.tools.base import Args, Ctx, Item, Refs, Result, ref, tool
from relay.tools.util import ch_name
from relay.utils.errors import Denied, ExecError

PAUSE = 0.35  # gentle pacing so large clean-ups don't hammer Discord's rate limits


def _is_voice(c) -> bool:
    return isinstance(c, (discord.VoiceChannel, discord.StageChannel))


def _in_scope(c, scope: str) -> bool:
    if scope == "all":
        return True
    if scope == "categories":
        return isinstance(c, discord.CategoryChannel)
    if scope == "voice":
        return _is_voice(c)
    if scope == "text":
        return not _is_voice(c) and not isinstance(c, discord.CategoryChannel)
    return False


class DeleteChannels(Args):
    scope: Literal["all", "text", "voice", "categories"] = "all"
    keep: list[str] | None = ref("channel_or_category", None,
                                 "channels or categories to leave alone (keeping a category keeps everything in it)")


async def _guard_delete_channels(ctx: Ctx, a: DeleteChannels, r: Refs) -> None:
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_channels")
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_channels")


def _desc_delete_channels(a: DeleteChannels, r: Refs) -> str:
    what = {"all": "EVERY channel and category", "text": "every text channel", "voice": "every voice channel",
            "categories": "every category"}[a.scope]
    keep = r.label("keep")
    return f"DELETE {what} in the server" + (f", except {keep}" if keep else "")


@tool("delete_channels", "delete many channels at once: scope all/text/voice/categories, optionally keeping some. Use this for 'delete all channels', "
      "'wipe the server', 'remove everything except #general' instead of asking which ones", DeleteChannels,
      risk="high", perm="manage_channels", tags=("channels", "bulk"), guard=_guard_delete_channels, timeout=600,
      describe=_desc_delete_channels)
async def delete_channels(ctx: Ctx, a: DeleteChannels, r: Refs) -> Result:
    g = ctx.guild
    keep_ids = set(r.many("keep"))
    for cid in list(keep_ids):
        c = g.get_channel(cid)
        if isinstance(c, discord.CategoryChannel):
            keep_ids.update(k.id for k in c.channels)
    protected_cats = {c.category_id for c in g.channels if c.id in keep_ids and getattr(c, "category_id", None)}
    targets = [c for c in g.channels if c.id not in keep_ids and _in_scope(c, a.scope)
               and not (isinstance(c, discord.CategoryChannel) and c.id in protected_cats)]
    if not targets:
        return Result("There was nothing to delete.")
    here = ctx.channel_id
    # children before categories, and the channel the request came from last so I can still answer there
    targets.sort(key=lambda c: (isinstance(c, discord.CategoryChannel), c.id == here))
    items: list[Item] = []
    deleted: list[str] = []
    touched: list[int] = []
    gone_here = False
    for c in targets:
        label = ch_name(c)
        try:
            try:
                ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_channels", c)
            except Denied:
                items.append(Item(c.id, False, f"you can't manage {label}", "denied"))
                continue
            await c.delete(reason=ctx.reason("bulk channel delete"))
            deleted.append(label)
            touched.append(c.id)
            items.append(Item(c.id, True, label))
            gone_here = gone_here or c.id == here
        except discord.Forbidden:
            items.append(Item(c.id, False, f"Discord refused for {label}", "forbidden"))
        except discord.NotFound:
            items.append(Item(c.id, True, f"{label} was already gone"))
        except discord.HTTPException as e:
            items.append(Item(c.id, False, f"{label}: {str(e.text or e)[:80]}", "http"))
        await asyncio.sleep(PAUSE)
    bad = [i for i in items if not i.ok]
    text = f"Deleted {len(deleted)} channel{'s' if len(deleted) != 1 else ''}."
    if keep_ids:
        kept = [ch_name(g.get_channel(i)) for i in keep_ids if g.get_channel(i) and not isinstance(g.get_channel(i), discord.CategoryChannel)][:8]
        if kept:
            text += " Kept " + ", ".join(kept) + "."
    if bad:
        text += " Couldn't delete: " + "; ".join(i.text for i in bad[:6]) + ("…" if len(bad) > 6 else "") + "."
        if any(i.error_code == "http" for i in bad):
            text += " (Discord won't let the rules and community-updates channels go while Community is on.)"
    if gone_here and ctx.actor is not None:
        try:
            await ctx.actor.send(text)
        except discord.HTTPException:
            pass
    return Result(text, ok=not bad, items=items, touched=touched, details={"deleted": deleted[:100], "scope": a.scope},
                  event=("channel.delete", "staff"))


class DeleteRoles(Args):
    keep: list[str] | None = ref("role", None, "roles to leave alone")


async def _guard_delete_roles(ctx: Ctx, a: DeleteRoles, r: Refs) -> None:
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_roles")
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_roles")


@tool("delete_roles", "delete all custom roles at once (never @everyone, bot-managed roles, or roles above me), optionally keeping some",
      DeleteRoles, risk="high", perm="manage_roles", tags=("roles", "bulk"), guard=_guard_delete_roles, timeout=600,
      describe=lambda a, r: "DELETE every custom role" + (f", except {r.label('keep')}" if r.label("keep") else ""))
async def delete_roles(ctx: Ctx, a: DeleteRoles, r: Refs) -> Result:
    keep = set(r.many("keep"))
    roles = [x for x in ctx.guild.roles if not x.is_default() and not x.managed and x.id not in keep and x < ctx.me.top_role]
    if not roles:
        return Result("There were no roles I could delete.")
    items, done = [], []
    for role in sorted(roles, key=lambda x: x.position):
        try:
            ctx.app.auth.check_role(ctx.actor, role)
            await role.delete(reason=ctx.reason("bulk role delete"))
            done.append(role.name)
            items.append(Item(role.id, True, role.name))
        except Denied:
            items.append(Item(role.id, False, f"you can't manage {role.name}", "denied"))
        except discord.HTTPException:
            items.append(Item(role.id, False, f"Discord refused for {role.name}", "forbidden"))
        await asyncio.sleep(PAUSE)
    bad = [i for i in items if not i.ok]
    text = f"Deleted {len(done)} role{'s' if len(done) != 1 else ''}."
    if bad:
        text += " Couldn't: " + "; ".join(i.text for i in bad[:6]) + "."
    return Result(text, ok=not bad, items=items, touched=[i.target_id for i in items if i.ok], details={"deleted": done[:100]},
                  event=("role.delete", "staff"))


class ClearMessages(Args):
    channel: str = ref("channel", desc="channel to clear")
    count: int = Field(100, ge=1, le=1000, description="how many recent messages to remove")
    member: str | None = ref("member", None, "only remove this member's messages")


async def _guard_clear(ctx: Ctx, a: ClearMessages, r: Refs) -> None:
    ch = ctx.guild.get_channel(r.one("channel"))
    if ch is None or not hasattr(ch, "purge"):
        raise ExecError("not text", user_message="I can only clear messages in text channels.")
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, "manage_messages", ch)
    ctx.app.auth.check_bot_perm(ctx.guild, "manage_messages", ch)


@tool("clear_messages", "bulk-delete the most recent messages in a channel (optionally only one member's); skips pinned messages", ClearMessages,
      risk="high", perm="manage_messages", tags=("messages", "bulk"), guard=_guard_clear, timeout=600,
      describe=lambda a, r: f"DELETE the last {a.count} messages in {r.label('channel')}" + (f" from {r.label('member')}" if a.member else ""))
async def clear_messages(ctx: Ctx, a: ClearMessages, r: Refs) -> Result:
    ch = ctx.guild.get_channel(r.one("channel"))
    who = r.one("member")
    removed = await ch.purge(limit=a.count, check=lambda m: not m.pinned and (who is None or m.author.id == who),
                             reason=ctx.reason("bulk message delete"))
    return Result(f"Removed {len(removed)} message{'s' if len(removed) != 1 else ''} from {ch_name(ch)}.", resource_id=ch.id,
                  resource_type="channel", touched=[ch.id], details={"removed": len(removed)}, event=("message.bulk_delete", "staff"))
