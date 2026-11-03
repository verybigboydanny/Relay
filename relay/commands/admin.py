from __future__ import annotations

from typing import Literal

import discord
from discord.ext import commands


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="config", description="Server configuration (admins)")
    @commands.guild_only()
    async def config(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Subcommands: show, role, channel, timezone.")

    @config.command(name="show", description="Show what's configured")
    async def config_show(self, ctx: commands.Context):
        await self.bot.run_cmd(ctx, "recall", {"scope": "guild"}, ephemeral=True)

    @config.command(name="role", description="Add or remove a role in a permission group")
    async def config_role(self, ctx: commands.Context, group: str, role: discord.Role, action: Literal["add", "remove"] = "add"):
        await self.bot.run_cmd(ctx, "configure", {"setting": "role_group", "group": group, "role": f"<@&{role.id}>", "action": action}, ephemeral=True)

    @config.command(name="channel", description="Set the announcements or security log channel")
    async def config_channel(self, ctx: commands.Context, which: Literal["announcements", "security"], channel: discord.TextChannel):
        await self.bot.run_cmd(ctx, "configure", {"setting": f"{which}_channel", "channel": f"<#{channel.id}>"}, ephemeral=True)

    @config.command(name="timezone", description="Set the server's default timezone, e.g. Asia/Kolkata")
    async def config_tz(self, ctx: commands.Context, timezone: str):
        await self.bot.run_cmd(ctx, "configure", {"setting": "timezone", "timezone": timezone}, ephemeral=True)

    @commands.hybrid_group(name="tasks", description="Scheduled tasks")
    @commands.guild_only()
    async def tasks(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Subcommands: list, cancel.")

    @tasks.command(name="list", description="List scheduled tasks")
    async def tasks_list(self, ctx: commands.Context, status: Literal["pending", "all"] = "pending"):
        await self.bot.run_cmd(ctx, "list_tasks", {"status": status}, ephemeral=True)

    @tasks.command(name="cancel", description="Cancel a scheduled task by id")
    async def tasks_cancel(self, ctx: commands.Context, task_id: str):
        await self.bot.run_cmd(ctx, "cancel_task", {"task": task_id}, ephemeral=True)

    @commands.hybrid_group(name="memory", description="What Relay remembers")
    @commands.guild_only()
    async def memory(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Subcommands: list, remember, forget.")

    @memory.command(name="list", description="Show stored memories")
    async def memory_list(self, ctx: commands.Context, scope: Literal["all", "user", "guild"] = "all"):
        await self.bot.run_cmd(ctx, "recall", {"scope": scope}, ephemeral=True)

    @memory.command(name="remember", description="Store a preference or note")
    async def memory_remember(self, ctx: commands.Context, text: str, scope: Literal["user", "guild"] = "user", key: str | None = None):
        await self.bot.run_cmd(ctx, "remember", {"text": text, "scope": scope, "key": key}, ephemeral=True)

    @memory.command(name="forget", description="Delete a stored memory by id or key")
    async def memory_forget(self, ctx: commands.Context, memory: str, scope: Literal["user", "guild"] = "user"):
        await self.bot.run_cmd(ctx, "forget", {"memory": memory, "scope": scope}, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
