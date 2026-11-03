from __future__ import annotations

from datetime import timedelta

import pytest

from relay.services.binder import BoundPlan
from relay.tools.base import REGISTRY
from relay.utils import time_parser as tp
from relay.utils.errors import Denied, PlanError
from tests.helpers import step, test_tools, world  # noqa: F401


def plan(*steps):
    return BoundPlan(list(steps), plan_id="pl1")


async def make(app, w, p=None, user=None):
    p = p or plan(step("s1", "t_danger", {"name": "x"}))
    return await app.confirmations.create(p, guild_id=w.g.id, requester_id=(user or w.admin).id, channel_id=w.ch.id,
                                          request_id="rq", source="natural_language", request_text="do it")


def test_policy_by_risk_and_mode(app):
    c = app.confirmations
    low, high = step("s1", "t_make", {"name": "a"}), step("s1", "t_danger", {"name": "a"})
    med = step("s1", "t_multi", {"name": "a"})
    assert not c.step_needs(low, "natural_language") and c.step_needs(med, "natural_language") and c.step_needs(high, "natural_language")
    app.cfg.confirmation.direct = "always"
    assert c.step_needs(low, "natural_language")
    app.cfg.confirmation.direct = "never"
    assert not c.step_needs(high, "natural_language")
    app.cfg.confirmation.direct = "risky"
    assert not c.step_needs(high, "scheduler")


def test_scheduled_and_recurring_have_their_own_policies(app):
    c = app.confirmations
    once = step("s1", "t_make", {"name": "a"}, when={"kind": "once", "run_at": "x"})
    every = step("s1", "t_make", {"name": "a"}, when={"kind": "every", "run_at": "x"})
    assert c.step_needs(once, "natural_language") and c.step_needs(every, "natural_language")
    app.cfg.confirmation.scheduled = "never"
    assert not c.step_needs(once, "natural_language") and c.step_needs(every, "natural_language")


def test_commands_can_skip_policy_confirmation_when_configured(app):
    high = step("s1", "t_danger", {"name": "a"})
    assert app.confirmations.step_needs(high, "command")
    app.cfg.confirmation.slash_requires_confirmation = False
    assert not app.confirmations.step_needs(high, "command")


def test_escalation_always_needs_confirmation_even_when_policy_is_never(app):
    app.cfg.confirmation.direct = "never"
    s = step("s1", "configure", {"setting": "role_group", "group": "moderator", "role": "r"}, refs={"role": 5})
    assert app.confirmations.step_needs(s, "natural_language")
    s2 = step("s1", "create_role", {"name": "x", "permissions": ["administrator"]})
    assert app.confirmations.step_needs(s2, "natural_language")
    s3 = step("s1", "create_role", {"name": "x", "permissions": ["manage_events"]})
    assert app.confirmations.step_needs(s3, "natural_language") is True
    app.cfg.confirmation.direct = "never"
    s4 = step("s1", "create_role", {"name": "x", "permissions": ["view_channel"]})
    assert not app.confirmations.step_needs(s4, "natural_language")


async def test_only_requester_can_approve_and_only_once(app, world):
    rec = await make(app, world)
    with pytest.raises(Denied):
        await app.confirmations.decide(rec["id"], world.mod.id, world.g.id, True)
    r, p = await app.confirmations.decide(rec["id"], world.admin.id, world.g.id, True)
    assert p.plan_id == "pl1" and p.steps[0].tool == "t_danger"
    with pytest.raises(PlanError) as e:
        await app.confirmations.decide(rec["id"], world.admin.id, world.g.id, True)
    assert "already approved" in e.value.user_message
    rows = await app.audit.search(world.g.id, plan_id="pl1")
    actions = {(r["action"], r["status"]) for r in rows}
    assert ("confirmation.requested", "success") in actions and ("confirmation.denied", "denied") in actions
    assert ("confirmation.approved", "success") in actions


async def test_other_guild_cannot_touch_confirmation(app, world):
    rec = await make(app, world)
    with pytest.raises(PlanError):
        await app.confirmations.decide(rec["id"], world.admin.id, world.g.id + 1, True)


async def test_expiry(app, world):
    rec = await make(app, world)
    await app.store.update_if("confirmations", rec["id"], {}, {"expires_at": tp.iso(tp.now() - timedelta(seconds=1))}, world.g.id)
    with pytest.raises(PlanError) as e:
        await app.confirmations.decide(rec["id"], world.admin.id, world.g.id, True)
    assert "expired" in e.value.user_message
    assert (await app.store.get("confirmations", rec["id"]))["status"] == "expired"


async def test_reject_is_recorded_and_final(app, world):
    rec = await make(app, world)
    _, p = await app.confirmations.decide(rec["id"], world.admin.id, world.g.id, False)
    assert p is None
    with pytest.raises(PlanError):
        await app.confirmations.decide(rec["id"], world.admin.id, world.g.id, True)
    assert any(r["action"] == "confirmation.rejected" for r in await app.audit.search(world.g.id))


async def test_modified_plan_is_discarded(app, world):
    rec = await make(app, world)
    stored = (await app.store.get("confirmations", rec["id"]))["data"]["plan"]
    stored["steps"][0]["args"]["name"] = "something else entirely"
    await app.store.update_if("confirmations", rec["id"], {}, {"data": {"plan": stored}}, world.g.id)
    with pytest.raises(PlanError) as e:
        await app.confirmations.decide(rec["id"], world.admin.id, world.g.id, True)
    assert "changed" in e.value.user_message
    assert (await app.store.get("confirmations", rec["id"]))["status"] == "rejected"


async def test_pending_cap_per_user(app, world):
    app.cfg.confirmation.max_pending_per_user = 2
    await make(app, world)
    await make(app, world)
    with pytest.raises(PlanError):
        await make(app, world)
    await make(app, world, user=world.mod)
