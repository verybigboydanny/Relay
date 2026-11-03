from __future__ import annotations

import discord
import pytest

from relay.services import permission_service as ps
from relay.utils.errors import PlanError
from tests.dfake import ADMIN, add_role, mk_guild, mk_member


def test_normalization_and_aliases():
    assert ps.norm_perm("View Channel") == "view_channel"
    assert ps.norm_perm("read") == "view_channel"
    assert ps.norm_perm("manage events") == "manage_events"
    assert ps.norm_perm("read_messages") == "view_channel"
    with pytest.raises(PlanError):
        ps.norm_perm("fly")


def test_apply_change_moves_between_allow_and_deny():
    p = ps.apply_change((0, 0), allow={"view_channel"})
    assert ps.names_of(p[0]) == ["view_channel"]
    p = ps.apply_change(p, deny={"view_channel"})
    assert p[0] == 0 and ps.names_of(p[1]) == ["view_channel"]
    assert ps.apply_change(p, reset={"view_channel"}) == (0, 0)


def test_overwrite_roundtrip():
    ow = discord.PermissionOverwrite(view_channel=True, send_messages=False)
    a, d = ps.ow_pair(ow)
    assert ps.pair_to_ow(a, d).pair() == ow.pair()


def test_effective_order_everyone_roles_member():
    view = ps.FLAGS["view_channel"]
    base = view
    ev, role, member = 1, 2, 3
    deny_ev = {ev: (0, view)}
    assert ps.effective(base, set(), member, ev, deny_ev) & view == 0
    assert ps.effective(base, {role}, member, ev, {**deny_ev, role: (view, 0)}) & view
    assert ps.effective(base, {role}, member, ev, {**deny_ev, role: (view, 0), member: (0, view)}) & view == 0
    assert ps.effective(discord.Permissions(administrator=True).value, set(), member, ev, deny_ev) & view


def test_lockout_detection():
    g = mk_guild()
    mod = add_role(g, "Mod", 5)
    alice = mk_member(g, "alice", [mod])
    bob = mk_member(g, "bob")
    old = {}
    new = {g.id: (0, ps.FLAGS["view_channel"])}
    lost = ps.lockouts(g, new, old, [("alice", alice), ("bob", bob)])
    assert len(lost) == 2
    new[mod.id] = (ps.FLAGS["view_channel"], 0)
    assert [m for m in ps.lockouts(g, new, old, [("alice", alice), ("bob", bob)])] == ["bob would lose access to the channel"]


def test_dangerous_sets():
    assert "administrator" in ps.ESCALATING and ps.ESCALATING <= ps.DANGEROUS
    assert "send_messages" not in ps.DANGEROUS
