from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import discord

from relay.utils.errors import Ambiguous, Unresolved

MEMBER_WORDS = {"him", "her", "them", "he", "she", "they", "that user", "that member", "the user", "same user",
                "that person", "the same person", "same member"}
CHANNEL_WORDS = {"it", "this", "that", "that channel", "the channel", "same channel", "the same channel"}
HERE_WORDS = {"here", "current", "this channel", "current channel", "this chat"}
ROLE_WORDS = {"that role", "the role", "same role", "the same role"}
CATEGORY_WORDS = {"that category", "the category", "same category"}
SELF_WORDS = {"me", "myself", "i", "my"}

KIND_ENTITY = {"member": "member", "user": "member", "channel": "channel", "category": "category",
               "channel_or_category": "channel", "role": "role"}


@dataclass
class Resolved:
    id: int
    label: str


@dataclass
class Env:
    guild: discord.Guild
    actor_id: int
    channel_id: int | None
    entities: dict = field(default_factory=dict)
    allowed_ids: set[int] = field(default_factory=set)
    overrides: dict = field(default_factory=dict)


def norm_channel(s: str) -> str:
    return re.sub(r"[\s_\-]+", "-", s.strip().lstrip("#").lower()).strip("-")


def norm_name(s: str) -> str:
    return s.strip().lstrip("@").casefold()


def _ch_label(c) -> str:
    if isinstance(c, discord.CategoryChannel):
        return f"category {c.name}"
    cat = f" (in {c.category.name})" if getattr(c, "category", None) else ""
    return f"#{c.name}{cat}"


