from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import discord
from pydantic import Field

from relay.services import permission_service as ps
from relay.services.member_resolver import norm_channel, norm_name
from relay.tools.base import Args, Ctx, Item, Refs, Result, tool


@dataclass(frozen=True)
class Ch:
    emoji: str
    name: str
    kind: str = "text"
    topic: str = ""
    readonly: bool = False
    limit: int = 0


@dataclass(frozen=True)
class Cat:
    emoji: str
    title: str
    channels: tuple
    staff: bool = False


STAFF = Cat("🛡️", "Staff", (
    Ch("🛡️", "staff-chat", topic="Private space for the team."),
    Ch("🧾", "mod-log", topic="Moderation notes and decisions."),
    Ch("🔊", "Staff Room", "voice"),
), staff=True)

INFO = Cat("📌", "Info", (
    Ch("👋", "welcome", topic="Start here. Say hi and have a look around.", readonly=True),
    Ch("📜", "rules", topic="The house rules. Short and fair.", readonly=True),
    Ch("📢", "announcements", topic="News and updates from the team.", readonly=True),
))

TEMPLATES: dict[str, dict] = {
    "gaming": {
        "blurb": "a gaming server",
        "member": ("Gamer", 0x9B59B6),
        "staff": ("Moderator", 0xE74C3C),
        "categories": (
            INFO,
            Cat("💬", "Lounge", (
                Ch("💬", "general", topic="Hang out and talk about anything."),
                Ch("😂", "memes", topic="Keep it funny and friendly."),
                Ch("🎬", "clips-and-highlights", topic="Share your best plays."),
                Ch("🤝", "looking-for-group", topic="Find people to play with."),
                Ch("🔊", "Lounge", "voice"),
                Ch("🎧", "Chill", "voice"),
            )),
            Cat("🎮", "Games", (
                Ch("🎯", "game-discussion", topic="Talk about what you're playing."),
                Ch("💡", "tips-and-guides", topic="Share strategies, builds, and guides."),
                Ch("🏆", "achievements", topic="Brag about your wins."),
                Ch("🎮", "Squad 1", "voice", limit=4),
                Ch("🎮", "Squad 2", "voice", limit=4),
                Ch("🎮", "Squad 3", "voice", limit=4),
            )),
            STAFF,
        ),
    },
    "community": {
        "blurb": "a friendly community server",
        "member": ("Member", 0x2ECC71),
        "staff": ("Moderator", 0xE67E22),
        "categories": (
            INFO,
            Cat("💬", "Chat", (
                Ch("💬", "general", topic="The main place to talk."),
                Ch("🙋", "introductions", topic="Tell us a bit about yourself."),
                Ch("🎲", "off-topic", topic="Everything else."),
                Ch("📸", "media", topic="Photos, clips, and links."),
                Ch("🔊", "Lounge", "voice"),
                Ch("☕", "Hangout", "voice"),
            )),
            Cat("🎉", "Community", (
                Ch("📅", "events", topic="What's coming up.", readonly=True),
                Ch("💡", "suggestions", topic="Ideas to make this place better."),
                Ch("🗳️", "polls", topic="Quick votes."),
                Ch("🎤", "Event Stage", "voice"),
            )),
            STAFF,
        ),
    },
    "study": {
        "blurb": "a study group server",
        "member": ("Student", 0x3498DB),
        "staff": ("Moderator", 0xE74C3C),
        "categories": (
            INFO,
            Cat("📚", "Study", (
                Ch("💬", "general", topic="Chat and check in."),
                Ch("🧠", "homework-help", topic="Ask questions, explain your thinking."),
                Ch("🗂️", "resources", topic="Notes, links, and good references."),
                Ch("🗓️", "study-planner", topic="Goals, schedules, and accountability."),
            )),
            Cat("🎧", "Focus Rooms", (
                Ch("🔇", "Quiet Room", "voice", limit=6),
                Ch("📖", "Focus 1", "voice", limit=8),
                Ch("📖", "Focus 2", "voice", limit=8),
            )),
            Cat("☕", "Social", (
                Ch("🎲", "off-topic", topic="Breaks are allowed."),
                Ch("😂", "memes"),
                Ch("🔊", "Hangout", "voice"),
            )),
            STAFF,
        ),
    },
    "creative": {
        "blurb": "a creative community",
        "member": ("Creator", 0xF1C40F),
        "staff": ("Moderator", 0xE74C3C),
        "categories": (
            INFO,
            Cat("🖼️", "Showcase", (
                Ch("✨", "share-your-work", topic="Finished pieces go here."),
                Ch("🛠️", "work-in-progress", topic="Early drafts and experiments."),
                Ch("🗣️", "feedback", topic="Kind, specific critique."),
                Ch("🌟", "inspiration", topic="Things that made you want to make things."),
            )),
            Cat("💬", "Chat", (
                Ch("💬", "general"),
                Ch("🤝", "collabs", topic="Find partners for projects."),
                Ch("📎", "resources", topic="Tools, tutorials, and assets."),
                Ch("🎲", "off-topic"),
                Ch("🎨", "Studio", "voice"),
                Ch("🧑‍💻", "Co-working", "voice"),
            )),
            STAFF,
        ),
    },
}

