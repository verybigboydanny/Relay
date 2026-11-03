"""File-based mailbox between the dashboard app and the running bot.

The app writes a request into data/bridge/inbox. The bot handles it through the normal planner and writes the
answer to data/bridge/outbox. The bot also publishes data/bridge/state.json (servers, presence).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path

import discord

from relay.config import ROOT

log = logging.getLogger(__name__)

POLL = 0.35
STATE_EVERY = 4.0
STATUS = {"online": discord.Status.online, "idle": discord.Status.idle, "dnd": discord.Status.dnd,
          "invisible": discord.Status.invisible}
ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_AVATAR = 6 * 1024 * 1024


class BridgeError(Exception):
    pass


def _atomic(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def build_activity(cfg, text: str):
    """The discord.py activity for a PresenceCfg-like object, or None."""
    t = getattr(cfg, "activity_type", "none")
    text = (text or "").strip()
    if t == "none" or not text:
        return None
    if t == "playing":
        return discord.Game(name=text)
    if t == "streaming":
        return discord.Streaming(name=text, url=cfg.url or "https://twitch.tv/discord")
    if t == "custom":
        return discord.CustomActivity(name=text)
    kind = {"watching": discord.ActivityType.watching, "listening": discord.ActivityType.listening,
            "competing": discord.ActivityType.competing}[t]
    return discord.Activity(type=kind, name=text)


class PresenceManager:
    def __init__(self, bot):
        self.bot = bot
        self.cfg = None
        self._task: asyncio.Task | None = None
        self._i = 0
        self.shown = ""

    def fill(self, text: str) -> str:
        g = self.bot.guilds
        vals = {"servers": str(len(g)), "members": str(sum(x.member_count or 0 for x in g)),
                "prefix": self.bot.settings.cfg.bot.prefix}
        return re.sub(r"\{(servers|members|prefix)\}", lambda m: vals[m.group(1)], text)

    def lines(self) -> list[str]:
        c = self.cfg
        return [x for x in ([c.text] if c.text else []) + list(c.rotate) if x]

    async def _show(self, text: str) -> None:
        self.shown = self.fill(text)
        await self.bot.change_presence(status=STATUS[self.cfg.status], activity=build_activity(self.cfg, self.shown))

    async def apply(self, cfg) -> None:
        self.cfg = cfg
        self._i = 0
        await self._cancel()
        lines = self.lines()
        await self._show(lines[0] if lines else "")
        if len(lines) > 1 and cfg.activity_type != "none":
            self._task = asyncio.create_task(self._rotate())

    async def _rotate(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cfg.rotate_seconds)
                lines = self.lines()
                self._i += 1
                await self._show(lines[self._i % len(lines)])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("presence rotation stopped")

    async def _cancel(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def stop(self) -> None:
        await self._cancel()

    def current(self) -> dict:
        c = self.cfg
        return {"status": c.status if c else "online", "activity_type": c.activity_type if c else "none", "text": self.shown}


class Bridge:
    def __init__(self, bot, root: Path = ROOT):
        self.bot = bot
        self.dir = Path(root) / "data" / "bridge"
        self.inbox, self.outbox = self.dir / "inbox", self.dir / "outbox"
        self.presence = PresenceManager(bot)
        self._tasks: list[asyncio.Task] = []
        self._work: set[asyncio.Task] = set()
        self._picks: dict[str, tuple[float, object]] = {}

    async def start(self, presence_cfg=None) -> None:
        if presence_cfg is not None:
            try:
                await self.presence.apply(presence_cfg)
            except Exception:
                log.exception("couldn't apply presence")
        if self._tasks:
            return
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 120
        for d in (self.inbox, self.outbox):
            for f in d.glob("*"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
        self._tasks = [asyncio.create_task(self._poll()), asyncio.create_task(self._state_loop())]
        log.info("dashboard bridge ready")

    async def stop(self) -> None:
        await self.presence.stop()
        for t in self._tasks + list(self._work):
            t.cancel()
        self._tasks = []
        try:
            (self.dir / "state.json").unlink()
        except OSError:
            pass

    async def _poll(self) -> None:
        while True:
            try:
                for f in sorted(self.inbox.glob("*.json")):
                    try:
                        msg = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        msg = None
                    try:
                        f.unlink()
                    except OSError:
                        pass
                    if isinstance(msg, dict):
                        t = asyncio.create_task(self._handle(msg))
                        self._work.add(t)
                        t.add_done_callback(self._work.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bridge poll failed")
            await asyncio.sleep(POLL)

    async def _state_loop(self) -> None:
        while True:
            try:
                _atomic(self.dir / "state.json", self.state())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bridge state failed")
            await asyncio.sleep(STATE_EVERY)

    def state(self) -> dict:
        b, u = self.bot, self.bot.user
        lat = b.latency
        return {
            "ts": time.time(),
            "user": {"id": str(u.id), "name": u.name, "avatar": str(u.display_avatar.url)} if u else None,
            "latency_ms": round(lat * 1000) if isinstance(lat, float) and math.isfinite(lat) else None,
            "presence": self.presence.current(),
            "guilds": [{"id": str(g.id), "name": g.name, "members": g.member_count or 0,
                        "icon": str(g.icon.url) if g.icon else None,
                        "nick": g.me.nick if g.me else None,
                        "channels": [{"id": str(c.id), "name": c.name} for c in g.text_channels[:80]]} for g in b.guilds],
        }

    async def _handle(self, msg: dict) -> None:
        mid = str(msg.get("id") or "")
        if not ID_OK.match(mid):
            return
        try:
            out = await asyncio.wait_for(self._dispatch(msg), 240)
        except BridgeError as e:
            out = {"ok": False, "error": str(e)}
        except asyncio.TimeoutError:
            out = {"ok": False, "error": "That took too long on my side. Check Discord to see whether it went through."}
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bridge request failed: %s", msg.get("type"))
            out = {"ok": False, "error": "Something went wrong inside me while doing that."}
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            _atomic(self.outbox / f"{mid}.json", out)
        except OSError:
            log.exception("couldn't write bridge reply")

    async def _dispatch(self, msg: dict) -> dict:
        kind = msg.get("type")
        if kind == "ping":
            return {"ok": True}
        if kind == "presence":
            from relay.config import PresenceCfg
            try:
                cfg = PresenceCfg.model_validate(msg.get("presence") or {})
            except Exception as e:
                raise BridgeError(f"That presence isn't valid: {str(e)[:120]}")
            await self.presence.apply(cfg)
            return {"ok": True, "shown": self.presence.shown}
        if kind == "username":
            return await self._username(str(msg.get("name") or ""))
        if kind == "avatar":
            return await self._avatar(str(msg.get("data") or ""))
        guild, owner, channel = await self._where(msg)
        if kind == "nickname":
            nick = (str(msg.get("nickname") or "").strip() or None)
            if nick and len(nick) > 32:
                raise BridgeError("Nicknames can be at most 32 characters.")
            try:
                await guild.me.edit(nick=nick)
            except discord.Forbidden:
                raise BridgeError("I'm not allowed to change my nickname in that server (it needs the Change Nickname permission).")
            return {"ok": True, "text": f"Nickname in {guild.name} is now {nick or 'my default name'}."}
        assistant = self.bot.app.assistant
        if kind == "act":
            text = str(msg.get("text") or "").strip()
            if not text:
                raise BridgeError("What should I do?")
            return self._pack(await assistant.handle(text, guild, owner, channel), guild, channel)
        if kind == "confirm":
            return self._pack(await assistant.confirm(str(msg.get("cid") or ""), owner, guild, channel, bool(msg.get("approve"))), guild, channel)
        if kind == "pick":
            token = str(msg.get("token") or "")
            ent = self._picks.pop(token, None)
            if ent is None:
                raise BridgeError("That question has expired. Ask me again.")
            try:
                choice = int(msg.get("choice"))
            except (TypeError, ValueError):
                raise BridgeError("That wasn't one of the options.")
            return self._pack(await assistant.resume(ent[1], guild, owner, channel, choice), guild, channel)
        raise BridgeError("I don't know how to do that from here.")

    async def _where(self, msg: dict):
        bot = self.bot
        try:
            gid = int(msg.get("guild_id") or 0)
        except (TypeError, ValueError):
            gid = 0
        guild = bot.get_guild(gid) if gid else (bot.guilds[0] if len(bot.guilds) == 1 else None)
        if guild is None:
            raise BridgeError("Pick a server first." if bot.guilds else "I'm not in any server yet. Use the invite link on the Home tab.")
        owner = guild.get_member(guild.owner_id)
        if owner is None:
            try:
                owner = await guild.fetch_member(guild.owner_id)
            except discord.HTTPException:
                raise BridgeError("I couldn't find the server owner, so I can't act on their behalf.")
        channel = None
        try:
            cid = int(msg.get("channel_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid:
            channel = guild.get_channel(cid)
        me = guild.me
        if channel is None:
            sc = guild.system_channel
            if sc and sc.permissions_for(me).send_messages:
                channel = sc
        if channel is None:
            channel = next((c for c in guild.text_channels if c.permissions_for(me).send_messages), None) \
                or (guild.text_channels[0] if guild.text_channels else None)
        if channel is None:
            raise BridgeError("That server has no text channel I can use.")
        return guild, owner, channel

    def _pack(self, reply, guild, channel) -> dict:
        now = time.time()
        self._picks = {k: v for k, v in self._picks.items() if now - v[0] < 600}
        out = {"ok": True, "text": reply.text or "Done.", "guild_id": str(guild.id), "guild": guild.name, "channel_id": str(channel.id)}
        if reply.confirmation:
            out["confirmation"] = {"cid": reply.confirmation["id"]}
        if reply.ambiguity:
            token = uuid.uuid4().hex[:12]
            self._picks[token] = (now, reply.ambiguity["pending"])
            out["ambiguity"] = {"token": token, "options": [{"id": str(i), "label": str(lbl)[:100]} for i, lbl in reply.ambiguity["candidates"][:25]]}
        if reply.pager:
            out["lines"] = [str(x)[:300] for x in reply.pager[:80]]
        if reply.file:
            out["text"] += f"\n(That included a file, {reply.file[0]}. Ask me to post it in a channel to get it.)"
        return out

    async def _username(self, name: str) -> dict:
        name = name.strip()
        if not 2 <= len(name) <= 32:
            raise BridgeError("A username needs 2 to 32 characters.")
        try:
            await self.bot.user.edit(username=name)
        except discord.HTTPException as e:
            raise BridgeError("Discord refused that username. It may be taken, or changed too often (limit: about twice an hour). " + str(getattr(e, "text", "") or "")[:80])
        return {"ok": True, "text": f"I'm now called {name}."}

    async def _avatar(self, data_url: str) -> dict:
        m = re.match(r"^data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)$", data_url)
        if not m:
            raise BridgeError("Use a PNG, JPG, GIF or WebP image.")
        raw = base64.b64decode(m.group(2), validate=False)
        if len(raw) > MAX_AVATAR:
            raise BridgeError("That image is too big. Keep it under 6 MB.")
        try:
            await self.bot.user.edit(avatar=raw)
        except discord.HTTPException as e:
            raise BridgeError("Discord refused the new picture (they limit avatar changes to a few per hour). " + str(getattr(e, "text", "") or "")[:80])
        return {"ok": True, "text": "New profile picture is live."}
