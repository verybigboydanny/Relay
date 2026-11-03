from __future__ import annotations

import json

import pytest

from relay.config import AuditCfg
from relay.services.audit_service import AuditService
from relay.utils.errors import AuditFailure, StorageError

G, H = 1001, 2002


async def test_record_and_search_filters(app):
    a = app.audit
    await a.record(guild_id=G, action="ban_member", actor_id=1, target_id=2, plan_id="p1", request_id="r1", status="success",
                   authorization_result="allowed", confirmation_status="approved", resource_type="member", resource_id=2)
    await a.record(guild_id=G, action="ban_member", actor_id=1, target_id=3, status="denied", authorization_result="denied")
    await a.record(guild_id=H, action="ban_member", actor_id=1, target_id=2)
    assert len(await a.search(G)) == 2
    assert len(await a.search(G, status="denied")) == 1
    assert len(await a.search(G, target_id=2)) == 1
    assert len(await a.search(G, plan_id="p1")) == 1
    assert len(await a.search(G, request_id="r1")) == 1
    assert len(await a.search(H)) == 1
    row = (await a.search(G, plan_id="p1"))[0]
    assert row["schema_version"] == 1 and len(row["hash"]) == 64 and row["resource_id"] == "2"


async def test_details_are_redacted_and_bounded(app):
    await app.audit.record(guild_id=G, action="x", details={"token": "abc", "note": "my token is gsk_" + "z" * 30, "long": "y" * 5000,
                                                              "nested": {"api_key": "k", "ok": 1}})
    d = (await app.audit.search(G))[0]["details"]
    blob = json.dumps(d)
    assert d["token"] == "[redacted]" and "gsk_" not in blob and len(d["long"]) <= 300 and d["nested"] == {"api_key": "[redacted]", "ok": 1}


async def test_chain_verifies_and_detects_tampering(app):
    for i in range(5):
        await app.audit.record(guild_id=G, action=f"a{i}", actor_id=i)
    assert (await app.audit.verify(G))["ok"]
    store = app.store
    if store.mode == "local":
        f = next((store.root / "audit" / str(G)).glob("*.jsonl"))
        lines = f.read_text().splitlines()
        row = json.loads(lines[2])
        row["actor_id"] = 999
        lines[2] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        f.write_text("\n".join(lines) + "\n")
        res = await app.audit.verify(G)
        assert not res["ok"] and res["problems"][0]["problem"] == "hash mismatch"
    else:
        with pytest.raises(StorageError):
            await store._exec("UPDATE audit SET actor_id=999")


async def test_hmac_key_changes_hashes(app):
    await app.audit.record(guild_id=G, action="a")
    assert (await app.audit.verify(G))["keyed"]
    app.store.hmac_key = b"other"
    assert not (await app.audit.verify(G))["ok"]


async def test_strict_write_raises_but_lenient_swallows(app, monkeypatch):
    async def boom(entry):
        raise StorageError("disk full")
    monkeypatch.setattr(app.store, "append_audit", boom)
    assert await app.audit.record(guild_id=G, action="x") is None
    with pytest.raises(AuditFailure) as e:
        await app.audit.record(guild_id=G, action="x", strict=True)
    assert "didn't do that" in e.value.user_message


async def test_disabled_audit_writes_nothing(app):
    svc = AuditService(app.store, AuditCfg(enabled=False))
    assert await svc.record(guild_id=G, action="x") is None
    assert await app.audit.search(G) == []


async def test_notifier_fires_for_failures_only(app):
    seen = []

    async def n(gid, row):
        seen.append(row["action"])
    app.audit.notifier = n
    await app.audit.record(guild_id=G, action="create_channel", status="success")
    await app.audit.record(guild_id=G, action="create_channel", status="denied")
    await app.audit.record(guild_id=G, action="ban_member", status="success")
    import asyncio
    await asyncio.sleep(0.05)
    assert sorted(seen) == ["ban_member", "create_channel"]


async def test_notifier_failure_does_not_break_audit(app):
    async def n(gid, row):
        raise RuntimeError("channel gone")
    app.audit.notifier = n
    assert await app.audit.record(guild_id=G, action="x", status="failure")
    import asyncio
    await asyncio.sleep(0.05)
    assert len(await app.audit.search(G)) == 1


async def test_export_is_jsonl_and_capped(app):
    app.audit.cfg.export_max_rows = 100
    for i in range(3):
        await app.audit.record(guild_id=G, action=f"a{i}")
    data, n = await app.audit.export_jsonl(G)
    rows = [json.loads(x) for x in data.decode().splitlines()]
    assert n == 3 and [r["action"] for r in rows] == ["a0", "a1", "a2"]
    app.audit.cfg.export_max_rows = 100
    _, n = await app.audit.export_jsonl(G, since="2999-01-01T00:00:00.000000Z")
    assert n == 0


async def test_prune_respects_retention_and_records_itself(app):
    await app.audit.record(guild_id=G, action="old")
    assert await app.audit.prune(G, 5) == 0
    app.audit.cfg.retention_days = 1
    assert await app.audit.prune(G, 5) == 0
    assert any(r["action"] == "old" for r in await app.audit.search(G))
