from __future__ import annotations

import pytest

from relay.services.binder import BoundPlan
from relay.utils.errors import StorageError
from tests.helpers import CALLS, VERIFY, ctx_for, step, test_tools, world  # noqa: F401


async def run(app, w, steps, member=None, **kw):
    kw.setdefault("plan_id", "plan1")
    ctx = await ctx_for(app, w, member or w.admin, **kw)
    return await app.executor.run(BoundPlan(steps, plan_id="plan1"), ctx), ctx


async def test_dependency_order_and_deferred_reference_resolution(app, world):
    res, ctx = await run(app, world, [
        step("s2", "t_child", {"parent": "p"}, refs={"parent": "$s1"}, deps=["s1"]),
        step("s1", "t_make", {"name": "parent"}),
    ])
    assert [c[0] for c in CALLS] == ["make", "child"]
    assert CALLS[1][1] == ctx.results["s1"].resource_id
    assert res.ok and [o.step_id for o in res.outcomes] == ["s1", "s2"]


async def test_failed_step_skips_dependents_but_not_independent_steps(app, world):
    res, _ = await run(app, world, [
        step("s1", "t_fail", {"name": "a"}),
        step("s2", "t_child", {"parent": "p"}, refs={"parent": "$s1"}, deps=["s1"]),
        step("s3", "t_make", {"name": "indep"}),
    ])
    st = {o.step_id: o.status for o in res.outcomes}
    assert st == {"s1": "failure", "s2": "skipped", "s3": "success"}
    assert ("child", 1) not in CALLS and not res.ok
    text = res.render()
    assert "it exploded" in text and "1 of 3" in text and text.startswith("Partly done")


async def test_unexpected_exception_is_a_failure_with_no_internal_details(app, world):
    res, _ = await run(app, world, [step("s1", "t_crash")])
    assert res.outcomes[0].status == "failure" and "secret internals" not in res.render()
    rows = await app.audit.search(world.g.id, action="t_crash")
    assert rows[0]["status"] == "failure" and rows[0]["error_code"] == "internal"
    assert "secret" not in str(rows)


async def test_unauthorized_step_is_denied_and_never_runs(app, world):
    res, _ = await run(app, world, [step("s1", "t_staff", {"name": "z"})], member=world.plain)
    assert res.outcomes[0].status == "denied" and CALLS == []
    row = (await app.audit.search(world.g.id, action="t_staff"))[0]
    assert row["status"] == "denied" and row["authorization_result"] == "denied" and row["actor_id"] == world.plain.id


async def test_authorization_is_checked_again_at_execution_for_each_step(app, world):
    ctx = await ctx_for(app, world, world.plain)
    plan = BoundPlan([step("s1", "t_make", {"name": "ok"}), step("s2", "t_staff", {"name": "no"})])
    res = await app.executor.run(plan, ctx)
    assert [o.status for o in res.outcomes] == ["success", "denied"]


async def test_audit_has_start_and_final_entries_correlated_to_plan(app, world):
    await run(app, world, [step("s1", "t_make", {"name": "q"})], request_id="req9")
    rows = await app.audit.search(world.g.id, plan_id="plan1", limit=10)
    assert sorted(r["status"] for r in rows) == ["started", "success"]
    assert all(r["request_id"] == "req9" and r["actor_id"] == world.admin.id and r["authorization_result"] == "allowed" for r in rows)
    ev = await app.memory.query_events(world.g.id, kind="t.made")
    assert len(ev) == 1 and ev[0]["ref_id"] == "plan1"


async def test_fail_closed_blocks_risky_action_when_audit_is_down(app, world, monkeypatch):
    async def boom(e):
        raise StorageError("db down")
    monkeypatch.setattr(app.store, "append_audit", boom)
    res, _ = await run(app, world, [step("s1", "t_danger", {"name": "d"})])
    assert res.outcomes[0].status == "failure" and CALLS == [] and "audit" in res.render().lower()
    res, _ = await run(app, world, [step("s1", "t_make", {"name": "low"})])
    assert res.outcomes[0].status == "success"


async def test_fail_open_is_explicit_config(app, world, monkeypatch):
    app.audit.cfg.fail_closed = False

    async def boom(e):
        raise StorageError("db down")
    monkeypatch.setattr(app.store, "append_audit", boom)
    res, _ = await run(app, world, [step("s1", "t_danger", {"name": "d"})])
    assert res.outcomes[0].status == "success"


@pytest.mark.parametrize("verified,status", [(True, "success"), (False, "failure"), (None, "uncertain")])
async def test_network_error_state_is_verified_before_reporting(app, world, verified, status):
    VERIFY["value"] = verified
    res, _ = await run(app, world, [step("s1", "t_net", {"name": "n"})])
    assert res.outcomes[0].status == status
    assert len(CALLS) == 1
    if status == "uncertain":
        assert "Check the server" in res.render()
    row = [r for r in await app.audit.search(world.g.id, action="t_net") if r["status"] != "started"][0]
    assert row["status"] == status


async def test_partial_results_are_reported_per_item(app, world):
    res, _ = await run(app, world, [step("s1", "t_multi", {"name": "m"})])
    assert res.outcomes[0].status == "partial" and not res.ok
    rows = await app.audit.search(world.g.id, action="t_multi", limit=10)
    assert sorted(r["status"] for r in rows) == ["failure", "partial", "started", "success"]
    assert {r["target_id"] for r in rows if r["target_id"]} == {1, 2}


async def test_scheduled_step_is_stored_not_executed(app, world):
    when = {"kind": "once", "run_at": "2999-01-01T00:00:00.000000Z"}
    res, _ = await run(app, world, [step("s1", "t_make", {"name": "later"}, when=when)])
    assert res.outcomes[0].status == "scheduled" and CALLS == []
    tasks = await app.store.find("tasks", guild_id=world.g.id)
    assert len(tasks) == 1 and tasks[0]["status"] == "pending" and tasks[0]["actor_id"] == world.admin.id
    assert (await app.audit.search(world.g.id, action="task.create"))[0]["task_id"] == tasks[0]["id"]


async def test_preflight_reports_denied_steps_without_running(app, world):
    ctx = await ctx_for(app, world, world.plain)
    bad = await app.executor.preflight(BoundPlan([step("s1", "t_make"), step("s2", "t_staff")]), ctx)
    assert [s.id for s, _ in bad] == ["s2"] and CALLS == []
