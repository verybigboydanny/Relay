from __future__ import annotations

import pytest

from relay.services.audit_service import AuditService
from relay.config import AuditCfg
from relay.storage.base import new_rec, verify_rows
from relay.storage.json_backend import JsonStore
from relay.storage.migrate import format_report, migrate
from relay.storage.turso_backend import TursoStore
from tests.conftest import KEY
from tests.fakes import FakeLibsql

G, H = 101, 202


async def seed(store):
    a = AuditService(store, AuditCfg())
    for g in (G, H):
        for i in range(4):
            await a.record(guild_id=g, action=f"act{i}", actor_id=i, details={"n": i})
        await store.put("memories", new_rec(f"m{g}", g, {"text": "keep me"}, ts="2026-10-01T00:00:00.000000Z", actor_id=7, kind="preference", status="user"))
        await store.put("tasks", new_rec(f"t{g}", g, {"tool": "x"}, ts="2026-10-01T00:00:00.000000Z", status="pending", run_at="2026-12-01T00:00:00.000000Z"))
    await store.put("warnings", new_rec("w1", G, {"reason": "spam"}, ts="2026-10-01T00:00:00.000000Z", target_id=9, status="active"))


@pytest.mark.parametrize("direction", ["local_to_turso", "turso_to_local"])
async def test_migration_preserves_ids_data_and_audit_chain(tmp_path, direction):
    local, turso = JsonStore(tmp_path / "d", hmac_key=KEY), TursoStore(FakeLibsql(), hmac_key=KEY)
    await local.open(); await turso.open()
    src, dst = (local, turso) if direction == "local_to_turso" else (turso, local)
    await seed(src)
    dry = await migrate(src, dst, dry_run=True)
    assert dry.copied["memories"] == 2 and await dst.count("memories") == 0 and dry.clean
    rep = await migrate(src, dst)
    assert rep.clean and rep.copied["memories"] == 2 and rep.copied["warnings"] == 1 and rep.audit == {G: 4, H: 4}
    assert (await dst.get("memories", f"m{G}", G))["data"] == {"text": "keep me"}
    assert (await dst.get("tasks", f"t{H}", H))["run_at"] == "2026-12-01T00:00:00.000000Z"
    for g in (G, H):
        a, b = [r async for r in src.audit_rows(g)], [r async for r in dst.audit_rows(g)]
        assert [r["event_id"] for r in a] == [r["event_id"] for r in b]
        assert [r["hash"] for r in a] == [r["hash"] for r in b]
        assert verify_rows(b, KEY)["ok"]
    after = await AuditService(dst, AuditCfg()).record(guild_id=G, action="post-migration")
    assert after and verify_rows([r async for r in dst.audit_rows(G)], KEY)["ok"]
    again = await migrate(src, dst)
    assert again.copied["memories"] == 0 and again.skipped_same["memories"] == 2 and again.audit_conflicts


async def test_conflicting_records_are_reported_and_never_overwritten(tmp_path):
    local, turso = JsonStore(tmp_path / "d"), TursoStore(FakeLibsql())
    await local.open(); await turso.open()
    await local.put("memories", new_rec("m", G, {"text": "from local"}, ts="2026-10-01T00:00:00.000000Z", status="user"))
    await turso.put("memories", new_rec("m", G, {"text": "from turso"}, ts="2026-10-01T00:00:00.000000Z", status="user"))
    rep = await migrate(local, turso)
    assert not rep.clean and rep.conflicts == [("memories", "m", G)]
    assert (await turso.get("memories", "m", G))["data"]["text"] == "from turso"
    assert "CONFLICT" in format_report(rep, False)


async def test_tampered_source_audit_is_refused(tmp_path):
    local, turso = JsonStore(tmp_path / "d", hmac_key=KEY), TursoStore(FakeLibsql(), hmac_key=KEY)
    await local.open(); await turso.open()
    await seed(local)
    f = sorted((tmp_path / "d" / "audit" / str(G)).glob("*.jsonl"))[0]
    lines = f.read_text().splitlines()
    lines[1] = lines[1].replace('"actor_id":1', '"actor_id":42')
    f.write_text("\n".join(lines) + "\n")
    rep = await migrate(local, turso)
    assert any(isinstance(c, tuple) and c[0] == G for c in rep.audit_conflicts)
    assert await turso.query_audit(guild_id=G) == []
    assert len(await turso.query_audit(guild_id=H)) == 4


async def test_single_guild_migration(tmp_path):
    local, turso = JsonStore(tmp_path / "d", hmac_key=KEY), TursoStore(FakeLibsql(), hmac_key=KEY)
    await local.open(); await turso.open()
    await seed(local)
    rep = await migrate(local, turso, guild_id=H)
    assert rep.audit == {H: 4} and await turso.count("memories", guild_id=G) == 0 and await turso.count("memories", guild_id=H) == 1
