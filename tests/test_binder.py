from __future__ import annotations

import discord
import pytest

from relay.ai.schemas import PlannerOut
from relay.config import load_config
from relay.services.binder import Binder, BoundPlan, order_steps
from relay.services.member_resolver import Env, Resolver
from relay.tools import REGISTRY
from relay.utils.errors import Ambiguous, PlanError, Unresolved
from tests.dfake import add_role, mk_channel, mk_guild, mk_member

CFG = load_config()


@pytest.fixture
def world():
    g = mk_guild()
    mods = add_role(g, "Moderators", 20)
    alex = mk_member(g, "Alex", [mods])
    ram = mk_member(g, "rambutan")
    cat = mk_channel(g, "Baby", discord.CategoryChannel)
    chan = mk_channel(g, "general", category=cat)
    return g, dict(mods=mods, alex=alex, ram=ram, cat=cat, chan=chan)


def env(g, **kw):
    return Env(guild=g, actor_id=kw.pop("actor", 1), channel_id=kw.pop("channel", None), **kw)


def plan(*steps):
    return PlannerOut.model_validate({"type": "plan", "steps": [{"id": f"s{i + 1}", **s} for i, s in enumerate(steps)]})


async def bind(g, out, **kw):
    return await Binder(CFG, REGISTRY).bind(out, env(g, **kw), "UTC")


async def test_exact_member_by_name_mention_and_self(world):
    g, w = world
    for ref in ("rambutan", "@Rambutan", f"<@{w['ram'].id}>"):
        p = await bind(g, plan({"tool": "ban_member", "args": {"members": [ref]}}))
        assert p.steps[0].refs["members"] == [w["ram"].id]
    p = await bind(g, plan({"tool": "inspect_member", "args": {"member": "me"}}), actor=w["alex"].id)
    assert p.steps[0].refs["member"] == w["alex"].id


async def test_raw_ids_must_have_been_mentioned(world):
    g, w = world
    with pytest.raises(Unresolved):
        await bind(g, plan({"tool": "ban_member", "args": {"members": [str(w["ram"].id)]}}))
    p = await bind(g, plan({"tool": "ban_member", "args": {"members": [w["ram"].id]}}), allowed_ids={w["ram"].id})
    assert p.steps[0].refs["members"] == [w["ram"].id]
    p = await bind(g, plan({"tool": "ban_member", "args": {"members": [str(w["ram"].id)]}}), entities={"member": {"id": w["ram"].id, "name": "r"}})
    assert p.steps[0].refs["members"] == [w["ram"].id]


async def test_ambiguity_is_never_auto_resolved(world):
    g, w = world
    mk_member(g, "alex2")
    twin = mk_member(g, "Alex")
    with pytest.raises(Ambiguous) as e:
        await bind(g, plan({"tool": "ban_member", "args": {"members": ["alex"]}}))
    assert not e.value.suggestion and {c[0] for c in e.value.candidates} == {w["alex"].id, twin.id} and e.value.kind == "user"


async def test_fuzzy_match_is_only_a_suggestion(world):
    g, w = world
    with pytest.raises(Ambiguous) as e:
        await bind(g, plan({"tool": "ban_member", "args": {"members": ["rambut"]}}))
    assert e.value.suggestion and e.value.candidates[0][0] == w["ram"].id
    with pytest.raises(Unresolved):
        await bind(g, plan({"tool": "ban_member", "args": {"members": ["zzzz"]}}))


async def test_override_after_user_picks(world):
    g, w = world
    mk_member(g, "Alex")
    out = plan({"tool": "ban_member", "args": {"members": ["alex"]}})
    p = await bind(g, out, overrides={("user", "alex"): w["alex"].id}, allowed_ids={w["alex"].id})
    assert p.steps[0].refs["members"] == [w["alex"].id]


async def test_pronouns_use_context_entities(world):
    g, w = world
    ent = {"member": {"id": w["ram"].id, "name": "rambutan"}, "channel": {"id": w["chan"].id, "name": "general"}}
    p = await bind(g, plan({"tool": "timeout_member", "args": {"members": ["him"], "duration": "10m"}}, {"tool": "edit_channel", "args": {"channel": "it", "topic": "x"}}), entities=ent)
    assert p.steps[0].refs["members"] == [w["ram"].id] and p.steps[1].refs["channel"] == w["chan"].id
    with pytest.raises(Unresolved):
        await bind(g, plan({"tool": "timeout_member", "args": {"members": ["him"], "duration": "10m"}}))


