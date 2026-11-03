from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from relay.app import App
from relay.config import load_settings
from relay.storage.json_backend import JsonStore
from relay.storage.turso_backend import TursoStore
from tests.fakes import FakeLibsql

KEY = b"test-key"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(params=["local", "turso"])
async def store(request, tmp_path):
    if request.param == "local":
        s = JsonStore(tmp_path / "data", hmac_key=KEY)
    else:
        s = TursoStore(FakeLibsql(), hmac_key=KEY)
    await s.open()
    yield s
    await s.close()


class FakeGroq:
    model = "fake-model"

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []

    async def chat_json(self, messages):
        self.calls.append(("json", messages))
        if not self.replies:
            raise AssertionError("FakeGroq ran out of replies")
        r = self.replies.pop(0)
        return json.dumps(r) if isinstance(r, dict) else r

    async def chat_text(self, messages, max_tokens=700):
        self.calls.append(("text", messages))
        return self.replies.pop(0) if self.replies else "ok"

    async def ping(self):
        return {"ok": True, "model": "fake", "model_available": True, "json_mode": True}


def make_settings(tmp_path, **overrides):
    cfg = json.loads((ROOT / "config.json").read_text())
    for k, v in overrides.items():
        cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) else v
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return load_settings(p, env={"DISCORD_TOKEN": "x" * 30, "GROQ_API_KEY": "gsk_" + "a" * 30}, load_env_file=False)


@pytest.fixture
async def app(tmp_path, store):
    s = make_settings(tmp_path)
    a = App(s, store, groq=FakeGroq(), bot=types.SimpleNamespace(get_guild=lambda i: None, get_channel=lambda i: None, user=types.SimpleNamespace(id=1)))
    return a
