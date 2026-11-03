from __future__ import annotations

from datetime import timedelta

import pytest

from relay.storage.base import new_rec
from relay.utils import time_parser as tp
from relay.utils.errors import RelayError

G, H, U, V = 1001, 2002, 11, 22


async def test_user_memory_roundtrip_update_and_delete(app):
    m = app.memory
    rec = await m.add_memory(G, "user", U, "I prefer short replies", key="style")
    again = await m.add_memory(G, "user", U, "I prefer long replies", key="style")
    assert again["id"] == rec["id"]
    items = await m.list_memories(G, "user", U)
    assert len(items) == 1 and items[0]["data"]["text"] == "I prefer long replies"
    assert await m.delete_memory(G, "style", "user", U)
    assert await m.list_memories(G, "user", U) == []
    assert await app.store.get("memories", rec["id"], G) is None


async def test_memories_are_isolated_by_user_and_guild(app):
    m = app.memory
    await m.add_memory(G, "user", U, "mine")
    await m.add_memory(G, "guild", None, "server note")
    await m.add_memory(H, "user", U, "other guild")
    assert [x["data"]["text"] for x in await m.list_memories(G, "user", V)] == []
    assert [x["data"]["text"] for x in await m.list_memories(G, "user", U)] == ["mine"]
    assert [x["data"]["text"] for x in await m.list_memories(G, "guild")] == ["server note"]
    other = (await m.list_memories(H, "user", U))[0]
    assert await m.delete_memory(G, other["id"], "user", U) is None
    assert await app.store.get("memories", other["id"], H) is not None


@pytest.mark.parametrize("text", ["my password is hunter22222", "token: abcdef123456", "gsk_" + "a" * 30, "api key = sk-abcdefghijklmnopqrstuv"])
async def test_secrets_are_refused(app, text):
    with pytest.raises(RelayError) as e:
        await app.memory.add_memory(G, "user", U, text)
    assert "secret" in e.value.user_message or "credential" in e.value.user_message


async def test_memory_limits(app):
    app.cfg.memory.max_per_user = 2
    await app.memory.add_memory(G, "user", U, "a")
    await app.memory.add_memory(G, "user", U, "b")
    with pytest.raises(RelayError):
        await app.memory.add_memory(G, "user", U, "c")
    with pytest.raises(RelayError):
        await app.memory.add_memory(G, "user", V, "x" * 5000)


async def test_context_expires_and_is_scoped(app):
    m = app.memory
    await m.add_turn(G, 5, U, "create a channel", "Created #lol", {"channel": {"id": 9, "name": "lol"}})
    ctx = await m.context(G, 5, U)
    assert ctx["entities"]["channel"]["name"] == "lol" and len(ctx["turns"]) == 2
    assert (await m.context(G, 5, V))["turns"] == []
    assert (await m.context(G, 6, U))["turns"] == []
    assert (await m.context(H, 5, U))["turns"] == []
    rec = await app.store.get("context", f"{G}:5:{U}", G)
    await app.store.update_if("context", rec["id"], {}, {"expires_at": tp.iso(tp.now() - timedelta(minutes=1))}, G)
    assert (await m.context(G, 5, U))["turns"] == []
    assert await app.store.get("context", rec["id"], G) is None


async def test_context_redacts_secrets_and_trims(app):
    app.cfg.context.max_turns = 2
    for i in range(5):
        await app.memory.add_turn(G, 1, U, f"msg {i} password: supersecret9", f"reply {i}")
    ctx = await app.memory.context(G, 1, U)
    assert len(ctx["turns"]) == 4
    assert all("supersecret9" not in t["t"] for t in ctx["turns"])


async def test_events_are_stored_and_queried_by_range_guild_and_kind(app):
    m = app.memory
    await m.record_event(G, "channel.create", actor_id=U, resource_type="channel", resource_id=5, details={"name": "lol"})
    await m.record_event(G, "moderation.ban", actor_id=U, target_id=V)
    await m.record_event(H, "channel.create", actor_id=U)
    assert len(await m.query_events(G)) == 2
    assert len(await m.query_events(G, kind="moderation.ban")) == 1
    assert len(await m.query_events(G, target_id=V)) == 1
    assert await m.query_events(G, since=tp.iso(tp.now() + timedelta(hours=1))) == []
    ev = (await m.query_events(G, kind="channel.create"))[0]
    assert ev["data"]["resource_id"] == "5" and ev["data"]["source"] == "relay"


async def test_warnings_lifecycle(app):
    m = app.memory
    w = await m.add_warning(G, V, U, "spam", "p1")
    await m.add_warning(G, V, U, "spam again", "p2")
    assert len(await m.list_warnings(G, V)) == 2
    assert await m.remove_warning(G, w["id"], U)
    assert len(await m.list_warnings(G, V)) == 1
    assert len(await m.list_warnings(G, V, active_only=False)) == 2
    assert await m.remove_warning(G, w["id"], U) is None
    assert await m.list_warnings(H, V) == []


async def test_settings_merge_config_defaults_with_stored(app):
    from relay.config import GuildDefaults
    app.cfg.guilds[G] = GuildDefaults(roles={"moderator": [1, 2]}, announcement_channel=77)
    s = await app.memory.settings(G)
    assert s.roles["moderator"] == [1, 2] and s.announcement_channel == 77
    await app.memory.update_settings(G, {"roles": {"moderator": [3]}, "timezone": "Asia/Kolkata"})
    s = await app.memory.settings(G)
    assert s.roles["moderator"] == [3] and s.announcement_channel == 77 and s.timezone == "Asia/Kolkata"
    assert (await app.memory.settings(H)).roles == {}
    assert await app.memory.timezone_for(G) == "Asia/Kolkata"
    await app.memory.add_memory(G, "user", U, "America/New_York", key="timezone")
    assert await app.memory.timezone_for(G, U) == "America/New_York"


async def test_purge_expired_removes_old_but_keeps_warnings_and_fresh(app):
    s = app.store
    old = tp.iso(tp.now() - timedelta(days=400))
    await s.put("events", new_rec("old", G, {}, ts=old, kind="x"))
    await s.put("events", new_rec("new", G, {}, ts=tp.iso(tp.now()), kind="x"))
    await s.put("warnings", new_rec("w", G, {"reason": "r"}, ts=old, target_id=V, status="active"))
    await s.put("tasks", new_rec("t1", G, {}, ts=old, status="done"))
    await s.put("tasks", new_rec("t2", G, {}, ts=old, status="pending", run_at=tp.iso(tp.now())))
    out = await app.memory.purge_expired()
    assert out["events"] == 1 and out["tasks"] == 1
    assert await s.get("events", "new", G) and await s.get("warnings", "w", G) and await s.get("tasks", "t2", G)
