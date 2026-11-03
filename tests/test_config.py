from __future__ import annotations

import json

import pytest

from relay.config import load_config, load_settings
from relay.utils.errors import ConfigError

ENV = {"DISCORD_TOKEN": "t" * 30, "GROQ_API_KEY": "gsk_" + "k" * 30}


def write(tmp_path, data):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data))
    return p


def test_shipped_config_is_valid():
    cfg = load_config()
    assert cfg.storage.mode == "local" and cfg.bot.name == "Relay"
    assert "moderator" in cfg.policy.groups


def test_missing_secrets_fail_clearly(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(write(tmp_path, {}), env={}, load_env_file=False)
    assert "DISCORD_TOKEN" in str(e.value) and "GROQ_API_KEY" in str(e.value)


def test_turso_mode_needs_flag_and_credentials(tmp_path):
    with pytest.raises(ConfigError, match="enabled"):
        load_config(write(tmp_path, {"storage": {"mode": "turso"}}))
    p = write(tmp_path, {"storage": {"mode": "turso", "turso": {"enabled": True}}})
    with pytest.raises(ConfigError, match="TURSO_DATABASE_URL"):
        load_settings(p, env=ENV, load_env_file=False)
    s = load_settings(p, env={**ENV, "TURSO_DATABASE_URL": "libsql://x", "TURSO_AUTH_TOKEN": "tok"}, load_env_file=False)
    assert s.cfg.storage.mode == "turso"


@pytest.mark.parametrize("bad", [
    {"timezone": "Mars/Base"}, {"unknown_key": 1}, {"groq": {"temperature": 9}}, {"storage": {"mode": "mongo"}},
    {"limits": {"actions_per_plan": 0}}, {"log_level": "LOUD"}, {"confirmation": {"direct": "sometimes"}},
])
def test_invalid_settings_rejected(tmp_path, bad):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, bad))


def test_secrets_never_in_repr(tmp_path):
    s = load_settings(write(tmp_path, {}), env=ENV, load_env_file=False)
    assert "gsk_" not in repr(s) and "gsk_" not in str(s.secrets)


def test_bad_json_and_missing_file(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{nope")
    with pytest.raises(ConfigError):
        load_config(p)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.json")
