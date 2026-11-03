from __future__ import annotations

import asyncio
import types

import discord
import pytest

from relay.services.binder import BoundPlan, BoundStep
from relay.storage.base import new_rec
from relay.tools.base import REGISTRY, Args, Item, Result, Tool, ref
from relay.utils import time_parser as tp
from relay.utils.errors import ExecError, Retryable, Uncertain
from tests.dfake import add_role, mk_channel, mk_guild, mk_member

CALLS: list = []


class NameArgs(Args):
    name: str = "x"


class ParentArgs(Args):
    parent: str = ref("channel")


async def t_make(ctx, a, r):
    CALLS.append(("make", a.name))
    return Result(f"made {a.name}", resource_id=900 + len(CALLS), resource_type="channel", event=("t.made", "staff"))


async def t_child(ctx, a, r):
    CALLS.append(("child", r.one("parent")))
    return Result(f"child of {r.one('parent')}")


async def t_fail(ctx, a, r):
    CALLS.append(("fail", a.name))
    raise ExecError("boom", user_message="it exploded")


async def t_crash(ctx, a, r):
    raise ZeroDivisionError("secret internals")


async def t_net(ctx, a, r):
    CALLS.append(("net", a.name))
    raise asyncio.TimeoutError()


async def t_flaky(ctx, a, r):
    CALLS.append(("flaky", a.name))
    raise Retryable("later", user_message="try later")


async def t_multi(ctx, a, r):
    return Result("two done", items=[Item(1, True, "a"), Item(2, False, "b", "forbidden")])


VERIFY = {"value": None}


async def verify(ctx, a, r):
    return VERIFY["value"]


def _tools():
    kw = dict(access="open")
    return [
        Tool("t_make", "t", NameArgs, t_make, risk="low", creates="channel", **kw),
        Tool("t_child", "t", ParentArgs, t_child, risk="low", **kw),
        Tool("t_fail", "t", NameArgs, t_fail, risk="low", **kw),
        Tool("t_crash", "t", NameArgs, t_crash, risk="low", **kw),
        Tool("t_net", "t", NameArgs, t_net, risk="high", verify=verify, **kw),
        Tool("t_flaky", "t", NameArgs, t_flaky, risk="low", **kw),
        Tool("t_multi", "t", NameArgs, t_multi, risk="medium", **kw),
        Tool("t_staff", "t", NameArgs, t_make, risk="medium", access="staff"),
        Tool("t_danger", "t", NameArgs, t_make, risk="high", access="open"),
    ]


@pytest.fixture(autouse=True)
def test_tools():
    added = []
    for t in _tools():
        REGISTRY.tools[t.name] = t
        added.append(t.name)
    CALLS.clear()
    VERIFY["value"] = None
    yield
    for n in added:
        REGISTRY.tools.pop(n, None)


@pytest.fixture
def world(app):
    g = mk_guild()
    mods = add_role(g, "Mods", 20, discord.Permissions(moderate_members=True, kick_members=True))
    admin = mk_member(g, "boss", [add_role(g, "Boss", 40, discord.Permissions(administrator=True))])
    plain = mk_member(g, "plain")
    mod = mk_member(g, "mod", [mods])
    ch = mk_channel(g, "general")
    app.bot = types.SimpleNamespace(get_guild=lambda i: g if i == g.id else None, get_channel=lambda i: g.get_channel(i), user=g.me)
    return types.SimpleNamespace(g=g, admin=admin, plain=plain, mod=mod, ch=ch, mods=mods)


def step(id, tool, args=None, refs=None, deps=(), when=None, preview=None):
    return BoundStep(id, tool, args or {}, refs or {}, {}, list(deps), when, preview or f"{tool} {id}")


async def ctx_for(app, w, member, source="natural_language", **kw):
    return await app.make_ctx(w.g, member, member.id, w.ch, source, **kw)
