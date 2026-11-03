from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from relay.tools.util import NO_MENTIONS

if TYPE_CHECKING:
    from relay.bot import RelayBot
    from relay.assistant import Pending, Reply

log = logging.getLogger(__name__)


class ConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=r"relay:c:(?P<cid>[0-9a-f]{10}):(?P<v>ok|no)"):
    def __init__(self, cid: str, verb: str):
        super().__init__(discord.ui.Button(label="Confirm" if verb == "ok" else "Cancel",
                                           style=discord.ButtonStyle.danger if verb == "ok" else discord.ButtonStyle.secondary,
                                           custom_id=f"relay:c:{cid}:{verb}"))
        self.cid, self.verb = cid, verb

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["cid"], match["v"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "RelayBot" = interaction.client
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)
        await interaction.response.defer()
        reply = await bot.app.assistant.confirm(self.cid, interaction.user, interaction.guild, interaction.channel, self.verb == "ok")
        if reply.ephemeral:
            return await interaction.followup.send(reply.text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass
        await present(bot, lambda **kw: interaction.followup.send(**kw), reply, interaction.user, interaction.guild, interaction.channel)


def confirm_view(cid: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ConfirmButton(cid, "ok"))
    v.add_item(ConfirmButton(cid, "no"))
    return v


class PickView(discord.ui.View):
    def __init__(self, bot: "RelayBot", pend: "Pending", candidates: list[tuple[int, str]], guild, member, channel):
        super().__init__(timeout=120)
        self.bot, self.pend, self.guild, self.member, self.channel = bot, pend, guild, member, channel
        sel = discord.ui.Select(placeholder="Pick one", options=[discord.SelectOption(label=lbl[:100], value=str(i)) for i, lbl in candidates[:25]])
        sel.callback = self.picked
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.pend.member_id:
            await interaction.response.send_message("That question isn't for you.", ephemeral=True)
            return False
        return True

    async def picked(self, interaction: discord.Interaction) -> None:
        choice = int(interaction.data["values"][0])
        await interaction.response.edit_message(content="Got it.", view=None)
        self.stop()
        reply = await self.bot.app.assistant.resume(self.pend, self.guild, self.member, self.channel, choice)
        await present(self.bot, lambda **kw: interaction.followup.send(**kw), reply, self.member, self.guild, self.channel)

    async def on_timeout(self) -> None:
        self.stop()


class Pager(discord.ui.View):
    def __init__(self, lines: list[str], owner_id: int, per_page: int):
        super().__init__(timeout=300)
        self.lines, self.owner_id, self.per = lines, owner_id, max(per_page, 3)
        self.page = 0
        self._sync()

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.lines) // self.per))

    def text(self) -> str:
        chunk = self.lines[self.page * self.per:(self.page + 1) * self.per]
        return ("\n".join(chunk) + f"\n\nPage {self.page + 1}/{self.pages}")[:1990]

    def _sync(self) -> None:
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run the command yourself to page through it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(content=self.text(), view=self, allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(content=self.text(), view=self, allowed_mentions=NO_MENTIONS)


async def present(bot: "RelayBot", send: Callable[..., Awaitable], reply: "Reply", member, guild, channel, *, ephemeral_ok: bool = False) -> None:
    kw: dict = {"allowed_mentions": NO_MENTIONS}
    text = reply.text
    view = None
    per = bot.app.cfg.audit.page_size
    if reply.pager and len(reply.pager) > per:
        view = Pager(reply.pager, member.id, per)
        text = view.text()
    if reply.confirmation:
        view = confirm_view(reply.confirmation["id"])
    if reply.ambiguity:
        view = PickView(bot, reply.ambiguity["pending"], reply.ambiguity["candidates"], guild, member, channel)
    if reply.file:
        kw["file"] = discord.File(io.BytesIO(reply.file[1]), filename=reply.file[0])
    if view is not None:
        kw["view"] = view
    if ephemeral_ok and reply.ephemeral:
        kw["ephemeral"] = True
    await send(content=(text or "Done.")[:2000], **kw)
