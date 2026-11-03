from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from relay.services.binder import BoundPlan
from relay.services.scheduler import Scheduler
from relay.utils import time_parser as tp
from relay.utils.errors import ExecError
from tests.helpers import CALLS, ctx_for, step, test_tools, world  # noqa: F401


async def schedule(app, w, tool="t_make", args=None, when=None, member=None, refs=None):
    member = member or w.admin
    ctx = await ctx_for(app, w, member, plan_id="pl")
    when = when or {"kind": "once", "run_at": tp.iso(tp.now()), "delay_s": 0.0}
    s = step("s1", tool, args or {"name": "job"}, refs=refs, when=when)
    res = await app.executor.run(BoundPlan([s], plan_id="pl"), ctx)
    assert res.outcomes[0].status == "scheduled", res.render()
    return (await app.store.find("tasks", guild_id=w.g.id, order="ts", desc=True))[0]


async def drain(app):
    await app.scheduler.tick()
    if app.scheduler._running:
        await asyncio.wait(app.scheduler._running)


async def force_due(app, task, seconds_ago=0):
    await app.store.update_if("tasks", task["id"], {}, {"run_at": tp.iso(tp.now() - timedelta(seconds=seconds_ago))}, task["guild_id"])


async def test_due_task_runs_once_with_audit_and_completes(app, world):
    t = await schedule(app, world)
    await drain(app)
    assert CALLS == [("make", "job")]
    t = await app.store.get("tasks", t["id"])
    assert t["status"] == "done" and t["data"]["runs"] == 1 and t["data"]["last_result"] == "success"
    await drain(app)
    assert len(CALLS) == 1
    rows = await app.audit.search(world.g.id, task_id=t["id"], limit=20)
    assert {"task.create", "t_make"} <= {r["action"] for r in rows}
    assert any(r["actor_type"] == "scheduler" and r["actor_id"] == world.admin.id for r in rows if r["action"] == "t_make")


async def test_future_task_waits(app, world):
    t = await schedule(app, world, when={"kind": "once", "run_at": tp.iso(tp.now() + timedelta(hours=1)), "delay_s": 3600})
    await drain(app)
    assert CALLS == [] and (await app.store.get("tasks", t["id"]))["status"] == "pending"


async def test_concurrent_schedulers_never_double_run(app, world):
    t = await schedule(app, world)
    other = Scheduler(app)
    await asyncio.gather(app.scheduler.tick(), other.tick(), app.scheduler.tick(), other.tick())
    pending = list(app.scheduler._running | other._running)
    if pending:
        await asyncio.wait(pending)
    assert CALLS == [("make", "job")]


async def test_claim_is_atomic_at_storage_level(app, world):
    t = await schedule(app, world)
    ok = await asyncio.gather(*[app.store.update_if("tasks", t["id"], {"status": "pending"}, {"status": "running"}, world.g.id) for _ in range(5)])
    assert sum(ok) == 1


async def test_recurring_reschedules_from_now_without_backlog(app, world):
    rule = {"freq": "interval", "interval": "10 minutes"}
    t = await schedule(app, world, when={"kind": "every", "rule": rule, "run_at": tp.iso(tp.now()), "tz": "UTC"})
    await force_due(app, t, seconds_ago=3600 * 5)
    await app.store.update_if("tasks", t["id"], {}, {}, world.g.id)
    app.cfg.scheduler.late_grace_seconds = 10 * 3600
    await drain(app)
    t2 = await app.store.get("tasks", t["id"])
    assert len(CALLS) == 1 and t2["status"] == "pending"
    nxt = tp.from_iso(t2["run_at"])
    assert tp.now() < nxt <= tp.now() + timedelta(minutes=10, seconds=5) and t2["data"]["runs"] == 1
    await drain(app)
    assert len(CALLS) == 1


async def test_too_late_one_shot_is_marked_missed_not_run(app, world):
    t = await schedule(app, world)
    app.cfg.scheduler.late_grace_seconds = 60
    await force_due(app, t, seconds_ago=7200)
    await drain(app)
    assert CALLS == [] and (await app.store.get("tasks", t["id"]))["status"] == "missed"
    assert any(r["action"] == "task.missed" for r in await app.audit.search(world.g.id))


async def test_too_late_recurring_skips_to_next_slot(app, world):
    rule = {"freq": "interval", "interval": "1 hour"}
    t = await schedule(app, world, when={"kind": "every", "rule": rule, "run_at": tp.iso(tp.now()), "tz": "UTC"})
    app.cfg.scheduler.late_grace_seconds = 60
    await force_due(app, t, seconds_ago=7200)
    await drain(app)
    t2 = await app.store.get("tasks", t["id"])
    assert CALLS == [] and t2["status"] == "pending" and tp.from_iso(t2["run_at"]) > tp.now()


async def test_creator_loses_permission_task_is_denied_at_run_time(app, world):
    t = await schedule(app, world, tool="t_staff", member=world.admin)
    world.g.members.remove(world.admin)
    world.admin.roles = [world.g.default_role]
    world.admin.guild_permissions = type(world.admin.guild_permissions)(0)
    world.g.members.append(world.admin)
    from tests.helpers import REGISTRY
    await drain(app)
    t = await app.store.get("tasks", t["id"])
    assert CALLS == [] and t["status"] == "failed" and "restricted" not in t["data"]["last_error"]
    assert any(r["status"] == "denied" for r in await app.audit.search(world.g.id, task_id=t["id"]))


async def test_creator_left_server_is_denied(app, world):
    t = await schedule(app, world, tool="t_staff")
    world.g.members.remove(world.admin)
    world.g.fetch_member.side_effect = None
    import discord
    from unittest.mock import MagicMock
    world.g.fetch_member.side_effect = discord.NotFound(MagicMock(status=404), "gone")
    await drain(app)
    assert CALLS == [] and (await app.store.get("tasks", t["id"]))["status"] == "failed"


