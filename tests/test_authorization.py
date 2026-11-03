from __future__ import annotations

import discord
import pytest

from relay.config import load_config
from relay.services.authorization import Authorizer, grant_matches
from relay.services.memory_service import GuildSettings
from relay.tools import REGISTRY
from relay.utils.errors import Denied
from tests.dfake import ADMIN, add_role, mk_guild, mk_member


@pytest.fixture
def world():
    g = mk_guild()
    mods = add_role(g, "Mods", 20, discord.Permissions(moderate_members=True, kick_members=True))
    staff = add_role(g, "Staff", 10)
    admins = add_role(g, "Admins", 30)
    restricted = add_role(g, "Muted", 5)
    s = GuildSettings(roles={"moderator": [mods.id], "admin": [admins.id], "restricted": [restricted.id], "trusted": [staff.id]})
    return g, s, dict(mods=mods, staff=staff, admins=admins, restricted=restricted)


@pytest.fixture
def auth():
    return Authorizer(load_config())


def T(name):
    return REGISTRY.tools[name]


def test_owner_and_administrator_permission_are_admin(world, auth):
    g, s, r = world
    owner = mk_member(g, "owner", id=g.owner_id)
    root = mk_member(g, "root", [add_role(g, "SuperAdmin", 40, ADMIN)])
    plain = mk_member(g, "plain")
    assert auth.is_admin(owner, s) and auth.is_admin(root, s) and not auth.is_admin(plain, s)


def test_role_names_are_not_trusted_only_ids(world, auth):
    g, s, r = world
    fake = mk_member(g, "faker", [add_role(g, "Admins", 3), add_role(g, "Moderator", 3)])
    assert auth.groups(fake, s) == set()
    assert not auth.decide(T("ban_member"), fake, s).ok


def test_moderator_group_grants_but_needs_discord_permission(world, auth):
    g, s, r = world
    mod = mk_member(g, "mod", [r["mods"]])
    assert auth.decide(T("timeout_member"), mod, s).ok
    assert auth.decide(T("kick_member"), mod, s).ok
    assert not auth.decide(T("ban_member"), mod, s).ok
    assert not auth.decide(T("delete_channel"), mod, s).ok
    assert not auth.decide(T("create_role"), mod, s).ok
    no_perm = mk_member(g, "weak", [add_role(g, "Wk", 4)])
    s2 = GuildSettings(roles={"moderator": [no_perm.roles[1].id]})
    d = auth.decide(T("timeout_member"), no_perm, s2)
    assert not d.ok and "permission" in d.reason


def test_group_admin_can_do_everything(world, auth):
    g, s, r = world
    adm = mk_member(g, "adm", [r["admins"]])
    assert all(auth.decide(t, adm, s).ok for t in REGISTRY.tools.values())


def test_open_tools_for_everyone_but_restricted_blocked(world, auth):
    g, s, r = world
    plain = mk_member(g, "plain")
    muted = mk_member(g, "muted", [r["restricted"]])
    assert auth.decide(T("recall"), plain, s).ok
    assert not auth.decide(T("recall"), muted, s).ok
    assert not auth.decide(T("audit_search"), plain, s).ok
    assert not auth.decide(T("configure"), plain, s).ok


def test_audit_tools_are_not_in_read_wildcard(world, auth):
    assert not grant_matches(["@read"], T("audit_search"))
    assert grant_matches(["@read"], T("inspect_channel"))
    assert grant_matches(["*"], T("audit_export"))


def test_member_gone_is_denied(world, auth):
    g, s, r = world
    assert not auth.decide(T("recall"), None, s).ok


def test_hierarchy_rules(world, auth):
    g, s, r = world
    mod = mk_member(g, "mod", [r["mods"]])
    peer = mk_member(g, "peer", [r["mods"]])
    low = mk_member(g, "low")
    boss = mk_member(g, "boss", [r["admins"]])
    owner = mk_member(g, "own", id=g.owner_id)
    auth.check_member(mod, low)
    for tgt in (peer, boss, owner):
        with pytest.raises(Denied):
            auth.check_member(mod, tgt)
    with pytest.raises(Denied):
        auth.check_member(mod, mod)
    with pytest.raises(Denied):
        auth.check_member(boss, g.me)
    above_bot = mk_member(g, "above", [add_role(g, "Huge", 90)])
    with pytest.raises(Denied) as e:
        auth.check_member(owner, above_bot)
    assert "above my highest" in e.value.user_message


def test_role_checks(world, auth):
    g, s, r = world
    mod = mk_member(g, "mod", [r["mods"]])
    auth.check_role(mod, r["staff"])
    with pytest.raises(Denied):
        auth.check_role(mod, r["admins"])
    with pytest.raises(Denied):
        auth.check_role(mod, g.default_role)
    managed = add_role(g, "Integration", 2)
    managed.managed = True
    with pytest.raises(Denied) as e:
        auth.check_role(mod, managed)
    assert "managed" in e.value.user_message


def test_cannot_grant_beyond_own_permissions(world, auth):
    g, s, r = world
    mod = mk_member(g, "mod", [r["mods"]])
    auth.check_can_grant(mod, {"kick_members"}, s)
    with pytest.raises(Denied) as e:
        auth.check_can_grant(mod, {"ban_members"}, s)
    assert "don't have" in e.value.user_message
    with pytest.raises(Denied) as e:
        auth.check_can_grant(mod, {"administrator"}, s)
    assert "Administrator" in e.value.user_message
    group_admin = mk_member(g, "ga", [r["admins"]])
    with pytest.raises(Denied) as e:
        auth.check_can_grant(group_admin, {"administrator"}, s)
    assert "Administrator" in e.value.user_message
    owner = mk_member(g, "own", id=g.owner_id)
    auth.check_can_grant(owner, {"administrator"}, s)


def test_bot_permission_check(world, auth):
    g, s, r = world
    auth.check_bot_perm(g, "ban_members")
    g.me.guild_permissions = discord.Permissions.none()
    with pytest.raises(Denied) as e:
        auth.check_bot_perm(g, "ban_members")
    assert "I don't have" in e.value.user_message