class Resolver:
    async def resolve(self, env: Env, kind: str, ref: str) -> Resolved:
        try:
            return await self._resolve(env, kind, ref)
        except Ambiguous as e:
            e.kind = kind
            raise

    async def _resolve(self, env: Env, kind: str, ref: str) -> Resolved:
        raw = str(ref).strip()
        if not raw:
            raise Unresolved("empty reference")
        key = (kind, raw.casefold())
        if key in env.overrides:
            return await self._by_id(env, kind, env.overrides[key])
        low = raw.casefold()
        if kind in ("member", "user") and low in SELF_WORDS:
            return await self._by_id(env, kind, env.actor_id)
        if kind in ("channel", "channel_or_category", "category") and low in HERE_WORDS and env.channel_id:
            ch = env.guild.get_channel_or_thread(env.channel_id)
            if ch and (kind != "category" or isinstance(ch, discord.CategoryChannel)):
                return Resolved(ch.id, _ch_label(ch))
        ent_kind = KIND_ENTITY.get(kind)
        words = {"member": MEMBER_WORDS, "user": MEMBER_WORDS, "channel": CHANNEL_WORDS, "channel_or_category": CHANNEL_WORDS,
                 "role": ROLE_WORDS, "category": CATEGORY_WORDS}.get(kind, set())
        if low in words and ent_kind in env.entities:
            return await self._by_id(env, kind, int(env.entities[ent_kind]["id"]))
        m = re.fullmatch(r"<(?:@!?|@&|#)(\d{15,21})>", raw)
        if m:
            return await self._by_id(env, kind, int(m[1]), explicit=True)
        if raw.isdigit() and len(raw) >= 15:
            return await self._by_id(env, kind, int(raw))
        if kind in ("member", "user"):
            return await self._member_by_name(env, kind, raw)
        if kind == "banned":
            return await self._banned(env, raw)
        if kind in ("channel", "category", "channel_or_category"):
            return self._channel_by_name(env, kind, raw)
        if kind == "role":
            return self._role_by_name(env, raw)
        if kind == "principal":
            return await self._principal(env, raw)
        raise Unresolved(f"unknown reference kind {kind}")

    async def _by_id(self, env: Env, kind: str, i: int, explicit: bool = False) -> Resolved:
        if i != env.actor_id and i not in env.allowed_ids and not explicit and i not in {int(e["id"]) for e in env.entities.values()}:
            raise Unresolved(f"id {i} wasn't mentioned in the request")
        g = env.guild
        if kind in ("member", "user", "principal"):
            m = g.get_member(i)
            if m is None:
                try:
                    m = await g.fetch_member(i)
                except (discord.NotFound, discord.HTTPException):
                    m = None
            if m:
                return Resolved(m.id, f"@{m.display_name}")
        if kind in ("role", "principal"):
            r = g.get_role(i)
            if r:
                return Resolved(r.id, f"@{r.name}")
        if kind in ("channel", "category", "channel_or_category"):
            c = g.get_channel_or_thread(i)
            if c is not None:
                is_cat = isinstance(c, discord.CategoryChannel)
                if kind == "category" and not is_cat or kind == "channel" and is_cat:
                    raise Unresolved(f"{i} is not a {kind}")
                return Resolved(c.id, _ch_label(c))
        if kind == "user":
            try:
                u = await env.guild._state._get_client().fetch_user(i)
                return Resolved(u.id, f"{u.name} (not in server)")
            except (discord.NotFound, discord.HTTPException, AttributeError):
                pass
        if kind == "banned":
            try:
                e = await g.fetch_ban(discord.Object(i))
                return Resolved(e.user.id, f"{e.user.name} (banned)")
            except (discord.NotFound, discord.HTTPException):
                pass
        raise Unresolved(f"couldn't find a {kind} with id {i}")

    async def _member_by_name(self, env: Env, kind: str, raw: str) -> Resolved:
        g = env.guild
        q = norm_name(raw)

        def names(m: discord.Member):
            return {norm_name(x) for x in (m.name, m.display_name, m.global_name or "", m.nick or "") if x}

        exact = [m for m in g.members if q in names(m)]
        if not exact:
            try:
                found = await asyncio.wait_for(g.query_members(query=raw.lstrip("@"), limit=8), 5)
            except (asyncio.TimeoutError, discord.HTTPException, discord.ClientException):
                found = []
            exact = [m for m in found if q in names(m)]
            if not exact and found:
                raise Ambiguous(kind, raw, [(m.id, f"@{m.display_name} ({m.name})") for m in found[:8]], suggestion=True)
        if len(exact) == 1:
            return Resolved(exact[0].id, f"@{exact[0].display_name}")
        if len(exact) > 1:
            raise Ambiguous(kind, raw, [(m.id, f"@{m.display_name} ({m.name})") for m in exact[:10]])
        near = [m for m in g.members if any(q in n for n in names(m))][:8]
        if near:
            raise Ambiguous(kind, raw, [(m.id, f"@{m.display_name} ({m.name})") for m in near], suggestion=True)
        raise Unresolved(f"no member named '{raw}'")

    async def _banned(self, env: Env, raw: str) -> Resolved:
        q = norm_name(raw)
        hits = []
        try:
            async for e in env.guild.bans(limit=1000):
                if q in (norm_name(e.user.name), norm_name(e.user.global_name or "")):
                    hits.append(e.user)
        except discord.Forbidden:
            raise Unresolved("I can't read the ban list (missing Ban Members permission)")
        if len(hits) == 1:
            return Resolved(hits[0].id, f"{hits[0].name} (banned)")
        if hits:
            raise Ambiguous("banned", raw, [(u.id, f"{u.name} ({u.id})") for u in hits[:10]])
        raise Unresolved(f"no banned user named '{raw}'")

    def _channel_by_name(self, env: Env, kind: str, raw: str) -> Resolved:
        chans = [c for c in env.guild.channels if not isinstance(c, discord.Thread)]
        if kind == "category":
            chans = [c for c in chans if isinstance(c, discord.CategoryChannel)]
        elif kind == "channel":
            chans = [c for c in chans if not isinstance(c, discord.CategoryChannel)]
        q = norm_channel(raw)
        exact = [c for c in chans if norm_channel(c.name) == q]
        if len(exact) == 1:
            return Resolved(exact[0].id, _ch_label(exact[0]))
        if exact:
            raise Ambiguous(kind, raw, [(c.id, _ch_label(c)) for c in exact[:10]])
        near = [c for c in chans if q and (q in norm_channel(c.name) or norm_channel(c.name) in q)][:8]
        if near:
            raise Ambiguous(kind, raw, [(c.id, _ch_label(c)) for c in near], suggestion=True)
        raise Unresolved(f"no {kind.replace('_', ' ')} named '{raw}'")

    def _role_by_name(self, env: Env, raw: str) -> Resolved:
        q = norm_name(raw)
        if q in ("everyone", "@everyone"):
            return Resolved(env.guild.default_role.id, "@everyone")
        roles = [r for r in env.guild.roles if not r.is_default()]
        exact = [r for r in roles if norm_name(r.name) == q]
        if len(exact) == 1:
            return Resolved(exact[0].id, f"@{exact[0].name}")
        if exact:
            raise Ambiguous("role", raw, [(r.id, f"@{r.name}") for r in exact[:10]])
        near = [r for r in roles if q and (q in norm_name(r.name))][:8]
        if near:
            raise Ambiguous("role", raw, [(r.id, f"@{r.name}") for r in near], suggestion=True)
        raise Unresolved(f"no role named '{raw}'")

    async def _principal(self, env: Env, raw: str) -> Resolved:
        try:
            return self._role_by_name(env, raw)
        except Unresolved:
            return await self._member_by_name(env, "member", raw)
