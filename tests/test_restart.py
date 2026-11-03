from __future__ import annotations

import json
import types

from relay.app import App
from relay.config import load_settings
from relay.storage.factory import build_store
from relay.storage.base import verify_rows
from tests.conftest import FakeGroq, ROOT, make_settings


async def boot(tmp_path):
    s = make_settings(tmp_path, storage={"mode": "local", "local": {"directory": str(tmp_path / "data")}})
    store = build_store(s)
    await store.open()
    return App(s, store, groq=FakeGroq(), bot=types.SimpleNamespace(get_guild=lambda i: None, get_channel=lambda i: None)), store


async def test_memory_audit_and_tasks_survive_process_restart(tmp_path):
    app, store = await boot(tmp_path)
    await app.memory.add_memory(5, "user", 9, "reply briefly", key="style")
    await app.memory.add_turn(5, 1, 9, "make a channel", "done", {"channel": {"id": 3, "name": "lol"}})
    await app.memory.record_event(5, "channel.create", actor_id=9, resource_id=3)
    await app.memory.add_warning(5, 77, 9, "spam", "p")
    await app.audit.record(guild_id=5, action="create_channel", actor_id=9)
    await store.close()
    app2, store2 = await boot(tmp_path)
    assert (await app2.memory.list_memories(5, "user", 9))[0]["data"]["text"] == "reply briefly"
    assert (await app2.memory.context(5, 1, 9))["entities"]["channel"]["name"] == "lol"
    assert len(await app2.memory.query_events(5)) == 1 and len(await app2.memory.list_warnings(5, 77)) == 1
    await app2.audit.record(guild_id=5, action="after_restart")
    rows = [r async for r in store2.audit_rows(5)]
    assert [r["action"] for r in rows] == ["create_channel", "after_restart"] and verify_rows(rows, None)["ok"]
    await store2.close()


async def test_deleted_memory_is_gone_from_disk(tmp_path):
    app, store = await boot(tmp_path)
    rec = await app.memory.add_memory(5, "user", 9, "super private preference text")
    await app.memory.delete_memory(5, rec["id"], "user", 9)
    for p in (tmp_path / "data").rglob("*"):
        if p.is_file():
            assert "super private preference text" not in p.read_text(errors="ignore"), p
    await store.close()


def test_startup_fails_clearly_without_credentials(tmp_path):
    import subprocess, sys, os
    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "GROQ_API_KEY")}
    out = subprocess.run([sys.executable, "main.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 2 and "DISCORD_TOKEN" in out.stderr and "GROQ_API_KEY" in out.stderr
