from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from relay.ai import prompts
from relay.ai.schemas import PlannerOut, StepIn
from relay.services.binder import BoundPlan, Binder
from relay.services.executor import PlanResult, new_request_id
from relay.services.member_resolver import Env
from relay.tools.base import ref_fields
from relay.utils import time_parser as tp
from relay.utils.errors import AIError, Ambiguous, PlanError, RateLimited, RelayError, Unresolved
from relay.utils.redact import scrub_text

if TYPE_CHECKING:
    from relay.app import App

log = logging.getLogger(__name__)

ID_RE = re.compile(r"\d{15,21}")
ENTITY_KEY = {"member": "member", "user": "member", "channel": "channel", "channel_or_category": "channel",
              "category": "category", "role": "role"}


@dataclass
class Pending:
    out: PlannerOut
    text: str
    guild_id: int
    member_id: int
    channel_id: int
    source: str
    overrides: dict = field(default_factory=dict)
    extra_ids: set = field(default_factory=set)
    system: str | None = None
    repaired: bool = False
    last_ambiguity: tuple | None = None
    candidate_ids: set = field(default_factory=set)


@dataclass
class Reply:
    text: str
    confirmation: dict | None = None
    ambiguity: dict | None = None
    file: tuple[str, bytes] | None = None
    pager: list[str] | None = None
    ephemeral: bool = False


def server_layout(guild: discord.Guild, limit: int = 3600) -> str:
    """Compact, sanitized picture of the server so the planner can resolve and count things."""
    clean = prompts.clean

    def tag(c) -> str:
        return clean(c.name, 32) + (" (voice)" if isinstance(c, (discord.VoiceChannel, discord.StageChannel)) else "")

    lines = []
    for cat in sorted(guild.categories, key=lambda c: c.position)[:30]:
        kids = ", ".join(tag(c) for c in cat.channels[:40])
        lines.append(f"[{clean(cat.name, 40)}] {kids or '(empty)'}")
    loose = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel) and c.category is None]
    if loose:
        lines.append("(no category) " + ", ".join(tag(c) for c in loose[:40]))
    roles = [r for r in sorted(guild.roles, key=lambda r: -r.position) if not r.is_default()][:40]
    if roles:
        lines.append("Roles: " + ", ".join(f"{clean(r.name, 30)}({len(r.members)})" for r in roles))
    lines.append(f"Totals: {len(guild.channels)} channels incl. {len(guild.categories)} categories, {len(guild.roles) - 1} roles, {guild.member_count or 0} members")
    return "\n".join(lines)[:limit]


