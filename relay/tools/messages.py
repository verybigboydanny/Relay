from __future__ import annotations

import re

import discord
from pydantic import Field

from relay.tools.base import Args, Ctx, Refs, Result, ref, tool
from relay.tools.util import NO_MENTIONS, esc, need_channel, parse_message_ref, require_actor_channel, require_bot_channel, require_view
from relay.utils import time_parser as tp
from relay.utils.errors import Denied, ExecError

EVERYONE = re.compile(r"@(everyone|here)")


def _clean(content: str, allow_everyone: bool) -> str:
    return content if allow_everyone else EVERYONE.sub("@\u200b\\1", content)


def _target_channel(ctx: Ctx, r: Refs, announcement: bool = False):
    cid = r.one("channel")
    if cid is None and announcement and ctx.settings.announcement_channel:
        cid = ctx.settings.announcement_channel
    if cid is None:
        cid = ctx.channel_id
    return need_channel(ctx, cid)


def _check_post(ctx: Ctx, ch, ping_everyone: bool = False) -> None:
    if not isinstance(ch, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
        raise Denied("not postable", user_message="I can't post in that kind of channel.")
    require_view(ctx, ch)
    require_actor_channel(ctx, ch, "send_messages")
    require_bot_channel(ctx, ch, "send_messages")
    if ping_everyone:
        require_actor_channel(ctx, ch, "mention_everyone")
        require_bot_channel(ctx, ch, "mention_everyone")


def _mentions(ctx: Ctx, ch, ids: list[int], ping_everyone: bool):
    users, roles = [], []
    for i in ids:
        role = ctx.guild.get_role(i)
        if role:
            if not (role.mentionable or ctx.is_admin() or ch.permissions_for(ctx.actor).mention_everyone):
                raise Denied("role ping", user_message=f"You can't ping {role.name}; it isn't mentionable.")
            roles.append(role)
        else:
            users.append(discord.Object(i))
    return discord.AllowedMentions(everyone=ping_everyone, users=users, roles=roles, replied_user=False)


class SendMessage(Args):
    content: str = Field(min_length=1, max_length=1900)
    channel: str | None = ref("channel", None, "defaults to the current channel")
    ping: list[str] | None = ref("principal", None, "members/roles to mention")
    ping_everyone: bool = False
    announcement: bool = False
    draft: bool = Field(False, description="only show the text, don't post")


async def _guard_send(ctx, a, r):
    if a.draft:
        return
    _check_post(ctx, _target_channel(ctx, r, a.announcement), a.ping_everyone)


@tool("send_message", "post a message in a channel (announcement=true uses the announcements channel; draft=true only shows the text)",
      SendMessage, risk="low", access="open", tags=("messages",), guard=_guard_send,
      describe=lambda a, r: ("draft a message" if a.draft else f"post in {r.label('channel') or 'this channel'}: " + esc(a.content, 80)))
async def send_message(ctx: Ctx, a: SendMessage, r: Refs) -> Result:
    if a.draft:
        return Result(f"Draft:\n{esc(a.content, 1800)}", details={"draft": True})
    ch = _target_channel(ctx, r, a.announcement)
    am = _mentions(ctx, ch, r.many("ping"), a.ping_everyone)
    msg = await ch.send(_clean(a.content, a.ping_everyone), allowed_mentions=am)
    return Result(f"Posted in #{ch.name}: {msg.jump_url}", resource_id=msg.id, resource_type="message", touched=[ch.id],
                  details={"channel": ch.id, "length": len(a.content)}, entities={"channel": {"id": ch.id, "name": ch.name}})


class Remind(Args):
    text: str = Field(min_length=1, max_length=500)
    to: list[str] | None = ref("principal", None, "who to remind; defaults to the requester")
    channel: str | None = ref("channel", None)
    dm: bool = False


async def _guard_remind(ctx, a, r):
    if not a.dm:
        _check_post(ctx, _target_channel(ctx, r))


@tool("remind", "send a reminder (use 'when' to schedule it); mentions the target in a channel or DMs them", Remind, risk="low",
      access="open", tags=("messages", "schedule"), guard=_guard_remind, idempotent=False,
      describe=lambda a, r: f"remind {r.label('to') or 'you'}: " + esc(a.text, 80))
async def remind(ctx: Ctx, a: Remind, r: Refs) -> Result:
    targets = r.many("to") or [ctx.actor_id]
    body = f"Reminder: {_clean(a.text, False)}"
    if a.dm:
        sent = 0
        for t in targets:
            m = await ctx.member(t)
            if m is None:
                continue
            try:
                await m.send(f"{body}\n(from {ctx.guild.name})")
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                pass
        if sent:
            return Result(f"Sent the reminder by DM to {sent} {'person' if sent == 1 else 'people'}.", details={"dm": sent})
    ch = _target_channel(ctx, r)
    if ch is None:
        raise ExecError("no channel", user_message="There's nowhere to post that reminder.")
    am = _mentions(ctx, ch, targets, False)
    ping = " ".join(f"<@&{t}>" if ctx.guild.get_role(t) else f"<@{t}>" for t in targets)
    await ch.send(f"{ping} {body}", allowed_mentions=am)
    return Result(f"Reminded {len(targets)} in #{ch.name}.", resource_type="message", details={"channel": ch.id})


class MessageRef(Args):
    message: str = Field(description="message link, or id together with channel")
    channel: str | None = ref("channel", None)


async def _msg(ctx: Ctx, a, r: Refs) -> discord.Message:
    cid, mid = parse_message_ref(ctx, a.message, r.one("channel") or ctx.channel_id)
    ch = need_channel(ctx, cid)
    require_view(ctx, ch)
    try:
        return await ch.fetch_message(mid)
    except discord.NotFound:
        raise ExecError("no message", user_message="I couldn't find that message.")


class EditMessage(MessageRef):
    content: str = Field(min_length=1, max_length=1900)


async def _guard_edit_msg(ctx, a, r):
    m = await _msg(ctx, a, r)
    if m.author.id != ctx.me.id:
        raise Denied("not mine", user_message="I can only edit messages I posted myself.")
    require_actor_channel(ctx, m.channel, "manage_messages")


@tool("edit_message", "edit a message that Relay posted earlier", EditMessage, risk="low", perm="manage_messages", tags=("messages",),
      guard=_guard_edit_msg, describe=lambda a, r: "edit one of my messages")
async def edit_message(ctx: Ctx, a: EditMessage, r: Refs) -> Result:
    m = await _msg(ctx, a, r)
    await m.edit(content=_clean(a.content, False), allowed_mentions=NO_MENTIONS)
    return Result(f"Edited the message: {m.jump_url}", resource_id=m.id, resource_type="message")


async def _guard_delete_msg(ctx, a, r):
    m = await _msg(ctx, a, r)
    require_actor_channel(ctx, m.channel, "manage_messages")
    if m.author.id != ctx.me.id:
        require_bot_channel(ctx, m.channel, "manage_messages")


@tool("delete_message", "delete a message by link or id", MessageRef, risk="medium", perm="manage_messages", tags=("messages",),
      guard=_guard_delete_msg, describe=lambda a, r: f"delete message {esc(a.message, 60)}")
async def delete_message(ctx: Ctx, a: MessageRef, r: Refs) -> Result:
    m = await _msg(ctx, a, r)
    info = {"channel": m.channel.id, "author": m.author.id, "length": len(m.content)}
    await m.delete()
    return Result(f"Deleted the message from <@{m.author.id}> in #{m.channel.name}.", resource_id=m.id, resource_type="message",
                  details=info, event=("message.delete", "staff"))


class PinMessage(MessageRef):
    pin: bool = True


async def _guard_pin(ctx, a, r):
    m = await _msg(ctx, a, r)
    ch = m.channel
    perms = ch.permissions_for(ctx.actor)
    if not (ctx.is_admin() or perms.manage_messages or getattr(perms, "pin_messages", False)):
        raise Denied("no pin perm", user_message="You can't pin messages in that channel.")
    if not getattr(ch.permissions_for(ctx.me), "pin_messages", False):
        require_bot_channel(ctx, ch, "manage_messages")


@tool("pin_message", "pin or unpin a message", PinMessage, risk="low", perm="manage_messages", tags=("messages",), guard=_guard_pin,
      idempotent=True, describe=lambda a, r: ("pin" if a.pin else "unpin") + f" message {esc(a.message, 60)}")
async def pin_message(ctx: Ctx, a: PinMessage, r: Refs) -> Result:
    m = await _msg(ctx, a, r)
    if a.pin:
        await m.pin(reason=ctx.reason("pinned"))
    else:
        await m.unpin(reason=ctx.reason("unpinned"))
    return Result(f"{'Pinned' if a.pin else 'Unpinned'} the message: {m.jump_url}", resource_id=m.id, resource_type="message")


class SearchMessages(Args):
    channel: str | None = ref("channel", None, "defaults to the current channel")
    author: str | None = ref("member", None)
    contains: str | None = Field(None, max_length=100)
    period: str | None = Field(None, description="today|yesterday|last 24 hours|last 7 days ...")
    limit: int = Field(15, ge=1, le=30)
    summarize: bool = Field(False, description="true to summarize the discussion instead of listing it")


async def _guard_search(ctx, a, r):
    ch = need_channel(ctx, r.one("channel") or ctx.channel_id)
    require_view(ctx, ch)
    if not ctx.is_admin() and not ch.permissions_for(ctx.actor).read_message_history:
        raise Denied("no history", user_message="You don't have permission to read that channel's history.")
    require_bot_channel(ctx, ch, "read_message_history")


@tool("search_messages", "find or summarize recent messages in a channel by author, text or period (only what Discord history exposes to me)",
      SearchMessages, risk="read", access="open", tags=("messages",), guard=_guard_search,
      describe=lambda a, r: f"search messages in {r.label('channel') or 'this channel'}")
async def search_messages(ctx: Ctx, a: SearchMessages, r: Refs) -> Result:
    ctx.app.limits.check_history(ctx.guild.id)
    ch = need_channel(ctx, r.one("channel") or ctx.channel_id)
    after = None
    if a.period:
        after = tp.parse_period(a.period, tp.tz_of(ctx.tz_name))[0]
    scan = ctx.app.cfg.limits.max_history_fetch
    found, seen, blank = [], 0, 0
    async for m in ch.history(limit=scan, after=after, oldest_first=False):
        seen += 1
        if not m.content:
            blank += 1
        if r.one("author") and m.author.id != r.one("author"):
            continue
        if a.contains and a.contains.lower() not in m.content.lower():
            continue
        if not m.content and not m.attachments:
            continue
        found.append(m)
        if len(found) >= a.limit:
            break
    if not found:
        note = " Message content may be hidden because the Message Content intent isn't enabled." if seen and blank == seen else ""
        return Result(f"No matching messages in the last {seen} I could read in #{ch.name}.{note}")
    found.reverse()
    lines = [f"[{tp.fmt_local(m.created_at, tp.tz_of(ctx.tz_name))}] {esc(m.author.display_name, 32)}: {esc(m.content or '(attachment)', 220)} <{m.jump_url}>" for m in found]
    head = f"{len(found)} message{'s' if len(found) != 1 else ''} in #{ch.name} (scanned the latest {seen}):"
    if a.summarize:
        return Result(head, summarize=True, data={"records": lines, "question": f"Summarize the discussion in #{ch.name}" + (f" ({a.period})" if a.period else "")})
    return Result(head + "\n" + "\n".join(lines)[:1700], data={"lines": lines})
