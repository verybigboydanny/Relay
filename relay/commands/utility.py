from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from relay.tools.util import NO_MENTIONS
from relay.utils import time_parser as tp


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="What Relay can do")
    @commands.guild_only()
    async def help(self, ctx: commands.Context):
        name = self.bot.settings.cfg.bot.name
        await ctx.send(
            f"Mention me and say what you want, for example: \"{name}, create a private staff category with an announcements channel\", "
            "\"mute rambutan for 10 minutes\", \"every Friday at 7 PM post a moderation summary in #admin\", \"what happened yesterday?\", "
            "\"undo the last permission change\".\n"
            "Risky actions ask for confirmation first. Commands: help, status, ask, permissions, config, tasks, memory, audit, warn, timeout, kick, ban, unban, history.",
            allowed_mentions=NO_MENTIONS)

    @commands.hybrid_command(name="ask", description="Ask Relay to do something in plain English")
    @commands.guild_only()
    async def ask(self, ctx: commands.Context, *, request: str):
        await ctx.defer()
        reply = await self.bot.app.assistant.handle(request, ctx.guild, ctx.author, ctx.channel, "natural_language")
        await self.bot.reply_for(ctx, reply)

    @commands.hybrid_command(name="status", description="Relay's health and counters")
    @commands.guild_only()
    async def status(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        app = self.bot.app
        st = await app.store.health()
        pending = await app.store.count("tasks", guild_id=ctx.guild.id, where={"status": "pending"})
        waiting = await app.store.count("confirmations", guild_id=ctx.guild.id, where={"status": "pending"})
        up = tp.fmt_duration((tp.now() - self.bot.started).total_seconds())
        await ctx.send(f"Up {up}. Latency {self.bot.latency * 1000:.0f} ms. Storage: {st['mode']} ({'ok' if st['ok'] else 'FAILING'}). "
                       f"Model: {app.cfg.groq.model}. Pending tasks here: {pending}. Waiting confirmations: {waiting}.",
                       allowed_mentions=NO_MENTIONS, ephemeral=True)

    @commands.hybrid_command(name="groq", description="Check the Groq connection (admins)")
    @commands.guild_only()
    async def groq_check(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        app = self.bot.app
        s = await app.memory.settings(ctx.guild.id)
        if not app.auth.is_admin(ctx.author, s):
            return await ctx.send("Admins only.", ephemeral=True)
        res = await app.groq.ping()
        if not res["ok"]:
            return await ctx.send(f"Groq check failed: {res['error']}.", ephemeral=True)
        note = "" if res["model_available"] else " The configured model wasn't in Groq's model list; update groq.model in config.json" + (f" (available: {', '.join(res['suggestions'])})." if res.get("suggestions") else ".")
        await ctx.send(f"Groq reachable. Model {res['model']}; JSON mode {'on' if res['json_mode'] else 'off (model rejected it)'}.{note}", ephemeral=True)

    @commands.hybrid_command(name="storage", description="Storage diagnostics (admins)")
    @commands.guild_only()
    async def storage_check(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        app = self.bot.app
        s = await app.memory.settings(ctx.guild.id)
        if not app.auth.is_admin(ctx.author, s):
            return await ctx.send("Admins only.", ephemeral=True)
        h = await app.store.health()
        counts = {c: await app.store.count(c, guild_id=ctx.guild.id) for c in ("memories", "events", "tasks", "warnings", "confirmations", "snapshots")}
        v = await app.audit.verify(ctx.guild.id)
        await ctx.send(f"Backend: {h['mode']} ({'ok' if h['ok'] else h.get('error')}). Records here: " + ", ".join(f"{k} {n}" for k, n in counts.items()) +
                       f". Audit entries: {v['checked']}, chain {'intact' if v['ok'] else 'PROBLEMS: ' + str(len(v['problems']))}.", ephemeral=True)

    @commands.hybrid_command(name="permissions", description="Why can (or can't) a member see a channel")
    @commands.guild_only()
    async def permissions(self, ctx: commands.Context, member: discord.Member, channel: discord.abc.GuildChannel):
        await ctx.defer(ephemeral=True)
        reply = await self.bot.app.assistant.run_tool("explain_access", {"member": f"<@{member.id}>", "channel": f"<#{channel.id}>"},
                                                      ctx.guild, ctx.author, ctx.channel)
        await self.bot.reply_for(ctx, reply, ephemeral=True)



async def setup(bot):
    cog = Utility(bot)
    await bot.add_cog(cog)
