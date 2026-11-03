from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from relay.app import App
from relay.bridge import Bridge
from relay.config import Settings
from relay.storage.factory import build_store
from relay.tools.util import NO_MENTIONS
from relay.ui import ConfirmButton, present
from relay.utils import time_parser as tp

log = logging.getLogger(__name__)

COGS = ("relay.commands.chat", "relay.commands.utility", "relay.commands.admin", "relay.commands.moderation",
        "relay.commands.audit", "relay.commands.observers")


class RelayBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.moderation = True
        super().__init__(command_prefix=settings.cfg.bot.prefix, intents=intents, help_command=None,
                         allowed_mentions=NO_MENTIONS, max_messages=None)
        self.settings = settings
        self.app: App | None = None
        self.started = tp.now()
        self.bridge = Bridge(self)

    async def setup_hook(self) -> None:
        store = build_store(self.settings)
        await store.open()
        self.app = App(self.settings, store, bot=self)
        self.app.audit.notifier = self.notify_security
        self.add_dynamic_items(ConfirmButton)
        for c in COGS:
            await self.load_extension(c)
        if self.settings.cfg.bot.slash_commands and self.settings.cfg.bot.sync_commands_on_start:
            await self.tree.sync()
        await self.app.scheduler.start()
        self.maintenance.start()
        log.info("setup complete: storage=%s model=%s", store.mode, self.settings.cfg.groq.model)

    async def on_ready(self) -> None:
        log.info("logged in as %s in %d guilds", self.user, len(self.guilds))
        await self.bridge.start(self.settings.cfg.presence)

    async def close(self) -> None:
        await self.bridge.stop()
        if self.app:
            await self.app.scheduler.stop()
        if self.maintenance.is_running():
            self.maintenance.cancel()
        await super().close()
        if self.app:
            await self.app.store.close()

    async def notify_security(self, guild_id: int, row: dict) -> None:
        g = self.get_guild(guild_id)
        if not g:
            return
        s = await self.app.memory.settings(guild_id)
        ch = g.get_channel(s.security_channel) if s.security_channel else None
        if ch is None:
            return
        who = f"<@{row['actor_id']}>" if row.get("actor_id") else row["actor_type"]
        await ch.send(f"[audit] {row['action']} {row['status']} by {who}"
                      + (f" on <@{row['target_id']}>" if row.get("target_id") else "")
                      + f" (event {row['event_id'][:8]})", allowed_mentions=NO_MENTIONS)

    @tasks.loop(minutes=30)
    async def maintenance(self) -> None:
        try:
            now = tp.iso(tp.now())
            for rec in await self.app.store.find("confirmations", where={"status": "pending"}, limit=500):
                if rec["expires_at"] and rec["expires_at"] <= now:
                    await self.app.store.update_if("confirmations", rec["id"], {"status": "pending"}, {"status": "expired"}, rec["guild_id"])
            await self.app.memory.purge_expired()
            for gid in await self.app.store.guild_ids():
                await self.app.audit.prune(gid, self.user.id)
        except Exception:
            log.exception("maintenance failed")

    @maintenance.before_loop
    async def _before_maintenance(self) -> None:
        await self.wait_until_ready()

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.NoPrivateMessage)):
            return await ctx.send(f"That didn't parse: {error}", allowed_mentions=NO_MENTIONS)
        if isinstance(error, commands.CheckFailure):
            return await ctx.send("You can't use that here.", allowed_mentions=NO_MENTIONS)
        log.error("command error in %s", ctx.command, exc_info=error)
        await ctx.send("Something went wrong running that command.", allowed_mentions=NO_MENTIONS)

    async def reply_for(self, ctx: commands.Context, reply, *, ephemeral: bool = False) -> None:
        if ephemeral:
            reply.ephemeral = True
        await present(self, lambda **kw: ctx.send(**kw), reply, ctx.author, ctx.guild, ctx.channel, ephemeral_ok=True)

    async def run_cmd(self, ctx: commands.Context, tool: str, args: dict, *, ephemeral: bool = False, when: dict | None = None) -> None:
        await ctx.defer(ephemeral=ephemeral)
        reply = await self.app.assistant.run_tool(tool, args, ctx.guild, ctx.author, ctx.channel, when=when)
        await self.reply_for(ctx, reply, ephemeral=ephemeral)
