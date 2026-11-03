from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from relay.tools.base import recently_touched

log = logging.getLogger(__name__)

SETTLE = 1.5


class Observers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _record(self, guild: discord.Guild, kind: str, rid: int | None, *, vis: str = "staff", details: dict | None = None,
                      target_id: int | None = None, resource_type: str | None = None, channel_id: int | None = None,
                      lookup=None) -> None:
        try:
            await asyncio.sleep(SETTLE)
            app = self.bot.app
            if app is None or (rid and recently_touched(rid)) or (target_id and recently_touched(target_id)):
                return
            actor = None
            if lookup is not None:
                try:
                    async for e in guild.audit_logs(limit=6, action=lookup):
                        if getattr(e.target, "id", None) == (rid or target_id):
                            actor = e.user.id
                            break
                except (discord.Forbidden, discord.HTTPException):
                    pass
            if actor == self.bot.user.id:
                return
            await app.memory.record_event(guild.id, kind, actor_id=actor, target_id=target_id, resource_type=resource_type, resource_id=rid,
                                          channel_id=channel_id, visibility=vis, details=details, source="discord")
            await app.audit.record(guild_id=guild.id, action=f"observed.{kind}", actor_id=actor, actor_type="external", target_id=target_id,
                                   resource_type=resource_type, resource_id=rid, details={**(details or {}), "actor_known": actor is not None})
        except Exception:
            log.warning("observer failed for %s", kind, exc_info=True)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch):
        await self._record(ch.guild, "channel.create", ch.id, vis="channel", details={"name": ch.name}, resource_type="channel",
                           channel_id=ch.id, lookup=discord.AuditLogAction.channel_create)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, ch):
        await self._record(ch.guild, "channel.delete", ch.id, details={"name": ch.name}, resource_type="channel",
                           lookup=discord.AuditLogAction.channel_delete)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        changes = {}
        if before.name != after.name:
            changes["name"] = f"{before.name} -> {after.name}"
        if getattr(before, "category_id", None) != getattr(after, "category_id", None):
            changes["category"] = f"{getattr(before, 'category_id', None)} -> {getattr(after, 'category_id', None)}"
        if {t.id: tuple(map(lambda p: p.value, o.pair())) for t, o in before.overwrites.items()} != \
                {t.id: tuple(map(lambda p: p.value, o.pair())) for t, o in after.overwrites.items()}:
            changes["overwrites"] = "changed"
        if changes:
            await self._record(after.guild, "channel.permissions" if list(changes) == ["overwrites"] else "channel.update", after.id,
                               vis="staff" if "overwrites" in changes else "channel", details={"name": after.name, **changes},
                               resource_type="channel", channel_id=after.id, lookup=discord.AuditLogAction.channel_update)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        await self._record(role.guild, "role.create", role.id, details={"name": role.name}, resource_type="role", lookup=discord.AuditLogAction.role_create)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await self._record(role.guild, "role.delete", role.id, details={"name": role.name}, resource_type="role", lookup=discord.AuditLogAction.role_delete)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        ch = {}
        if before.name != after.name:
            ch["name"] = f"{before.name} -> {after.name}"
        if before.permissions != after.permissions:
            ch["permissions"] = "changed"
        if ch:
            await self._record(after.guild, "role.update", after.id, details=ch, resource_type="role", lookup=discord.AuditLogAction.role_update)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if set(before.roles) != set(after.roles):
            add = [r.name for r in set(after.roles) - set(before.roles)]
            rem = [r.name for r in set(before.roles) - set(after.roles)]
            await self._record(after.guild, "member.roles", None, target_id=after.id, resource_type="member",
                               details={"added": add, "removed": rem}, lookup=discord.AuditLogAction.member_role_update)
        if before.timed_out_until != after.timed_out_until:
            await self._record(after.guild, "moderation.timeout", None, target_id=after.id, resource_type="member",
                               details={"until": str(after.timed_out_until)}, lookup=discord.AuditLogAction.member_update)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await self._record(guild, "moderation.ban", None, target_id=user.id, resource_type="member", lookup=discord.AuditLogAction.ban)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        await self._record(guild, "moderation.unban", None, target_id=user.id, resource_type="member", lookup=discord.AuditLogAction.unban)


async def setup(bot):
    await bot.add_cog(Observers(bot))