class Assistant:
    def __init__(self, app: "App"):
        self.app = app

    async def handle(self, text: str, guild: discord.Guild, member: discord.Member, channel, source: str = "natural_language") -> Reply:
        app = self.app
        text = text.strip()
        if not text:
            return Reply("What do you need?")
        if len(text) > app.cfg.limits.max_request_chars:
            return Reply(f"That's too long for me to handle safely. Keep it under {app.cfg.limits.max_request_chars} characters.")
        return await self._guarded(guild, member, lambda: self._nl(text, guild, member, channel, source), text)

    async def run_tool(self, tool_name: str, args: dict, guild: discord.Guild, member: discord.Member, channel, *, when: dict | None = None) -> Reply:
        step = StepIn.model_validate({"id": "s1", "tool": tool_name, "args": args, "when": when})
        out = PlannerOut(type="plan", steps=[step])
        pend = Pending(out, f"/{tool_name}", guild.id, member.id, channel.id, "command")
        return await self._guarded(guild, member, lambda: self._bind_and_run(pend, guild, member, channel), pend.text)

    async def resume(self, pend: Pending, guild: discord.Guild, member: discord.Member, channel, choice_id: int) -> Reply:
        if member.id != pend.member_id or choice_id not in pend.candidate_ids:
            return Reply("That wasn't one of the options I offered, so I ignored it.")
        return await self._guarded(guild, member, lambda: self._bind_and_run(pend, guild, member, channel, choice=choice_id), pend.text)

    async def _guarded(self, guild, member, fn, text) -> Reply:
        app = self.app
        try:
            app.limits.check_request(guild.id, member.id)
        except RateLimited as e:
            return Reply(f"I'm getting a lot of requests from you. Try again in about {int(e.retry_after) + 1}s.")
        settings = await app.memory.settings(guild.id)
        groups = app.auth.groups(member, settings)
        if "restricted" in groups and "admin" not in groups:
            await app.audit.record(guild_id=guild.id, action="request", actor_id=member.id, status="denied",
                                   authorization_result="denied", reason="restricted group")
            return Reply("You aren't able to use me on this server.")
        if not app.limits.acquire_plan(guild.id):
            return Reply("I'm in the middle of other changes on this server. Give me a moment and ask again.")
        try:
            return await fn()
        except AIError as e:
            return Reply(e.user_message)
        except RelayError as e:
            return Reply(e.user_message)
        except Exception:
            log.exception("request failed")
            return Reply("Something went wrong on my side. I can't tell what, if anything, was changed; ask me to list recent actions to check.")
        finally:
            app.limits.release_plan(guild.id)

    async def _system(self, guild, member, channel, settings, tz_name, ctxdata, source_ids: set, no_clarify: bool = False) -> str:
        app = self.app
        tz = tp.tz_of(tz_name)
        now = tp.now().astimezone(tz)
        mems = []
        for m in (await app.memory.list_memories(guild.id, "user", member.id))[:8]:
            mems.append(f"(user) {m['data'].get('key') or 'note'}: {m['data']['text']}")
        for m in (await app.memory.list_memories(guild.id, "guild"))[:8]:
            mems.append(f"(server) {m['data'].get('key') or 'note'}: {m['data']['text']}")
        for g, ids in settings.roles.items():
            live = [guild.get_role(i) for i in ids]
            live = [r for r in live if r]
            if live:
                mems.append(f"(configured group {g}) roles: " + ", ".join(f"{r.name} (id {r.id})" for r in live))
                source_ids.update(r.id for r in live)
        for key in ("announcement_channel", "security_channel"):
            cid = getattr(settings, key)
            ch = guild.get_channel(cid) if cid else None
            if ch:
                mems.append(f"({key.replace('_', ' ')}) #{ch.name} (id {ch.id})")
                source_ids.add(ch.id)
        where = {"status": "pending"}
        if not app.auth.is_staff(member, settings):
            where["actor_id"] = member.id
        tasks = []
        for t in await app.store.find("tasks", guild_id=guild.id, where=where, order="run_at", limit=10):
            tasks.append(f"{t['id'][:6]}: {t['data']['preview']}")
        block = prompts.context_block(ctxdata["turns"], ctxdata["entities"], mems, tasks)
        return app.planner.system(guild=guild.name, requester=member.display_name, requester_id=member.id,
                                  channel=getattr(channel, "name", "unknown"), now_local=now.strftime("%A %Y-%m-%d %H:%M"),
                                  tz=tz_name, context=block, no_clarify=no_clarify, layout=server_layout(guild))

    async def _nl(self, text, guild, member, channel, source) -> Reply:
        app = self.app
        settings = await app.memory.settings(guild.id)
        tz_name = await app.memory.timezone_for(guild.id, member.id)
        ctxdata = await app.memory.context(guild.id, channel.id, member.id)
        ids = {int(x) for x in ID_RE.findall(text)}
        turns = ctxdata.get("turns") or []
        asked = bool(turns) and turns[-1].get("r") != "user" and str(turns[-1].get("t", "")).rstrip().endswith("?")
        system = await self._system(guild, member, channel, settings, tz_name, ctxdata, ids, no_clarify=asked)
        app.limits.check_ai(guild.id)
        out = await app.planner.plan(system, text)
        if out.type == "clarify" and asked:
            # one question is allowed; a second in a row never is
            app.limits.check_ai(guild.id)
            out = await app.planner.plan(system, text + "\n\n(You must plan now; clarification is not allowed.)")
        if out.type != "plan":
            await app.memory.add_turn(guild.id, channel.id, member.id, text, out.text)
            return Reply(out.text)
        pend = Pending(out, text, guild.id, member.id, channel.id, source, extra_ids=ids)
        pend.system = system
        return await self._bind_and_run(pend, guild, member, channel)

    async def _bind_and_run(self, pend: Pending, guild, member, channel, choice: int | None = None) -> Reply:
        app = self.app
        tz_name = await app.memory.timezone_for(guild.id, member.id)
        ctxdata = await app.memory.context(guild.id, channel.id, member.id)
        if choice is not None:
            pend.extra_ids.add(choice)
        env = Env(guild=guild, actor_id=member.id, channel_id=channel.id, entities=ctxdata["entities"],
                  allowed_ids=set(pend.extra_ids) | {int(e["id"]) for e in ctxdata["entities"].values()}, overrides=pend.overrides)
        env.allowed_ids |= {int(x) for x in ID_RE.findall(pend.text)}
        env.allowed_ids |= {i for i in pend.extra_ids}
        if choice is not None and pend.last_ambiguity:
            pend.overrides[pend.last_ambiguity] = choice
        try:
            plan = await app.binder.bind(pend.out, env, tz_name)
        except Ambiguous as e:
            pend.last_ambiguity = (e.kind, e.ref.casefold())
            pend.candidate_ids = {c[0] for c in e.candidates}
            noun = e.kind.replace("_", " ")
            lead = f"I couldn't find an exact {noun} called '{prompts.clean(e.ref, 40)}'. Did you mean one of these?" if e.suggestion \
                else f"More than one {noun} matches '{prompts.clean(e.ref, 40)}'. Which one?"
            return Reply(lead, ambiguity={"pending": pend, "candidates": e.candidates[:25]})
        except Unresolved as e:
            await app.memory.add_turn(guild.id, channel.id, member.id, pend.text, f"(couldn't resolve: {e})")
            return Reply(f"I couldn't resolve that: {e.user_message}.")
        except PlanError as e:
            if pend.source != "command" and pend.system and not pend.repaired:
                pend.repaired = True
                app.limits.check_ai(guild.id)
                pend.out = await app.planner.repair(pend.system, pend.text, pend.out, str(e))
                if pend.out.type != "plan":
                    return Reply(pend.out.text)
                return await self._bind_and_run(pend, guild, member, channel)
            return Reply(e.user_message)
        return await self.execute_plan(plan, pend.text, guild, member, channel, pend.source, ctxdata, pend.extra_ids | env.allowed_ids)

    def _remember_refs(self, plan: BoundPlan, ctxdata: dict) -> None:
        for s in plan.steps:
            tool = self.app.registry.tools[s.tool]
            for fname, kind in ref_fields(tool.args).items():
                key = ENTITY_KEY.get(kind)
                v, lab = s.refs.get(fname), s.labels.get(fname)
                if not key or v is None:
                    continue
                v = v[-1] if isinstance(v, list) else v
                lab = lab[-1] if isinstance(lab, list) else lab
                if isinstance(v, int):
                    ctxdata["entities"][key] = {"id": v, "name": re.sub(r"^[#@]|category |\s\(in .*\)$", "", str(lab))}

    def describe_when(self, step, tz_name: str) -> str:
        w = step.when
        if not w:
            return ""
        tz = tp.tz_of(tz_name)
        if w["kind"] == "every":
            return " " + tp.describe_rule(w["rule"], tz_name)
        if "delay_s" in w:
            return f" in {tp.fmt_duration(w['delay_s'])}"
        return " at " + tp.fmt_local(tp.from_iso(w["run_at"]), tz)

    async def execute_plan(self, plan: BoundPlan, text, guild, member, channel, source, ctxdata, allowed_ids) -> Reply:
        app = self.app
        rid = new_request_id()
        ctx = await app.make_ctx(guild, member, member.id, channel, source, plan_id=plan.plan_id, request_id=rid,
                                 entities=ctxdata["entities"], allowed_ids=set(allowed_ids))
        writes = [s for s in plan.steps if app.registry.tools[s.tool].risk != "read"]
        if writes:
            await app.audit.record(guild_id=guild.id, action="plan.created", actor_id=member.id, request_id=rid, plan_id=plan.plan_id,
                                   details={"request": scrub_text(text, 300), "source": source, "steps": [
                                       {"id": s.id, "tool": s.tool, "preview": s.preview, "scheduled": bool(s.when)} for s in plan.steps]})
        bad = await app.executor.preflight(plan, ctx)
        if bad:
            await app.audit.record(guild_id=guild.id, action="plan.denied", actor_id=member.id, request_id=rid, plan_id=plan.plan_id,
                                   status="denied", authorization_result="denied",
                                   details={"reasons": [{"step": s.id, "tool": s.tool, "reason": r} for s, r in bad]})
            msg = "I can't do that: " + "; ".join(f"{s.preview} ({r})" for s, r in bad) + "."
            await app.memory.add_turn(guild.id, channel.id, member.id, text, msg)
            return Reply(msg)
        if app.confirmations.required(plan, source, guild):
            rec = await app.confirmations.create(plan, guild_id=guild.id, requester_id=member.id, channel_id=channel.id,
                                                 request_id=rid, source=source, request_text=text)
            mins = app.cfg.confirmation.ttl_seconds // 60 or 1
            if len(plan.steps) == 1:
                head = f"Ready to {plan.steps[0].preview}{self.describe_when(plan.steps[0], ctx.tz_name)}."
                lines = [head, f"Confirm to go ahead, or cancel to drop it (this expires in {mins} min)."]
            else:
                lines = ["Here's the plan:"] + [f"• {s.preview.replace(chr(10), chr(10) + '  ')}{self.describe_when(s, ctx.tz_name)}" for s in plan.steps]
                lines.append(f"Confirm to run it, or cancel to drop it (this expires in {mins} min).")
            self._remember_refs(plan, ctxdata)
            await app.memory.save_context(guild.id, channel.id, member.id, ctxdata)
            return Reply("\n".join(lines), confirmation={"id": rec["id"], "requester": member.id})
        res = await app.executor.run(plan, ctx)
        return await self._finish(res, ctx, text, ctxdata, plan)

    async def _finish(self, res: PlanResult, ctx, text: str, ctxdata: dict, plan: BoundPlan) -> Reply:
        app = self.app
        file = pager = None
        for o in res.outcomes:
            r = o.result
            if not r:
                continue
            if r.summarize and r.data.get("records") is not None:
                try:
                    app.limits.check_ai(ctx.guild.id)
                    body = await app.planner.compose(r.data.get("question", text), r.data["records"])
                    o.text = f"{r.text}\n{body}" if r.text and not r.text.startswith("0 ") else body
                except (AIError, RateLimited):
                    o.text = r.text + "\n" + "\n".join(r.data["records"][:12])[:1500]
            if r.data.get("file"):
                file = r.data["file"]
            if r.data.get("lines") and len(r.data["lines"]) > 12:
                pager = r.data["lines"]
        reply_text = res.render()[:1950] or "Done."
        self._remember_refs(plan, ctxdata)
        ctxdata["entities"].update(ctx.entities)
        await app.memory.add_turn(ctx.guild.id, ctx.channel_id or 0, ctx.actor_id, text, reply_text, ctxdata["entities"])
        return Reply(reply_text, file=file, pager=pager)

    async def confirm(self, cid: str, user: discord.Member, guild: discord.Guild, channel, approve: bool) -> Reply:
        app = self.app
        try:
            rec, plan = await app.confirmations.decide(cid, user.id, guild.id, approve)
        except RelayError as e:
            return Reply(e.user_message, ephemeral=True)
        if not approve:
            return Reply("Cancelled. Nothing was changed.")
        if not app.limits.acquire_plan(guild.id):
            await app.confirmations.mark_executed(cid, guild.id, False)
            return Reply("I'm busy with other changes on this server. Ask again in a moment.")
        try:
            d = rec["data"]
            ctx = await app.make_ctx(guild, user, user.id, channel, d["source"], plan_id=plan.plan_id, request_id=d["request_id"], confirmed=True)
            bad = await app.executor.preflight(plan, ctx)
            if bad:
                await app.audit.record(guild_id=guild.id, action="plan.denied", actor_id=user.id, request_id=d["request_id"],
                                       plan_id=plan.plan_id, status="denied", authorization_result="denied", confirmation_status="approved",
                                       details={"reasons": [{"step": s.id, "reason": r} for s, r in bad], "phase": "revalidation"})
                await app.confirmations.mark_executed(cid, guild.id, False)
                return Reply("Things changed since I asked, and now I can't do that: " + "; ".join(f"{s.preview} ({r})" for s, r in bad) + ".")
            res = await app.executor.run(plan, ctx)
            await app.confirmations.mark_executed(cid, guild.id, res.ok)
            ctxdata = await app.memory.context(guild.id, channel.id, user.id)
            return await self._finish(res, ctx, d["request"], ctxdata, plan)
        except RelayError as e:
            return Reply(e.user_message)
        except Exception:
            log.exception("confirmed execution failed")
            return Reply("Something went wrong while carrying that out. Check recent changes before retrying.")
        finally:
            app.limits.release_plan(guild.id)
