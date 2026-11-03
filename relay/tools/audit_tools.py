from __future__ import annotations

import json

from pydantic import Field

from relay.tools.base import Args, Ctx, Refs, Result, ref, tool
from relay.tools.util import esc
from relay.utils import time_parser as tp


def fmt_audit(row: dict, tz) -> str:
    who = f"<@{row['actor_id']}>" if row.get("actor_id") else row["actor_type"]
    tgt = f" -> <@{row['target_id']}>" if row.get("target_id") else (f" -> {row['resource_type']} {esc(row['resource_id'], 24)}" if row.get("resource_id") else "")
    err = f" [{row['error_code']}]" if row.get("error_code") else ""
    prev = (row.get("details") or {}).get("preview")
    extra = f" | {esc(prev, 90)}" if prev else (f" | {esc(row['reason'], 80)}" if row.get("reason") else "")
    return f"{tp.fmt_local(tp.from_iso(row['ts']), tz)} {row['action']} {row['status']}{err} by {who}{tgt}{extra} (id {row['event_id'][:8]})"


class AuditSearch(Args):
    actor: str | None = ref("user", None)
    target: str | None = ref("user", None)
    action: str | None = Field(None, max_length=60, description="tool or event name, e.g. ban_member, set_channel_permissions, task.create")
    status: str | None = Field(None, description="success|failure|partial|denied|uncertain|started")
    period: str | None = None
    plan: str | None = Field(None, max_length=20)
    task: str | None = Field(None, max_length=20)
    limit: int = Field(10, ge=1, le=50)
    offset: int = Field(0, ge=0, le=5000)


@tool("audit_search", "search the audit log (actions Relay performed or observed) by actor, target, action, status, period, plan or task", AuditSearch,
      risk="read", tags=("audit", "sensitive"), describe=lambda a, r: "search the audit log")
async def audit_search(ctx: Ctx, a: AuditSearch, r: Refs) -> Result:
    since = until = None
    if a.period:
        s, u = tp.parse_period(a.period, tp.tz_of(ctx.tz_name))
        since, until = tp.iso(s), tp.iso(u)
    task_id = None
    if a.task:
        t = await ctx.app.scheduler.find(ctx.guild.id, a.task)
        task_id = t["id"] if t else a.task
    rows = await ctx.app.audit.search(ctx.guild.id, actor_id=r.one("actor"), target_id=r.one("target"), action=a.action,
                                      status=a.status, since=since, until=until, plan_id=a.plan, task_id=task_id,
                                      limit=a.limit, offset=a.offset)
    if not rows:
        return Result("No audit entries match that.")
    tz = tp.tz_of(ctx.tz_name)
    lines = [fmt_audit(x, tz) for x in rows]
    return Result("\n".join(lines)[:1900], data={"lines": lines, "count": len(rows)})


class AuditExport(Args):
    period: str = "today"


@tool("audit_export", "export audit entries for a period as a JSON Lines file", AuditExport, risk="read", access="admin",
      tags=("audit", "sensitive"), describe=lambda a, r: f"export the audit log ({a.period})")
async def audit_export(ctx: Ctx, a: AuditExport, r: Refs) -> Result:
    s, u = tp.parse_period(a.period, tp.tz_of(ctx.tz_name))
    data, n = await ctx.app.audit.export_jsonl(ctx.guild.id, tp.iso(s), tp.iso(u))
    name = f"audit-{ctx.guild.id}-{s.strftime('%Y%m%d')}.jsonl"
    return Result(f"Exported {n} audit entries ({a.period}).", data={"file": (name, data)}, details={"entries": n, "period": a.period})


class AuditVerify(Args):
    pass


@tool("audit_verify", "check the audit log's hash chain for gaps or edits", AuditVerify, risk="read", access="admin",
      tags=("audit", "sensitive"), describe=lambda a, r: "verify the audit chain")
async def audit_verify(ctx: Ctx, a: AuditVerify, r: Refs) -> Result:
    res = await ctx.app.audit.verify(ctx.guild.id)
    note = "" if res["keyed"] else " (no AUDIT_HMAC_KEY set, so this only catches accidental damage or partial edits, not a determined rewrite)"
    if res["ok"]:
        return Result(f"Checked {res['checked']} entries; the chain is intact{note}.")
    first = res["problems"][0]
    return Result(f"Checked {res['checked']} entries and found {len(res['problems'])} problem(s); first: {first['problem']} at event {first['event_id'][:8]}{note}.", ok=False)
