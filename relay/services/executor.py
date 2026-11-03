from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp
import discord

from relay.services.binder import BoundPlan, BoundStep, order_steps
from relay.tools.base import Ctx, Refs, Result, mark
from relay.utils.errors import Denied, ExecError, RelayError, Uncertain

if TYPE_CHECKING:
    from relay.app import App

log = logging.getLogger(__name__)

STEP_TIMEOUT = 60


@dataclass
class Outcome:
    step_id: str
    tool: str
    status: str
    text: str
    result: Result | None = None
    preview: str = ""
    code: str | None = None


@dataclass
class PlanResult:
    plan_id: str
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.status in ("success", "scheduled") for o in self.outcomes)

    @property
    def summarize(self) -> bool:
        return any(o.result and o.result.summarize for o in self.outcomes)

    def render(self) -> str:
        outs = [o for o in self.outcomes if o.text]
        if len(self.outcomes) == 1:
            return outs[0].text if outs else ""
        bad = [o for o in self.outcomes if o.status not in ("success", "scheduled")]
        done = len(self.outcomes) - len(bad)
        head = ("All set. Here's what I did:" if not bad else
                "That didn't go through:" if not done else "Partly done. Here's where things stand:")
        lines = [head] + ["• " + o.text.replace("\n", "\n  ") for o in outs]
        if bad:
            lines.append(f"{done} of {len(self.outcomes)} steps completed; the rest didn't happen or need a look.")
        return "\n".join(lines)


def classify(e: BaseException) -> tuple[str, str, str]:
    if isinstance(e, Denied):
        return "denied", e.code, e.user_message
    if isinstance(e, Uncertain) or isinstance(e, (asyncio.TimeoutError, aiohttp.ClientError)):
        return "uncertain", "uncertain", "The request may or may not have gone through."
    if isinstance(e, discord.Forbidden):
        return "failure", "forbidden", "Discord refused that (missing permission or role hierarchy)."
    if isinstance(e, discord.NotFound):
        return "failure", "not_found", "That no longer exists."
    if isinstance(e, discord.HTTPException):
        if e.status >= 500:
            return "uncertain", "discord_5xx", "Discord had a server error, so the result is uncertain."
        return "failure", f"http_{e.status}", f"Discord rejected it: {str(e.text)[:150]}"
    if isinstance(e, RelayError):
        return "failure", e.code, e.user_message
    if isinstance(e, OSError):
        return "uncertain", "io_error", "A network error interrupted this."
    return "failure", "internal", "Something went wrong on my side."