async def test_transient_failure_retries_with_backoff_then_gives_up(app, world):
    app.cfg.scheduler.retry_limit = 2
    app.cfg.scheduler.backoff_seconds = 30
    t = await schedule(app, world, tool="t_flaky")
    await drain(app)
    t1 = await app.store.get("tasks", t["id"])
    assert t1["status"] == "pending" and t1["data"]["attempts"] == 1
    assert tp.now() + timedelta(seconds=20) < tp.from_iso(t1["run_at"]) < tp.now() + timedelta(seconds=40)
    await force_due(app, t1)
    await drain(app)
    t2 = await app.store.get("tasks", t["id"])
    assert t2["data"]["attempts"] == 2 and tp.from_iso(t2["run_at"]) > tp.now() + timedelta(seconds=50)
    await force_due(app, t2)
    await drain(app)
    t3 = await app.store.get("tasks", t["id"])
    assert t3["status"] == "failed" and len(CALLS) == 3


async def test_destructive_task_is_not_retried_after_uncertain_outcome(app, world):
    t = await schedule(app, world, tool="t_net")
    await drain(app)
    t = await app.store.get("tasks", t["id"])
    assert t["status"] == "uncertain" and len(CALLS) == 1
    await drain(app)
    assert len(CALLS) == 1


async def test_restart_recovery_requeues_safe_and_flags_destructive(app, world):
    safe = await schedule(app, world, tool="t_make")
    risky = await schedule(app, world, tool="t_net")
    stale = tp.iso(tp.now() - timedelta(hours=1))
    for t in (safe, risky):
        await app.store.update_if("tasks", t["id"], {"status": "pending"}, {"status": "running", "data": {"claimed_at": stale}}, world.g.id)
    await app.scheduler._recover_stale()
    assert (await app.store.get("tasks", safe["id"]))["status"] == "pending"
    assert (await app.store.get("tasks", risky["id"]))["status"] == "uncertain"
    assert CALLS == []


async def test_live_running_task_is_not_recovered(app, world):
    t = await schedule(app, world)
    await app.store.update_if("tasks", t["id"], {"status": "pending"}, {"status": "running", "data": {"claimed_at": tp.iso(tp.now())}}, world.g.id)
    await app.scheduler._recover_stale()
    assert (await app.store.get("tasks", t["id"]))["status"] == "running"


async def test_tasks_survive_a_new_scheduler_instance(app, world):
    t = await schedule(app, world)
    fresh = Scheduler(app)
    await fresh.tick()
    if fresh._running:
        await asyncio.wait(fresh._running)
    assert CALLS == [("make", "job")] and (await app.store.get("tasks", t["id"]))["status"] == "done"


async def test_cancel_rules(app, world):
    t = await schedule(app, world, member=world.admin, when={"kind": "once", "run_at": tp.iso(tp.now() + timedelta(hours=1)), "delay_s": 3600})
    rec, msg = await app.scheduler.cancel(world.g.id, t["id"][:6], world.plain.id, False)
    assert not msg.startswith("Cancelled") and (await app.store.get("tasks", t["id"]))["status"] == "pending"
    rec, msg = await app.scheduler.cancel(world.g.id, t["id"][:6], world.admin.id, False)
    assert msg.startswith("Cancelled")
    await force_due(app, t)
    await drain(app)
    assert CALLS == [] and (await app.store.get("tasks", t["id"]))["status"] == "cancelled"
    _, msg = await app.scheduler.cancel(world.g.id, t["id"], world.admin.id, True)
    assert "cancelled" in msg
    _, msg = await app.scheduler.cancel(world.g.id, "nothere", world.admin.id, True)
    assert "couldn't find" in msg


async def test_admin_can_cancel_someone_elses_task(app, world):
    t = await schedule(app, world, member=world.mod, tool="t_make", when={"kind": "once", "run_at": tp.iso(tp.now() + timedelta(hours=1)), "delay_s": 3600})
    _, msg = await app.scheduler.cancel(world.g.id, t["id"], world.admin.id, True)
    assert msg.startswith("Cancelled")


async def test_task_limit_per_guild(app, world):
    app.cfg.scheduler.max_tasks_per_guild = 2
    far = {"kind": "once", "run_at": tp.iso(tp.now() + timedelta(days=1)), "delay_s": 86400}
    await schedule(app, world, when=far)
    await schedule(app, world, when=far)
    ctx = await ctx_for(app, world, world.admin)
    res = await app.executor.run(BoundPlan([step("s1", "t_make", {"name": "z"}, when=far)]), ctx)
    assert res.outcomes[0].status == "failure" and "limit" in res.outcomes[0].text


async def test_absolute_time_that_passed_while_waiting_for_confirmation_is_refused(app, world):
    past = {"kind": "once", "run_at": tp.iso(tp.now() - timedelta(minutes=5))}
    ctx = await ctx_for(app, world, world.admin)
    res = await app.executor.run(BoundPlan([step("s1", "t_make", {"name": "z"}, when=past)]), ctx)
    assert res.outcomes[0].status == "failure" and "passed" in res.outcomes[0].text


async def test_relative_delay_counts_from_creation_not_from_planning(app, world):
    when = {"kind": "once", "run_at": tp.iso(tp.now() - timedelta(minutes=4)), "delay_s": 120}
    t = await schedule(app, world, when=when)
    assert tp.now() + timedelta(seconds=100) < tp.from_iso(t["run_at"]) < tp.now() + timedelta(seconds=140)
