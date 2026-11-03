from __future__ import annotations

from dataclasses import dataclass

import discord

from relay.config import Config
from relay.tools.base import Tool
from relay.utils.errors import Denied


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    level: str = ""


def grant_matches(grants: list[str], tool: Tool) -> bool:
    for g in grants:
        if g == "*" or g == tool.name:
            return True
        if g == "@read" and tool.risk == "read" and "sensitive" not in tool.tags:
            return True
        if g.startswith("@") and g[1:] in tool.tags:
            return True
    return False


class Authorizer:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def groups(self, member: discord.Member, settings) -> set[str]:
        ids = {r.id for r in member.roles}
        out = {g for g, rids in settings.roles.items() if ids & set(rids)}
        if member.guild.owner_id == member.id:
            out.add("admin")
        elif self.cfg.policy.administrator_is_admin and member.guild_permissions.administrator:
            out.add("admin")
        return out

    def is_admin(self, member: discord.Member, settings) -> bool:
        return "admin" in self.groups(member, settings)

    def is_owner(self, member: discord.Member) -> bool:
        return member.guild.owner_id == member.id

    def is_staff(self, member: discord.Member, settings) -> bool:
        g = self.groups(member, settings)
        return bool(g & {"admin", "moderator"})

    def decide(self, tool: Tool, member: discord.Member | None, settings) -> Decision:
        if member is None:
            return Decision(False, "you're no longer a member of this server")
        groups = self.groups(member, settings)
        if "restricted" in groups and "admin" not in groups:
            return Decision(False, "you're restricted from using me")
        if "admin" in groups:
            return Decision(True, level="admin")
        if tool.access == "admin":
            return Decision(False, "that needs an administrator")
        if tool.access == "open":
            return Decision(True, level="open")
        grants = []
        for g in groups:
            grants += self.cfg.policy.groups.get(g, [])
        if not grant_matches(grants, tool):
            return Decision(False, f"your roles aren't allowed to use {tool.name.replace('_', ' ')}")
        if tool.perm and not getattr(member.guild_permissions, tool.perm, False):
            return Decision(False, f"you don't have the {tool.perm.replace('_', ' ')} permission")
        return Decision(True, level="granted")

    def outranks(self, actor: discord.Member, target: discord.Member | discord.Role) -> bool:
        guild = actor.guild
        if actor.id == guild.owner_id:
            return not (isinstance(target, discord.Member) and target.id == guild.owner_id and target.id != actor.id)
        if isinstance(target, discord.Member):
            if target.id == guild.owner_id:
                return False
            return actor.top_role > target.top_role
        return actor.top_role > target

    def bot_outranks(self, target: discord.Member | discord.Role) -> bool:
        me = target.guild.me
        if isinstance(target, discord.Member):
            if target.id == target.guild.owner_id:
                return False
            return me.top_role > target.top_role
        return me.top_role > target

    def check_member(self, actor: discord.Member, target: discord.Member, *, bot_acts: bool = True, allow_self: bool = False) -> None:
        if target.id == actor.id and not allow_self:
            raise Denied("self target", user_message="You can't do that to yourself.")
        if target.id == target.guild.me.id:
            raise Denied("bot target", user_message="I won't do that to myself.")
        if not self.outranks(actor, target):
            raise Denied("hierarchy", user_message=f"{target.display_name} is at or above your highest role.")
        if bot_acts and not self.bot_outranks(target):
            raise Denied("bot hierarchy", user_message=f"{target.display_name} is above my highest role, so I can't act on them.")

    def check_role(self, actor: discord.Member, role: discord.Role, *, bot_acts: bool = True) -> None:
        if role.is_default():
            raise Denied("everyone", user_message="That isn't allowed on @everyone.")
        if not self.outranks(actor, role):
            raise Denied("hierarchy", user_message=f"{role.name} is at or above your highest role.")
        if bot_acts and not self.bot_outranks(role):
            raise Denied("bot hierarchy", user_message=f"{role.name} is above my highest role, so I can't change it.")
        if role.managed and bot_acts:
            raise Denied("managed", user_message=f"{role.name} is managed by an integration and can't be assigned.")

    def check_can_grant(self, actor: discord.Member, perms: set[str], settings, channel=None) -> None:
        if "administrator" in perms and not (self.is_owner(actor) or actor.guild_permissions.administrator):
            raise Denied("admin grant", user_message="Only the owner or someone with Administrator can grant Administrator.")
        if self.is_admin(actor, settings):
            return
        have = actor.guild_permissions if channel is None else channel.permissions_for(actor)
        missing = [p for p in perms if not getattr(have, p, False)]
        if missing:
            raise Denied("grant beyond authority",
                         user_message="You can't grant permissions you don't have yourself: " + ", ".join(m.replace("_", " ") for m in missing) + ".")

    def check_bot_perm(self, guild: discord.Guild, perm: str, channel=None) -> None:
        have = guild.me.guild_permissions if channel is None else channel.permissions_for(guild.me)
        if not getattr(have, perm, False):
            raise Denied("bot lacks permission", user_message=f"I don't have the {perm.replace('_', ' ')} permission{' in that channel' if channel else ''}.")

    def check_actor_perm(self, actor: discord.Member, settings, perm: str, channel=None) -> None:
        if self.is_admin(actor, settings):
            return
        have = actor.guild_permissions if channel is None else channel.permissions_for(actor)
        if not getattr(have, perm, False):
            raise Denied("actor lacks permission", user_message=f"You don't have the {perm.replace('_', ' ')} permission{' there' if channel else ''}.")
