from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock

import discord

_ids = itertools.count(100000000000000000, 7)
ADMIN = discord.Permissions(administrator=True)


def nid() -> int:
    return next(_ids)


def mk_role(name: str, pos: int, perms: discord.Permissions | None = None, id: int | None = None, managed: bool = False) -> MagicMock:
    r = MagicMock(spec=discord.Role)
    r.id = id or nid()
    r.name = name
    r.position = pos
    r.permissions = perms or discord.Permissions.none()
    r.managed = managed
    r.hoist = False
    r.mentionable = False
    r.members = []
    r.colour = discord.Colour.default()
    r.is_default = lambda: pos == 0
    r.__gt__ = lambda self, o: self.position > o.position
    r.__lt__ = lambda self, o: self.position < o.position
    r.__ge__ = lambda self, o: self.position >= o.position
    r.__le__ = lambda self, o: self.position <= o.position
    r.__hash__ = lambda self: hash(self.id)
    r.__eq__ = lambda self, o: getattr(o, "id", None) == self.id
    r.edit = AsyncMock()
    r.delete = AsyncMock()
    return r


def mk_member(guild, name: str, roles: list | None = None, id: int | None = None) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = id or nid()
    m.name = name
    m.display_name = name
    m.global_name = None
    m.nick = None
    m.guild = guild
    m.roles = [guild.default_role] + list(roles or [])
    m.top_role = max(m.roles, key=lambda r: r.position)
    base = 0
    for r in m.roles:
        base |= r.permissions.value
    perms = discord.Permissions(base)
    if perms.administrator:
        perms = discord.Permissions.all()
    m.guild_permissions = perms
    m.timed_out_until = None
    m.joined_at = None
    for fn in ("send", "timeout", "add_roles", "remove_roles", "edit"):
        setattr(m, fn, AsyncMock())
    m.__hash__ = lambda self: hash(self.id)
    m.__eq__ = lambda self, o: getattr(o, "id", None) == self.id
    guild.members.append(m)
    return m


def mk_guild(name: str = "Test", owner_id: int | None = None) -> MagicMock:
    g = MagicMock(spec=discord.Guild)
    g.id = nid()
    g.name = name
    g.owner_id = owner_id or nid()
    g.default_role = mk_role("@everyone", 0, discord.Permissions(view_channel=True, send_messages=True, read_message_history=True), id=g.id)
    g.roles = [g.default_role]
    g.members = []
    g.channels = []
    g.categories = []
    g.get_member = lambda i: next((m for m in g.members if m.id == i), None)
    g.get_role = lambda i: next((r for r in g.roles if r.id == i), None)
    g.get_channel = lambda i: next((c for c in g.channels if c.id == i), None)
    g.get_channel_or_thread = g.get_channel
    g.fetch_member = AsyncMock(side_effect=lambda i: g.get_member(i) or (_ for _ in ()).throw(discord.NotFound(MagicMock(status=404), "nf")))
    g.query_members = AsyncMock(return_value=[])
    g.ban = AsyncMock()
    g.kick = AsyncMock()
    g.unban = AsyncMock()
    me = mk_member(g, "Relay", [mk_role("RelayBot", 50, discord.Permissions(manage_channels=True, manage_roles=True, ban_members=True,
                                                                         kick_members=True, moderate_members=True, manage_nicknames=True,
                                                                         view_channel=True, send_messages=True))], id=nid())
    me.roles[1].guild = g
    g.roles.append(me.roles[1])
    g.default_role.guild = g
    g.me = me
    return g


def mk_channel(guild, name: str, kind=discord.TextChannel, category=None, overwrites=None, perms_for=None) -> MagicMock:
    c = MagicMock(spec=kind)
    c.id = nid()
    c.name = name
    c.guild = guild
    c.category = category
    c.category_id = category.id if category else None
    c.position = len(guild.channels)
    c.overwrites = overwrites or {}
    c.topic = None
    c.type = discord.ChannelType.category if kind is discord.CategoryChannel else discord.ChannelType.text
    c.permissions_for = perms_for or (lambda m: m.guild_permissions)
    c.edit = AsyncMock()
    c.delete = AsyncMock()
    c.send = AsyncMock()
    if kind is discord.CategoryChannel:
        c.channels = []
        guild.categories.append(c)
    elif category is not None:
        category.channels.append(c)
    guild.channels.append(c)
    return c


def add_role(guild, name: str, pos: int, perms=None) -> MagicMock:
    r = mk_role(name, pos, perms)
    r.guild = guild
    guild.roles.append(r)
    return r
