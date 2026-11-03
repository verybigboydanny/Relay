from __future__ import annotations

import discord
from discord.ext import commands


def _when(delay: str | None) -> dict | None:
    return {"in": delay} if delay else None


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="warn", description="Warn a member")
    @commands.guild_only()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        await self.bot.run_cmd(ctx, "warn_member", {"members": [f"<@{member.id}>"], "reason": reason})

    @commands.hybrid_command(name="timeout", description="Time a member out (mute)")
    @commands.guild_only()
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str, reason: str | None = None, delay: str | None = None):
        await self.bot.run_cmd(ctx, "timeout_member", {"members": [f"<@{member.id}>"], "duration": duration, "reason": reason}, when=_when(delay))

    @commands.hybrid_command(name="untimeout", description="Remove a member's timeout")
    @commands.guild_only()
    async def untimeout(self, ctx: commands.Context, member: discord.Member, reason: str | None = None, delay: str | None = None):
        await self.bot.run_cmd(ctx, "untimeout_member", {"members": [f"<@{member.id}>"], "reason": reason}, when=_when(delay))

    @commands.hybrid_command(name="kick", description="Kick a member")
    @commands.guild_only()
    async def kick(self, ctx: commands.Context, member: discord.Member, reason: str | None = None, delay: str | None = None):
        await self.bot.run_cmd(ctx, "kick_member", {"members": [f"<@{member.id}>"], "reason": reason}, when=_when(delay))

    @commands.hybrid_command(name="ban", description="Ban a user (member or ID)")
    @commands.guild_only()
    async def ban(self, ctx: commands.Context, user: discord.User, reason: str | None = None, delay: str | None = None):
        await self.bot.run_cmd(ctx, "ban_member", {"members": [f"<@{user.id}>"], "reason": reason}, when=_when(delay))

    @commands.hybrid_command(name="unban", description="Unban a user")
    @commands.guild_only()
    async def unban(self, ctx: commands.Context, user: discord.User, reason: str | None = None, delay: str | None = None):
        await self.bot.run_cmd(ctx, "unban_user", {"users": [f"<@{user.id}>"], "reason": reason}, when=_when(delay))

    @commands.hybrid_command(name="history", description="A member's roles, timeouts and warnings")
    @commands.guild_only()
    async def history(self, ctx: commands.Context, member: discord.Member):
        await self.bot.run_cmd(ctx, "inspect_member", {"member": f"<@{member.id}>"}, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
