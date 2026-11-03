from __future__ import annotations

import json

import pytest

from relay.storage.base import GENESIS, merge_patch, new_rec, verify_rows
from relay.storage.json_backend import JsonStore
from relay.storage.turso_backend import TursoStore
from relay.utils.errors import StorageCorruption, StorageError
from tests.conftest import KEY
from tests.fakes import FakeLibsql

G = 111
H = 222


def rec(id, guild=G, ts="2026-10-01T00:00:00.000000Z", **kw):
    data = kw.pop("data", {"n": 1})
    return new_rec(id, guild, data, ts=ts, **kw)


def entry(guild=G, **kw):
    base = {"event_id": kw.pop("event_id", "e1"), "ts": kw.pop("ts", "2026-10-02T10:00:00.000000Z"),
            "guild_id": guild, "actor_id": 1, "actor_type": "user", "action": "channel.create", "status": "success",
            "details": {"a": 1}, "authorization_result": "allowed", "confirmation_status": "not_required"}
    base.update(kw)
    return base


async def test_put_get_delete(store):
    await store.put("memories", rec("a", actor_id=5, kind="preference", data={"text": "hi"}))
    got = await store.get("memories", "a", G)
    assert got["data"] == {"text": "hi"} and got["actor_id"] == 5
    assert await store.get("memories", "a", H) is None
    assert await store.delete("memories", "a", H) is False
    assert await store.delete("memories", "a", G) is True
    assert await store.get("memories", "a", G) is None


async def test_upsert_cannot_cross_guilds(store):
    await store.put("memories", rec("same", guild=G, data={"v": 1}))
    try:
        await store.put("memories", rec("same", guild=H, data={"v": 2}))
    except StorageError:
        pass
    assert (await store.get("memories", "same", G))["data"] == {"v": 1}


async def test_find_filters_order_paging(store):
    for i in range(5):
        await store.put("events", rec(f"e{i}", ts=f"2026-10-0{i + 1}T00:00:00.000000Z", kind="x" if i % 2 else "y",
                                      actor_id=i))
    await store.put("events", rec("other", guild=H, kind="x"))
    rows = await store.find("events", guild_id=G, where={"kind": "x"}, order="ts", desc=True)
    assert [r["id"] for r in rows] == ["e3", "e1"]
    rows = await store.find("events", guild_id=G, since="2026-10-02T00:00:00.000000Z", until="2026-10-04T00:00:00.000000Z")
    assert [r["id"] for r in rows] == ["e1", "e2"]
    assert len(await store.find("events", guild_id=G, limit=2, offset=1)) == 2
    assert await store.count("events", guild_id=G) == 5
    assert await store.count("events", guild_id=G, where={"actor_id": [0, 1]}) == 2
    assert all(r["guild_id"] == G for r in await store.find("events", guild_id=G))


async def test_update_if_is_atomic_and_merges(store):
    await store.put("tasks", rec("t", status="pending", run_at="2026-10-01T00:00:00.000000Z", data={"a": {"x": 1}, "b": 2}))
    assert await store.update_if("tasks", "t", {"status": "pending"}, {"status": "running", "data": {"a": {"y": 2}, "b": None}})
    assert not await store.update_if("tasks", "t", {"status": "pending"}, {"status": "running"})
    got = await store.get("tasks", "t")
    assert got["status"] == "running" and got["data"] == {"a": {"x": 1, "y": 2}}


async def test_due_and_purge(store):
    await store.put("tasks", rec("a", status="pending", run_at="2026-10-01T00:00:00.000000Z"))
    await store.put("tasks", rec("b", status="pending", run_at="2026-12-01T00:00:00.000000Z"))
    await store.put("tasks", rec("c", status="done"))
    due = await store.find("tasks", where={"status": "pending"}, due_before="2026-11-01T00:00:00.000000Z", order="run_at")
    assert [r["id"] for r in due] == ["a"]
    n = await store.purge("tasks", before="2026-10-15T00:00:00.000000Z", where={"status": "done"})
    assert n == 1 and await store.get("tasks", "c") is None and await store.get("tasks", "a")


async def test_unknown_collection_rejected(store):
    with pytest.raises(StorageError):
        await store.find("users; drop table x")


