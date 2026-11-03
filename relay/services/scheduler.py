from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import discord

from relay.services.binder import BoundPlan, BoundStep
from relay.storage.base import new_rec
from relay.tools.base import Ctx
from relay.utils import time_parser as tp
from relay.utils.errors import ExecError, RelayError

if TYPE_CHECKING:
    from relay.app import App

log = logging.getLogger(__name__)

QUIET_TOOLS = {"send_message", "remind", "moderation_summary"}
TRANSIENT = {"retryable", "rate_limited", "ai_error"}


class Scheduler:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg.scheduler
        self.instance = uuid.uuid4().hex[:8]
        self._loop: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._loop is None:
            self._stop.clear()
            self._loop = asyncio.create_task(self._forever(), name="relay-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._loop:
            await self._loop
            self._loop = None
        if self._running:
            await asyncio.wait(self._running, timeout=20)

    async def _forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), self.cfg.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def create(self, step: BoundStep, tool, args, values: dict, ctx: Ctx) -> tuple[str, str]:
        store = self.app.store
        pending = await store.count("tasks", guild_id=ctx.guild.id, where={"status": "pending"})
        if pending >= self.cfg.max_tasks_per_guild:
            raise ExecError("task cap", user_message=f"This server already has {pending} scheduled tasks (limit {self.cfg.max_tasks_per_guild}).")
        w = dict(step.when)
        t = tp.now()
        if "delay_s" in w:
            run_at = t + timedelta(seconds=w["delay_s"])
        else:
            run_at = tp.from_iso(w["run_at"])
            if run_at < t - timedelta(seconds=5):
                raise ExecError("time passed", user_message="That time passed while waiting. Ask again with a new time.")
        tid = uuid.uuid4().hex[:10]
        tz = tp.tz_of(ctx.tz_name)
        data = {"tool": step.tool, "args": step.args, "refs": values, "labels": step.labels, "preview": step.preview,
                "when": {"kind": w["kind"], "rule": w.get("rule"), "tz": w.get("tz", ctx.tz_name)},
                "channel_id": ctx.channel_id, "creator_id": ctx.actor_id, "plan_id": ctx.plan_id,
                "attempts": 0, "failures": 0, "runs": 0, "last_error": None, "last_result": None}
        first_target = next((v for x in values.values() for v in (x if isinstance(x, list) else [x])), None)
        await store.put("tasks", new_rec(tid, ctx.guild.id, data, ts=tp.iso(t), actor_id=ctx.actor_id,
                                         target_id=first_target if isinstance(first_target, int) else None,
                                         kind=step.tool, status="pending", run_at=tp.iso(run_at)))
        await self.app.audit.record(guild_id=ctx.guild.id, action="task.create", actor_id=ctx.actor_id,
                                    request_id=ctx.request_id, plan_id=ctx.plan_id, task_id=tid, resource_type="task",
                                    resource_id=tid, authorization_result="allowed",
                                    confirmation_status="approved" if ctx.confirmed else "not_required",
                                    details={"tool": step.tool, "preview": step.preview, "run_at": tp.iso(run_at), "recurring": w["kind"] == "every"})
        when = tp.describe_rule(w["rule"], w.get("tz", ctx.tz_name)) + f", next {tp.fmt_local(run_at, tz)}" if w["kind"] == "every" \
            else tp.fmt_local(run_at, tz)
        return f"Scheduled for {when}: {step.preview} (task {tid[:6]}).", tid

    async def cancel(self, guild_id: int, ref: str, actor_id: int, is_admin: bool) -> tuple[dict | None, str]:
        rec = await self.find(guild_id, ref)
        if rec is None:
            return None, "I couldn't find a task matching that."
        if rec["actor_id"] != actor_id and not is_admin:
            return rec, "Only the person who scheduled it, or an administrator, can cancel that task."
        ok = await self.app.store.update_if("tasks", rec["id"], {"status": "pending"}, {"status": "cancelled", "run_at": None}, guild_id)
        if not ok:
            return rec, f"That task is {rec['status']} and can't be cancelled."
        await self.app.audit.record(guild_id=guild_id, action="task.cancel", actor_id=actor_id, task_id=rec["id"],
                                    resource_type="task", resource_id=rec["id"], authorization_result="allowed",
                                    details={"preview": rec["data"]["preview"]})
        return rec, f"Cancelled: {rec['data']['preview']} (task {rec['id'][:6]})."

    async def find(self, guild_id: int, ref: str) -> dict | None:
        ref = ref.strip().lstrip("#")
        rec = await self.app.store.get("tasks", ref, guild_id)
        if rec or len(ref) < 4:
            return rec
        rows = await self.app.store.find("tasks", guild_id=guild_id, limit=500, desc=True)
        hits = [r for r in rows if r["id"].startswith(ref)]
        return hits[0] if len(hits) == 1 else None

    async def tick(self) -> None:
        store = self.app.store
        await self._recover_stale()
        now_s = tp.iso(tp.now())
        due = await store.find("tasks", where={"status": "pending"}, due_before=now_s, order="run_at", limit=self.cfg.batch_size)
        for rec in due:
            if len(self._running) >= self.cfg.max_concurrent:
                break
            claimed = await store.update_if("tasks", rec["id"], {"status": "pending"},
                                            {"status": "running", "data": {"claimed_at": now_s, "claimed_by": self.instance}},
                                            rec["guild_id"])
            if not claimed:
                continue
            t = asyncio.create_task(self._run(rec), name=f"task-{rec['id']}")
            self._running.add(t)
            t.add_done_callback(self._running.discard)

    async def _recover_stale(self) -> None:
        store = self.app.store
        cutoff = tp.iso(tp.now() - timedelta(seconds=self.cfg.stale_claim_seconds))
        for rec in await store.find("tasks", where={"status": "running"}, limit=200):
            if rec["data"].get("claimed_at", "") >= cutoff or rec["id"] in {t.get_name()[5:] for t in self._running}:
                continue
            tool = self.app.registry.tools.get(rec["data"]["tool"])
            safe = tool is not None and (tool.idempotent or tool.risk in ("read", "low"))
            new = {"status": "pending"} if safe else {"status": "uncertain", "run_at": None}
            if await store.update_if("tasks", rec["id"], {"status": "running"}, new, rec["guild_id"]):
                await self.app.audit.record(guild_id=rec["guild_id"], action="task.recovered", actor_type="relay", task_id=rec["id"],
                                            status="success" if safe else "uncertain",
                                            details={"result": "requeued" if safe else "interrupted mid-run; outcome unknown"})

    async def _finish(self, rec: dict, status: str, run_at: str | None, data: dict) -> None:
        await self.app.store.update_if("tasks", rec["id"], {"status": "running"},
                                       {"status": status, "run_at": run_at, "data": data}, rec["guild_id"])

    async def _run(self, rec: dict) -> None:
        app = self.app
        data = rec["data"]
        gid, tid = rec["guild_id"], rec["id"]
        now = tp.now()
        when = data["when"]
        recurring = when["kind"] == "every"
        late = (now - tp.from_iso(rec["run_at"])).total_seconds()
        try:
            if late > self.cfg.late_grace_seconds:
                if recurring:
                    nxt = tp.next_occurrence(when["rule"], now, tp.tz_of(when["tz"]))
                    await self._finish(rec, "pending", tp.iso(nxt), {"last_error": f"skipped a run that was {int(late)}s late"})
                else:
                    await self._finish(rec, "missed", None, {"last_error": f"{int(late)}s late; past the grace period"})
                await app.audit.record(guild_id=gid, action="task.missed", actor_type="scheduler", task_id=tid, status="failure",
                                       error_code="missed", details={"late_seconds": int(late), "recurring": recurring})
                await self._notify(rec, f"Skipped a scheduled run of {data['preview']} because the bot was offline too long.")
                return
            guild = app.bot.get_guild(gid)
            if guild is None:
                raise ExecError("guild unavailable", code="retryable", user_message="The server wasn't available.")
            actor = guild.get_member(data["creator_id"])
            if actor is None:
                try:
                    actor = await guild.fetch_member(data["creator_id"])
                except discord.NotFound:
                    actor = None
            ctx = await app.make_ctx(guild, actor, data["creator_id"], guild.get_channel(data["channel_id"]), "scheduler",
                                     plan_id=data["plan_id"], task_id=tid, actor_type="scheduler")
            step = BoundStep("s1", data["tool"], data["args"], data["refs"], data["labels"], [], None, data["preview"])
            res = await app.executor.run(BoundPlan([step], plan_id=data["plan_id"]), ctx)
            oc = res.outcomes[0]
        except RelayError as e:
            class _O:
                status, code, text = "failure", e.code, e.user_message
            oc = _O()
        except Exception as e:
            log.exception("task %s crashed", tid)
            class _O:
                status, code, text = "uncertain", "internal", f"Crashed: {type(e).__name__}"
            oc = _O()
        await self._settle(rec, oc, recurring, now)

    async def _settle(self, rec: dict, oc, recurring: bool, now) -> None:
        data = rec["data"]
        when = data["when"]
        attempts = data.get("attempts", 0)
        patch = {"last_result": oc.status, "last_error": None if oc.status == "success" else oc.text[:200],
                 "last_run": tp.iso(now)}
        transient = oc.status == "failure" and getattr(oc, "code", None) in TRANSIENT
        if transient and attempts < self.cfg.retry_limit:
            delay = self.cfg.backoff_seconds * (2 ** attempts)
            patch["attempts"] = attempts + 1
            await self._finish(rec, "pending", tp.iso(now + timedelta(seconds=delay)), patch)
            return
        patch["attempts"] = 0
        if oc.status == "success":
            patch["runs"] = data.get("runs", 0) + 1
            patch["failures"] = 0
        else:
            patch["failures"] = data.get("failures", 0) + 1
        if recurring:
            if patch["failures"] >= self.cfg.max_consecutive_failures:
                await self._finish(rec, "disabled", None, patch)
                await self._notify(rec, f"Disabled recurring task {rec['id'][:6]} ({data['preview']}) after repeated failures.")
                return
            nxt = tp.next_occurrence(when["rule"], now, tp.tz_of(when["tz"]))
            await self._finish(rec, "pending", tp.iso(nxt), patch)
        else:
            final = {"success": "done", "uncertain": "uncertain"}.get(oc.status, "failed")
            await self._finish(rec, final, None, patch)
        if oc.status != "success":
            await self._notify(rec, f"Scheduled task {rec['id'][:6]} ({data['preview']}): {oc.text}")
        elif data["tool"] not in QUIET_TOOLS:
            await self._notify(rec, oc.text)

    async def _notify(self, rec: dict, text: str) -> None:
        try:
            ch = self.app.bot.get_channel(rec["data"].get("channel_id") or 0)
            if ch is not None:
                await ch.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            log.warning("couldn't post scheduler notice", exc_info=True)
