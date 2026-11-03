from __future__ import annotations

import re

import discord

from relay.tools.base import Ctx
from relay.utils.errors import Denied, ExecError, PlanError

NO_MENTIONS = discord.AllowedMentions.none()
LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")

_COLORS = {n: getattr(discord.Colour, n)() for n in (
    "red", "blue", "green", "gold", "orange", "purple", "teal", "magenta", "dark_red", "dark_blue", "dark_green",
    "dark_gold", "dark_orange", "dark_purple", "dark_teal", "light_grey", "dark_grey", "blurple", "yellow", "pink")
    if hasattr(discord.Colour, n)}


def esc(s, n: int = 200) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(s)))[:n]


def parse_color(s: str | None) -> discord.Colour | None:
    if not s:
        return None
    t = s.strip().lower().replace(" ", "_")
    if t in _COLORS:
        return _COLORS[t]
    m = re.fullmatch(r"#?([0-9a-f]{6})", t)
    if m:
        return discord.Colour(int(m[1], 16))
    raise PlanError(f"bad colour {s}", user_message=f"I don't recognise the colour '{s}'. Use a hex code like #3498db or a basic colour name.")


def ow_snapshot(channel) -> dict:
    return {str(t.id): ["role" if isinstance(t, discord.Role) else "member", *_pair(o)] for t, o in channel.overwrites.items()}


def _pair(o):
    a, d = o.pair()
    return [a.value, d.value]


async def principal(ctx: Ctx, pid: int):
    r = ctx.guild.get_role(pid)
    if r:
        return r
    m = await ctx.member(pid)
    if m:
        return m
    raise ExecError("principal gone", user_message="One of the roles or members involved no longer exists.")


def label_of(ctx: Ctx, pid: int) -> str:
    r = ctx.guild.get_role(pid)
    if r:
        return "@everyone" if r.is_default() else f"@{r.name}"
    m = ctx.guild.get_member(pid)
    return f"@{m.display_name}" if m else f"<{pid}>"


def ch_name(c) -> str:
    return f"category {c.name}" if isinstance(c, discord.CategoryChannel) else f"#{c.name}"


def need_channel(ctx: Ctx, cid: int | None, what: str = "channel"):
    c = ctx.guild.get_channel(cid) if cid else None
    if c is None:
        raise ExecError(f"{what} gone", user_message=f"That {what} doesn't exist (any more).")
    return c


def require_view(ctx: Ctx, channel) -> None:
    if ctx.actor is None:
        raise Denied("no actor")
    if ctx.is_admin():
        return
    if not channel.permissions_for(ctx.actor).view_channel:
        raise Denied("cannot view", user_message="You can't see that channel, so I can't act on it for you.")


def require_actor_channel(ctx: Ctx, channel, perm: str) -> None:
    ctx.app.auth.check_actor_perm(ctx.actor, ctx.settings, perm, channel)


def require_bot_channel(ctx: Ctx, channel, perm: str) -> None:
    ctx.app.auth.check_bot_perm(ctx.guild, perm, channel)


def parse_message_ref(ctx: Ctx, text: str, channel_id: int | None) -> tuple[int, int]:
    m = LINK.search(text)
    if m:
        if int(m[1]) != ctx.guild.id:
            raise Denied("other guild", user_message="That message link is from a different server.")
        return int(m[2]), int(m[3])
    if text.strip().isdigit() and channel_id:
        return channel_id, int(text.strip())
    raise PlanError("bad message ref", user_message="I need a message link, or a message ID plus the channel.")