async def test_audit_chain_filters_and_verify(store):
    for i in range(4):
        await store.append_audit(entry(event_id=f"e{i}", ts=f"2026-10-0{i + 1}T10:00:00.000000Z",
                                       action="role.create" if i % 2 else "channel.create", plan_id="p1" if i < 2 else None,
                                       target_id=9 if i == 3 else None))
    await store.append_audit(entry(guild=H, event_id="other"))
    rows = await store.query_audit(guild_id=G, limit=10)
    assert [r["event_id"] for r in rows] == ["e3", "e2", "e1", "e0"]
    assert len(await store.query_audit(guild_id=G, action="role.create")) == 2
    assert len(await store.query_audit(guild_id=G, plan_id="p1")) == 2
    assert len(await store.query_audit(guild_id=G, target_id=9)) == 1
    assert len(await store.query_audit(guild_id=G, since="2026-10-02T00:00:00.000000Z", until="2026-10-04T00:00:00.000000Z")) == 2
    assert len(await store.query_audit(guild_id=H)) == 1
    chain = [r async for r in store.audit_rows(G)]
    assert chain[0]["prev_hash"] == GENESIS
    assert verify_rows(chain, KEY)["ok"]
    assert not verify_rows(chain, b"wrong")["ok"]
    chain[1]["details"]["a"] = 999
    assert not verify_rows(chain, KEY)["ok"]


async def test_audit_is_append_only_in_turso():
    fake = FakeLibsql()
    s = TursoStore(fake, hmac_key=KEY)
    await s.open()
    await s.append_audit(entry())
    with pytest.raises(Exception):
        fake.db.execute("UPDATE audit SET status='x'")
    with pytest.raises(Exception):
        fake.db.execute("DELETE FROM audit")
    assert await s.prune_audit(G, "2027-01-01T00:00:00.000000Z") == 1


async def test_audit_survives_restart_json(tmp_path):
    s = JsonStore(tmp_path, hmac_key=KEY)
    await s.open()
    await s.append_audit(entry(event_id="a"))
    s2 = JsonStore(tmp_path, hmac_key=KEY)
    await s2.open()
    await s2.append_audit(entry(event_id="b"))
    rows = [r async for r in s2.audit_rows(G)]
    assert [r["event_id"] for r in rows] == ["a", "b"] and verify_rows(rows, KEY)["ok"]


async def test_audit_torn_line_recovered(tmp_path):
    s = JsonStore(tmp_path, hmac_key=KEY)
    await s.open()
    await s.append_audit(entry(event_id="a"))
    f = next((tmp_path / "audit" / str(G)).glob("*.jsonl"))
    with open(f, "ab") as fh:
        fh.write(b'{"event_id": "torn')
    s2 = JsonStore(tmp_path, hmac_key=KEY)
    await s2.open()
    await s2.append_audit(entry(event_id="b"))
    rows = [r async for r in s2.audit_rows(G)]
    assert [r["event_id"] for r in rows] == ["a", "b"] and verify_rows(rows, KEY)["ok"]


async def test_json_persists_across_instances(tmp_path):
    s = JsonStore(tmp_path)
    await s.open()
    await s.put("memories", rec("m", data={"text": "keep"}))
    s2 = JsonStore(tmp_path)
    await s2.open()
    assert (await s2.get("memories", "m", G))["data"]["text"] == "keep"


async def test_json_corruption_restores_backup_or_raises(tmp_path):
    s = JsonStore(tmp_path)
    await s.open()
    await s.put("memories", rec("m1"))
    await s.put("memories", rec("m2"))
    path = tmp_path / "memories" / f"{G}.json"
    path.write_text("{not json")
    s2 = JsonStore(tmp_path)
    await s2.open()
    assert await s2.get("memories", "m1", G) is not None
    assert list(path.parent.glob("*.corrupt-*"))
    path.write_text("garbage")
    path.with_name(path.name + ".bak").write_text("garbage too")
    s3 = JsonStore(tmp_path)
    await s3.open()
    with pytest.raises(StorageCorruption):
        await s3.get("memories", "m1", G)
    assert path.read_text() == "garbage"


async def test_json_rejects_newer_schema(tmp_path):
    (tmp_path / "_schema.json").write_text(json.dumps({"version": 99}))
    with pytest.raises(StorageError):
        await JsonStore(tmp_path).open()


def test_merge_patch():
    assert merge_patch({"a": {"b": 1, "c": 2}, "d": 1}, {"a": {"b": None, "e": 3}, "d": 2}) == {"a": {"c": 2, "e": 3}, "d": 2}