STAFF_PERMS = ["kick_members", "moderate_members", "manage_messages"]


def _cat_name(c: Cat, fancy: bool) -> str:
    return f"{c.emoji} {c.title}" if fancy else c.title


def _ch_name(c: Ch, fancy: bool) -> str:
    if c.kind == "voice":
        return f"{c.emoji} {c.name}" if fancy else c.name
    return f"{c.emoji}・{c.name}" if fancy else c.name


def _plan_cats(a) -> list[Cat]:
    t = TEMPLATES[a.template]
    cats = [c for c in t["categories"] if a.staff_area or not c.staff]
    extra = []
    for n in a.extra_text_channels:
        extra.append(Ch("💬", n.strip().lower().replace(" ", "-")))
    for n in a.extra_voice_channels:
        extra.append(Ch("🔊", n.strip(), "voice"))
    if extra:
        cats = list(cats)
        idx = next((i for i, c in enumerate(cats) if not c.staff and c.title != "Info"), 0)
        cats[idx] = Cat(cats[idx].emoji, cats[idx].title, tuple(cats[idx].channels) + tuple(extra), cats[idx].staff)
    return cats


class SetupServer(Args):
    template: Literal["gaming", "community", "study", "creative"]
    fancy_names: bool = Field(True, description="emoji-styled channel names")
    staff_area: bool = Field(True, description="private staff category and a Moderator role")
    member_role: str | None = Field(None, max_length=50, description="rename the member role")
    staff_role: str | None = Field(None, max_length=50, description="rename the staff role")
    extra_text_channels: list[str] = Field(default_factory=list, max_length=6)
    extra_voice_channels: list[str] = Field(default_factory=list, max_length=4)


def _names(a):
    t = TEMPLATES[a.template]
    return (a.member_role or t["member"][0]), (a.staff_role or t["staff"][0])


def _outline(a, r=None) -> str:
    t = TEMPLATES[a.template]
    member, staff = _names(a)
    lines = [f"set this server up as {t['blurb']}:", f"  Roles: {member}" + (f", {staff} (staff)" if a.staff_area else "")]
    for c in _plan_cats(a):
        items = ", ".join(_ch_name(x, False) + (" (read-only)" if x.readonly else "") for x in c.channels)
        lines.append(f"  {_cat_name(c, a.fancy_names)}{' (private)' if c.staff else ''}: {items}")
    lines.append("  Anything that already exists with the same name is left alone.")
    return "\n".join(lines)


async def _guard(ctx, a, r):
    auth = ctx.app.auth
    for p in ("manage_channels", "manage_roles"):
        auth.check_bot_perm(ctx.guild, p)
        auth.check_actor_perm(ctx.actor, ctx.settings, p)
    if a.staff_area:
        auth.check_can_grant(ctx.actor, set(STAFF_PERMS), ctx.settings)


@tool("setup_server", "set the whole server up from a ready-made layout (gaming, community, study, creative): roles, categories, topics, read-only info channels, a private staff area. Use this for 'set this server up as ...' instead of asking questions",
      SetupServer, risk="high", perm="manage_channels", confirm="always", tags=("channels", "roles"), guard=_guard,
      schedulable=False, timeout=300, describe=_outline)