async def test_here_means_current_channel_and_channel_names_normalize(world):
    g, w = world
    p = await bind(g, plan({"tool": "inspect_channel", "args": {"channel": "current"}}), channel=w["chan"].id)
    assert p.steps[0].refs["channel"] == w["chan"].id
    p = await bind(g, plan({"tool": "inspect_channel", "args": {"channel": "#General"}}))
    assert p.steps[0].refs["channel"] == w["chan"].id
    p = await bind(g, plan({"tool": "inspect_channel", "args": {"channel": "baby"}}))
    assert p.steps[0].refs["channel"] == w["cat"].id


async def test_dependency_on_something_created_earlier_in_the_plan(world):
    g, w = world
    p = await bind(g, plan(
        {"tool": "create_category", "args": {"name": "Staff", "private": True}},
        {"tool": "create_role", "args": {"name": "Helpers"}},
        {"tool": "create_channel", "args": {"name": "ops", "category": "Staff", "private": True, "allow_roles": ["Helpers"]}},
        {"tool": "assign_role", "args": {"members": ["rambutan"], "role": "Helpers"}}))
    ch, assign = p.steps[2], p.steps[3]
    assert ch.refs["category"] == "$s1" and ch.refs["allow_roles"] == ["$s2"]
    assert set(ch.depends_on) == {"s1", "s2"} and assign.depends_on == ["s2"]
    assert [s.id for s in order_steps(p.steps)][:2] == ["s1", "s2"]


async def test_missing_target_without_creation_step_fails(world):
    g, w = world
    with pytest.raises(Unresolved):
        await bind(g, plan({"tool": "create_channel", "args": {"name": "ops", "category": "Nowhere"}}))


def test_cycles_and_unknown_dependencies_rejected():
    from relay.services.binder import BoundStep
    a = BoundStep("s1", "x", {}, {}, {}, ["s2"], None, "")
    b = BoundStep("s2", "x", {}, {}, {}, ["s1"], None, "")
    with pytest.raises(PlanError):
        order_steps([a, b])
    with pytest.raises(PlanError):
        order_steps([BoundStep("s1", "x", {}, {}, {}, ["s9"], None, "")])


async def test_unknown_tool_extra_args_and_bad_values_rejected(world):
    g, w = world
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "format_disk", "args": {}}))
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "ban_member", "args": {"members": ["rambutan"], "nuke": True}}))
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "timeout_member", "args": {"members": ["rambutan"], "duration": "40 days"}}))
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "create_channel", "args": {"name": ""}}))


async def test_plan_and_bulk_caps(world):
    g, w = world
    many = [{"tool": "inspect_channel", "args": {"channel": "general"}}] * (CFG.limits.actions_per_plan + 1)
    with pytest.raises(PlanError):
        await bind(g, plan(*many))
    for i in range(CFG.limits.max_bulk_targets + 1):
        mk_member(g, f"user{i}")
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "ban_member", "args": {"members": [f"user{i}" for i in range(CFG.limits.max_bulk_targets + 1)]}}))


async def test_scheduling_forms(world):
    g, w = world
    p = await bind(g, plan({"tool": "ban_member", "args": {"members": ["rambutan"]}, "when": {"in": "2 minutes"}}))
    assert p.steps[0].when["kind"] == "once" and p.steps[0].when["delay_s"] == pytest.approx(120, abs=1)
    p = await bind(g, plan({"tool": "moderation_summary", "args": {}, "when": {"every": {"freq": "weekly", "weekday": "fri", "time": "19:00"}}}))
    assert p.steps[0].when["kind"] == "every" and p.steps[0].when["rule"]["weekday"] == "fri"
    with pytest.raises(PlanError) as e:
        await bind(g, plan({"tool": "ban_member", "args": {"members": ["rambutan"]}, "when": {"at": "2020-01-01 10:00"}}))
    assert "past" in e.value.user_message
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "ban_member", "args": {"members": ["rambutan"]}, "when": {"at": "tomorrow 5pm"}}))
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "moderation_summary", "args": {}, "when": {"every": {"freq": "interval", "interval": "30 seconds"}}}))
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "inspect_channel", "args": {"channel": "general"}, "when": {"in": "1 hour"}}))
    with pytest.raises(PlanError):
        await bind(g, plan({"tool": "configure", "args": {"setting": "timezone", "timezone": "UTC"}, "when": {"in": "1 hour"}}))


async def test_bound_plan_serialization_roundtrip(world):
    g, w = world
    p = await bind(g, plan({"tool": "ban_member", "args": {"members": ["rambutan"], "reason": "spam"}}))
    q = BoundPlan.from_dict(p.to_dict())
    assert q.to_dict() == p.to_dict() and "BAN" in q.steps[0].preview
