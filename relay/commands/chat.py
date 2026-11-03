from __future__ import annotations

import re

import discord
from discord.ext import commands

from relay.ui import present


class Chat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _is_reply_to_me(self, message: discord.Message) -> bool:
        ref = message.reference
        if ref is None or ref.message_id is None:
            return False
        target = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if target is None:
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
        return target.author.id == self.bot.user.id

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        bot = self.bot
        if message.author.bot or message.guild is None or not bot.settings.cfg.bot.mention_trigger or bot.app is None:
            return
        if not isinstance(message.author, discord.Member):
            return
        if message.content.startswith(bot.settings.cfg.bot.prefix):
            return
        mention = re.compile(rf"<@!?{bot.user.id}>")
        mentioned = bool(mention.search(message.content)) and bot.user in message.mentions
        if not mentioned and not await self._is_reply_to_me(message):
            return
        text = mention.sub("", message.content).strip()
        if not text:
            return await message.reply(bot.settings.cfg.personality.greeting.strip() or "Hey, what can I do for you?", mention_author=False)
        async with message.channel.typing():
            reply = await bot.app.assistant.handle(text, message.guild, message.author, message.channel)
        await present(bot, lambda **kw: message.reply(mention_author=False, **kw), reply, message.author, message.guild, message.channel)


async def setup(bot):
    await bot.add_cog(Chat(bot))
