from __future__ import annotations

import discord
from discord.ext import commands


class Audit(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="audit", description="Search the audit log (staff)")
    @commands.guild_only()
    async def audit(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Subcommands: recent, user, target, action, failures, task, export, verify.")

    async def _search(self, ctx, **args):
        args = {k: v for k, v in args.items() if v is not None}
        args.setdefault("limit", 50)
        await self.bot.run_cmd(ctx, "audit_search", args, ephemeral=True)

    @audit.command(name="recent", description="The latest entries")
    async def recent(self, ctx: commands.Context, limit: int = 20):
        await self._search(ctx, limit=max(1, min(limit, 50)))

    @audit.command(name="user", description="Actions performed by a user")
    async def by_user(self, ctx: commands.Context, user: discord.User):
        await self._search(ctx, actor=f"<@{user.id}>")

    @audit.command(name="target", description="Actions that affected a user")
    async def by_target(self, ctx: commands.Context, user: discord.User):
        await self._search(ctx, target=f"<@{user.id}>")

    @audit.command(name="action", description="Entries for one action, e.g. ban_member")
    async def by_action(self, ctx: commands.Context, action: str, period: str | None = None):
        await self._search(ctx, action=action, period=period)

    @audit.command(name="failures", description="Failed or denied entries")
    async def failures(self, ctx: commands.Context, status: str = "failure", period: str | None = None):
        await self._search(ctx, status=status, period=period)

    @audit.command(name="task", description="History of one scheduled task")
    async def by_task(self, ctx: commands.Context, task_id: str):
        await self._search(ctx, task=task_id)

    @audit.command(name="export", description="Export a period as a JSON Lines file (admins)")
    async def export(self, ctx: commands.Context, period: str = "today"):
        await self.bot.run_cmd(ctx, "audit_export", {"period": period}, ephemeral=True)

    @audit.command(name="verify", description="Check the audit hash chain (admins)")
    async def verify(self, ctx: commands.Context):
        await self.bot.run_cmd(ctx, "audit_verify", {}, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Audit(bot))
