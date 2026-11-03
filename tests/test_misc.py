from __future__ import annotations

import logging

import pytest

from relay.config import LimitsCfg
from relay.logging_setup import ScrubFilter, scrub, setup_logging
from relay.services.limits import Limiter
from relay.utils.errors import RateLimited
from relay.utils.redact import has_secret, redact, scrub_text


def test_rate_limits_per_user_and_guild(monkeypatch):
    lim = Limiter(LimitsCfg(user_per_minute=2, guild_per_minute=3))
    lim.check_request(1, 10)
    lim.check_request(1, 10)
    with pytest.raises(RateLimited) as e:
        lim.check_request(1, 10)
    assert e.value.retry_after > 0
    lim.check_request(1, 11)
    with pytest.raises(RateLimited):
        lim.check_request(1, 12)
    lim.check_request(2, 10)


def test_window_expires(monkeypatch):
    import relay.services.limits as L
    t = [1000.0]
    monkeypatch.setattr(L.time, "monotonic", lambda: t[0])
    lim = Limiter(LimitsCfg(user_per_minute=1))
    lim.check_request(1, 1)
    with pytest.raises(RateLimited):
        lim.check_request(1, 1)
    t[0] += 61
    lim.check_request(1, 1)


def test_ai_and_history_limits_and_plan_slots():
    lim = Limiter(LimitsCfg(ai_per_minute_per_guild=1, history_searches_per_minute=1, concurrent_plans_per_guild=2))
    lim.check_ai(1)
    with pytest.raises(RateLimited):
        lim.check_ai(1)
    lim.check_history(1)
    with pytest.raises(RateLimited):
        lim.check_history(1)
    assert lim.acquire_plan(1) and lim.acquire_plan(1) and not lim.acquire_plan(1)
    lim.release_plan(1)
    assert lim.acquire_plan(1) and lim.acquire_plan(2)
    for _ in range(5):
        lim.release_plan(1)
    assert lim._active[1] == 0


DISCORD = ""


def test_secret_scrubbing():
    text = f"login {DISCORD} and gsk_{'a' * 30} Authorization: Bearer abc.def password=hunter22222"
    out = scrub(text)
    assert DISCORD not in out and "gsk_" not in out and "abc.def" not in out and "hunter22222" not in out
    assert has_secret("api key is 123456789abc") and not has_secret("I like the blue channel")
    assert scrub_text("x" * 400, 50).endswith("…") and len(scrub_text("x" * 400, 50)) == 50


def test_redact_nested():
    d = redact({"a": [{"Authorization": "x"}, "fine"], "b": {"client_secret": "y", "deep": {"deeper": {"d": {"d": {"d": {"d": 1}}}}}}})
    assert d["a"][0]["Authorization"] == "[redacted]" and d["b"]["client_secret"] == "[redacted]"


def test_log_files_never_contain_secrets(tmp_path):
    setup_logging("INFO", tmp_path)
    log = logging.getLogger("relay.test")
    log.error("failed with token %s and key %s", DISCORD, "gsk_" + "b" * 30)
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "relay.log").read_text()
    assert "[redacted]" in text and DISCORD not in text and "gsk_bbbb" not in text
    assert any(isinstance(f, ScrubFilter) for h in logging.getLogger().handlers for f in h.filters)


async def test_groq_client_drops_unsupported_params_and_reports_missing_model():
    import groq, httpx
    from relay.ai.groq_client import GroqClient
    from relay.config import GroqCfg
    from types import SimpleNamespace

    seen = []

    class Completions:
        async def create(self, **kw):
            seen.append(dict(kw))
            if "reasoning_effort" in kw:
                raise groq.BadRequestError("reasoning_effort is not supported", response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
            if "response_format" in kw:
                raise groq.BadRequestError("response_format json unsupported", response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"type":"reply","text":"hi"}'))])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    c = GroqClient("k", GroqCfg(), client=fake)
    assert "hi" in await c.chat_json([{"role": "user", "content": "x"}])
    assert "reasoning_effort" in seen[0] and "reasoning_effort" not in seen[-1] and "response_format" not in seen[-1]
    assert not c._json_ok and c._effort is None

    class Missing:
        async def create(self, **kw):
            raise groq.NotFoundError("no such model", response=httpx.Response(404, request=httpx.Request("POST", "http://x")), body=None)

    c2 = GroqClient("k", GroqCfg(), client=SimpleNamespace(chat=SimpleNamespace(completions=Missing())))
    from relay.utils.errors import AIError
    with pytest.raises(AIError) as e:
        await c2.chat_json([])
    assert "openai/gpt-oss-120b" in e.value.user_message