class Executor:
    def __init__(self, app: "App"):
        self.app = app

    def resolve_refs(self, step: BoundStep, ctx: Ctx) -> dict:
        out = {}
        for k, v in step.refs.items():
            def one(x):
                if isinstance(x, str) and x.startswith("$"):
                    r = ctx.results.get(x[1:])
                    if r is None or r.resource_id is None:
                        raise ExecError(f"dependency {x[1:]} produced nothing", user_message="A step this depends on didn't produce what it needed.")
                    return r.resource_id
                return int(x)
            out[k] = [one(x) for x in v] if isinstance(v, list) else one(v)
        return out

    async def check(self, step: BoundStep, ctx: Ctx, refs_values: dict | None = None) -> str | None:
        tool = self.app.registry.get(step.tool)
        d = self.app.auth.decide(tool, ctx.actor, ctx.settings)
        if not d.ok:
            return d.reason
        if tool.guard:
            deferred = any(isinstance(x, str) or (isinstance(x, list) and any(isinstance(i, str) for i in x))
                           for x in (refs_values or step.refs).values())
            if deferred:
                return None
            try:
                args = tool.args.model_validate(step.args)
                await tool.guard(ctx, args, Refs(refs_values or step.refs, step.labels))
            except Denied as e:
                return e.user_message
        return None

    async def preflight(self, plan: BoundPlan, ctx: Ctx) -> list[tuple[BoundStep, str]]:
        bad = []
        for s in plan.steps:
            reason = await self.check(s, ctx)
            if reason:
                bad.append((s, reason))
        return bad

    async def run(self, plan: BoundPlan, ctx: Ctx) -> PlanResult:
        res = PlanResult(plan.plan_id)
        failed: set[str] = set()
        for s in order_steps(plan.steps):
            blockers = [d for d in s.depends_on if d in failed]
            if blockers:
                failed.add(s.id)
                res.outcomes.append(Outcome(s.id, s.tool, "skipped", f"Skipped {s.preview} because an earlier step it needs didn't finish.", None, s.preview))
                await self.app.audit.record(guild_id=ctx.guild.id, action=s.tool, status="failure", actor_id=ctx.actor_id,
                                            actor_type=ctx.actor_type, request_id=ctx.request_id, plan_id=ctx.plan_id,
                                            task_id=ctx.task_id, error_code="dependency_failed", details={"step": s.id, "skipped_for": blockers})
                continue
            oc = await self._step(s, ctx)
            res.outcomes.append(oc)
            if oc.status not in ("success", "scheduled"):
                failed.add(s.id)
        return res

    async def _step(self, s: BoundStep, ctx: Ctx) -> Outcome:
        app = self.app
        tool = app.registry.get(s.tool)
        gid = ctx.guild.id
        base = dict(guild_id=gid, action=tool.name, actor_id=ctx.actor_id, actor_type=ctx.actor_type,
                    request_id=ctx.request_id, plan_id=ctx.plan_id, task_id=ctx.task_id,
                    confirmation_status="approved" if ctx.confirmed else "not_required")
        started = time.monotonic()
        try:
            args = tool.args.model_validate(s.args)
            values = self.resolve_refs(s, ctx)
        except RelayError as e:
            await app.audit.record(**base, status="failure", error_code=e.code, details={"step": s.id})
            return Outcome(s.id, s.tool, "failure", f"{s.preview}: {e.user_message}", None, s.preview)
        except Exception:
            log.exception("step setup failed")
            return Outcome(s.id, s.tool, "failure", f"{s.preview}: invalid step.", None, s.preview)
        refs = Refs(values, s.labels)
        reason = await self.check(s, ctx, values)
        if reason:
            await app.audit.record(**base, status="denied", authorization_result="denied", reason=reason,
                                   details={"step": s.id, "preview": s.preview})
            return Outcome(s.id, s.tool, "denied", f"Not allowed: {s.preview} ({reason}).", None, s.preview, "denied")
        base["authorization_result"] = "allowed"
        sensitive = "sensitive" in tool.tags
        if s.when and ctx.source != "scheduler":
            try:
                text, task_id = await app.scheduler.create(s, tool, args, values, ctx)
            except RelayError as e:
                await app.audit.record(**base, status="failure", error_code=e.code, details={"step": s.id, "when": s.when})
                return Outcome(s.id, s.tool, "failure", e.user_message, None, s.preview, e.code)
            return Outcome(s.id, s.tool, "scheduled", text, Result(text=text), s.preview)
        if tool.risk != "read":
            try:
                await app.audit.record(**base, status="started", strict=app.audit.cfg.fail_closed and tool.risk in ("medium", "high"),
                                       details={"step": s.id, "preview": s.preview})
            except RelayError as e:
                return Outcome(s.id, s.tool, "failure", e.user_message, None, s.preview)
            mark([v for x in values.values() for v in (x if isinstance(x, list) else [x])])
        try:
            result = await asyncio.wait_for(tool.run(ctx, args, refs), tool.timeout or STEP_TIMEOUT)
            status, code, text = result.status, None, result.text
        except Exception as e:
            status, code, text = classify(e)
            if status == "failure" and code == "internal":
                log.exception("tool %s crashed", tool.name)
            result = None
            if status == "uncertain" and tool.verify:
                try:
                    v = await tool.verify(ctx, args, refs)
                except Exception:
                    v = None
                if v is True:
                    status, text, code = "success", f"{s.preview}: done (confirmed after a network error).", None
                elif v is False:
                    status, text, code = "failure", f"{s.preview}: didn't go through.", "not_applied"
            if status == "uncertain":
                text = f"{s.preview}: {text} Check the server before retrying."
            elif status != "success":
                text = f"{s.preview}: {text}"
        ms = int((time.monotonic() - started) * 1000)
        if result is not None:
            mark(result.touched + ([result.resource_id] if result.resource_id else []))
            ctx.results[s.id] = result
            ctx.entities.update(result.entities)
        elif status == "success":
            ctx.results[s.id] = Result(text=text)
        if tool.risk != "read" or sensitive:
            details = {"step": s.id, "preview": s.preview, **(result.details if result else {})}
            await app.audit.record(**base, status=status, error_code=code, duration_ms=ms,
                                   resource_type=result.resource_type if result else None,
                                   resource_id=result.resource_id if result else None,
                                   target_id=result.items[0].target_id if result and len(result.items) == 1 else None, details=details)
            if result and len(result.items) > 1:
                for it in result.items:
                    await app.audit.record(**base, status="success" if it.ok else "failure", target_id=it.target_id,
                                           error_code=it.error_code, details={"step": s.id, "item": it.text[:200]})
            if result and result.event and status in ("success", "partial"):
                etype, vis = result.event
                await app.memory.record_event(gid, etype, actor_id=ctx.actor_id, target_id=result.items[0].target_id if result.items else None,
                                              resource_type=result.resource_type, resource_id=result.resource_id,
                                              channel_id=result.resource_id if result.resource_type == "channel" else None,
                                              visibility=vis, details=result.details, plan_id=ctx.plan_id,
                                              source="scheduler" if ctx.source == "scheduler" else "relay")
        return Outcome(s.id, s.tool, status, text, result, s.preview, code)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
