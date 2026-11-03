from __future__ import annotations

from typing import Literal

from pydantic import Field

from relay.tools.base import Args, Ctx, Refs, Result, tool
from relay.tools.util import esc
from relay.utils import time_parser as tp
from relay.utils.errors import ExecError


class ListTasks(Args):
    status: Literal["pending", "all"] = "pending"
    mine_only: bool = False


@tool("list_tasks", "list scheduled tasks (yours, or everyone's if you're staff)", ListTasks, risk="read", access="open",
      tags=("schedule",), describe=lambda a, r: "list scheduled tasks")
async def list_tasks(ctx: Ctx, a: ListTasks, r: Refs) -> Result:
    where = {} if a.status == "all" else {"status": ["pending", "running"]}
    staff = ctx.app.auth.is_staff(ctx.actor, ctx.settings)
    if a.mine_only or not staff:
        where["actor_id"] = ctx.actor_id
    rows = await ctx.app.store.find("tasks", guild_id=ctx.guild.id, where=where, order="run_at", limit=30)
    if not rows:
        return Result("No scheduled tasks.")
    tz = tp.tz_of(ctx.tz_name)
    lines = []
    for t in rows:
        nxt = tp.fmt_local(tp.from_iso(t["run_at"]), tz) if t["run_at"] else "-"
        every = f" [{tp.describe_rule(t['data']['when']['rule'], ctx.tz_name)}]" if t["data"]["when"]["kind"] == "every" else ""
        lines.append(f"{t['id'][:6]} | {t['status']} | {nxt}{every} | {esc(t['data']['preview'], 90)} | by <@{t['actor_id']}>")
    return Result("\n".join(lines), data={"lines": lines})


class CancelTask(Args):
    task: str = Field(min_length=3, max_length=40, description="task id (or the first 6 characters)")


@tool("cancel_task", "cancel a scheduled task by id", CancelTask, risk="low", access="open", tags=("schedule",), schedulable=False,
      describe=lambda a, r: f"cancel task {a.task}")
async def cancel_task(ctx: Ctx, a: CancelTask, r: Refs) -> Result:
    rec, msg = await ctx.app.scheduler.cancel(ctx.guild.id, a.task, ctx.actor_id, ctx.is_admin())
    if rec is None or not msg.startswith("Cancelled"):
        raise ExecError("cancel failed", user_message=msg)
    return Result(msg, resource_type="task", details={"task": rec["id"]}, event=("task.cancel", "staff"))