async def setup_server(ctx: Ctx, a: SetupServer, r: Refs) -> Result:
    g = ctx.guild
    t = TEMPLATES[a.template]
    fancy = a.fancy_names
    member_name, staff_name = _names(a)
    items: list[Item] = []
    reused: list[str] = []
    touched: list[int] = []
    summary: list[str] = []

    async def role(name: str, colour: int, perms: list[str], hoist: bool):
        for existing in g.roles:
            if norm_name(existing.name) == norm_name(name):
                reused.append(f"the {existing.name} role")
                return existing
        try:
            made = await g.create_role(name=name, permissions=discord.Permissions(ps.value_of(perms)), colour=discord.Colour(colour),
                                       hoist=hoist, reason=ctx.reason("server setup"))
            touched.append(made.id)
            items.append(Item(made.id, True, f"role {name}"))
            return made
        except discord.HTTPException as e:
            items.append(Item(None, False, f"role {name}: {str(e.text)[:80]}", "http"))
            return None

    member_role = await role(member_name, t["member"][1], [], False)
    staff_role = await role(staff_name, t["staff"][1], STAFF_PERMS, True) if a.staff_area else None
    roles_line = [x for x in (member_name if member_role else None, f"{staff_name} (staff)" if staff_role else None) if x]
    if roles_line:
        summary.append("Roles: " + ", ".join(roles_line))
    first_channel = None
    for c in _plan_cats(a):
        cname = _cat_name(c, fancy)
        cat = next((x for x in g.categories if norm_channel(x.name) == norm_channel(cname)), None)
        if cat is None:
            ow = {}
            if c.staff:
                ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
                      g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
                if staff_role:
                    ow[staff_role] = discord.PermissionOverwrite(view_channel=True)
                if ctx.actor and not ctx.is_admin():
                    ow[ctx.actor] = discord.PermissionOverwrite(view_channel=True)
            try:
                cat = await g.create_category(cname, overwrites=ow, reason=ctx.reason("server setup"))
                touched.append(cat.id)
                items.append(Item(cat.id, True, f"category {cname}"))
            except discord.HTTPException as e:
                items.append(Item(None, False, f"category {cname}: {str(e.text)[:80]}", "http"))
                continue
        made_names = []
        for ch in c.channels:
            nm = _ch_name(ch, fancy)
            if any(norm_channel(x.name) == norm_channel(nm) for x in cat.channels):
                reused.append(f"#{nm}")
                continue
            try:
                if ch.kind == "voice":
                    new = await g.create_voice_channel(nm, category=cat, user_limit=ch.limit, reason=ctx.reason("server setup"))
                else:
                    ow = {}
                    if ch.readonly:
                        ow = {g.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
                              g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
                        if staff_role:
                            ow[staff_role] = discord.PermissionOverwrite(send_messages=True)
                    kw = {"overwrites": ow} if ow else {}
                    new = await g.create_text_channel(nm, category=cat, topic=ch.topic or None, reason=ctx.reason("server setup"), **kw)
                touched.append(new.id)
                items.append(Item(new.id, True, f"channel {nm}"))
                made_names.append(ch.name + (" (read-only)" if ch.readonly else ""))
                first_channel = first_channel or new
            except discord.HTTPException as e:
                items.append(Item(None, False, f"channel {nm}: {str(e.text)[:80]}", "http"))
        if made_names:
            summary.append(f"{cname}{' (private)' if c.staff else ''}: " + ", ".join(made_names))
    failed = [i.text for i in items if not i.ok]
    head = f"Your server is now set up as {t['blurb']}." if not failed else f"I set up most of {t['blurb']}, but some parts failed."
    body = [head] + [f"• {s}" for s in summary]
    if reused:
        body.append("Already there, so I left them alone: " + ", ".join(reused[:10]) + (f" and {len(reused) - 10} more" if len(reused) > 10 else "") + ".")
    if failed:
        body.append("Couldn't create: " + "; ".join(failed[:5]) + ".")
    if not items:
        body = ["Everything in that layout already exists, so there was nothing to add."]
    return Result("\n".join(body), items=items, touched=touched, resource_type="server",
                  details={"template": a.template, "created": [i.text for i in items if i.ok][:60], "reused": reused[:30]},
                  entities={"channel": {"id": first_channel.id, "name": first_channel.name}} if first_channel else {},
                  event=("server.setup", "staff") if any(i.ok for i in items) else None)
