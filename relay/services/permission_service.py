from __future__ import annotations

import re

import discord

from relay.utils.errors import PlanError

FLAGS: dict[str, int] = dict(discord.Permissions.VALID_FLAGS)
_CANON: dict[int, str] = {}
for _n in sorted(FLAGS, key=lambda n: (n in ("read_messages", "manage_permissions", "manage_emojis", "manage_emojis_and_stickers", "external_emojis", "external_stickers"), n)):
    _CANON.setdefault(FLAGS[_n], _n)

ALIASES = {
    "view": "view_channel", "see": "view_channel", "read": "view_channel", "access": "view_channel",
    "send": "send_messages", "write": "send_messages", "talk": "send_messages", "post": "send_messages",
    "history": "read_message_history", "read_history": "read_message_history",
    "reactions": "add_reactions", "react": "add_reactions", "attach": "attach_files", "upload": "attach_files",
    "embed": "embed_links", "links": "embed_links", "join": "connect", "slash_commands": "use_application_commands",
    "commands": "use_application_commands", "everyone_ping": "mention_everyone", "ping_everyone": "mention_everyone",
    "events": "manage_events", "manage_event": "manage_events", "manage_server": "manage_guild",
    "timeout": "moderate_members", "timeout_members": "moderate_members", "ban": "ban_members", "kick": "kick_members",
    "webhooks": "manage_webhooks", "channels": "manage_channels", "roles": "manage_roles",
    "messages": "manage_messages", "pin": "pin_messages", "admin": "administrator",
}

ESCALATING = {"administrator"}
DANGEROUS = {"administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "ban_members",
             "kick_members", "mention_everyone", "manage_messages", "moderate_members", "manage_permissions",
             "view_audit_log", "manage_expressions", "manage_nicknames", "manage_threads", "manage_events"}


def norm_perm(name: str) -> str:
    key = re.sub(r"[\s\-]+", "_", str(name).strip().lower())
    key = ALIASES.get(key, key)
    if key not in FLAGS:
        raise PlanError(f"unsupported permission '{name}'", user_message=f"'{name}' isn't a permission I recognise.")
    return _CANON[FLAGS[key]]


def norm_perms(names) -> set[str]:
    return {norm_perm(n) for n in (names or [])}


def pretty(names) -> str:
    return ", ".join(n.replace("_", " ").title() for n in names)


def names_of(value: int) -> list[str]:
    return sorted(n for b, n in _CANON.items() if value & b)


def value_of(names) -> int:
    v = 0
    for n in names:
        v |= FLAGS[norm_perm(n)]
    return v


def ow_pair(ow: discord.PermissionOverwrite | None) -> tuple[int, int]:
    if ow is None:
        return 0, 0
    a, d = ow.pair()
    return a.value, d.value


def pair_to_ow(a: int, d: int) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite.from_pair(discord.Permissions(a), discord.Permissions(d))


def apply_change(pair: tuple[int, int], allow=(), deny=(), reset=()) -> tuple[int, int]:
    a, d = pair
    for n in allow:
        b = FLAGS[n]
        a |= b
        d &= ~b
    for n in deny:
        b = FLAGS[n]
        d |= b
        a &= ~b
    for n in reset:
        b = FLAGS[n]
        a &= ~b
        d &= ~b
    return a, d


def effective(base: int, role_ids: set[int], member_id: int, everyone_id: int, ow: dict[int, tuple[int, int]]) -> int:
    admin = FLAGS["administrator"]
    if base & admin:
        return discord.Permissions.all().value
    perms = base
    a, d = ow.get(everyone_id, (0, 0))
    perms = (perms & ~d) | a
    ra = rd = 0
    for rid in role_ids:
        if rid in ow:
            ra |= ow[rid][0]
            rd |= ow[rid][1]
    perms = (perms & ~rd) | ra
    a, d = ow.get(member_id, (0, 0))
    return (perms & ~d) | a


def overwrites_map(channel) -> dict[int, tuple[int, int]]:
    return {t.id: ow_pair(o) for t, o in channel.overwrites.items()}


def member_effective(member: discord.Member, ow: dict[int, tuple[int, int]]) -> int:
    rids = {r.id for r in member.roles if r.id != member.guild.id}
    return effective(member.guild_permissions.value, rids, member.id, member.guild.id, ow)


def lockouts(guild: discord.Guild, new_ow: dict[int, tuple[int, int]], old_ow: dict[int, tuple[int, int]],
             people: list[tuple[str, discord.Member]]) -> list[str]:
    view = FLAGS["view_channel"]
    out = []
    for label, m in people:
        if m is None:
            continue
        if member_effective(m, old_ow) & view and not member_effective(m, new_ow) & view:
            out.append(f"{label} would lose access to the channel")
    return out


def describe_pair(pair: tuple[int, int]) -> str:
    a, d = pair
    parts = [f"+{n}" for n in names_of(a)] + [f"-{n}" for n in names_of(d)]
    return " ".join(parts) or "inherit"


def diff_lines(old: dict[int, tuple[int, int]], new: dict[int, tuple[int, int]], label) -> list[str]:
    out = []
    for tid in sorted(set(old) | set(new)):
        o, n = old.get(tid, (0, 0)), new.get(tid, (0, 0))
        if o != n:
            out.append(f"{label(tid)}: {describe_pair(o)} -> {describe_pair(n)}")
    return out


def privilege_flags(allow: set[str]) -> tuple[set[str], set[str]]:
    return allow & ESCALATING, allow & DANGEROUS
