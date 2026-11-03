from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from relay.utils.errors import ConfigError
from relay.utils.time_parser import TimeError, tz_of

ROOT = Path(__file__).resolve().parent.parent


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BotCfg(_M):
    name: str = Field("Relay", min_length=1, max_length=32)
    prefix: str = Field("r!", min_length=1, max_length=5)
    slash_commands: bool = True
    sync_commands_on_start: bool = True
    mention_trigger: bool = True


class GroqCfg(_M):
    model: str = "openai/gpt-oss-120b"
    temperature: float = Field(0.1, ge=0, le=1.5)
    max_tokens: int = Field(4000, ge=200, le=16000)
    timeout_seconds: float = Field(30, gt=0, le=120)
    max_retries: int = Field(2, ge=0, le=5)
    json_mode: Literal["json_object", "prompt"] = "json_object"
    reasoning_effort: Literal["low", "medium", "high"] | None = "low"


class LocalCfg(_M):
    directory: str = "data"


class TursoCfg(_M):
    enabled: bool = False


class StorageCfg(_M):
    mode: Literal["local", "turso"] = "local"
    local: LocalCfg = LocalCfg()
    turso: TursoCfg = TursoCfg()


class SchedulerCfg(_M):
    poll_seconds: float = Field(5, ge=1, le=60)
    batch_size: int = Field(10, ge=1, le=100)
    max_concurrent: int = Field(4, ge=1, le=32)
    max_tasks_per_guild: int = Field(100, ge=1, le=10000)
    retry_limit: int = Field(3, ge=0, le=10)
    backoff_seconds: float = Field(30, ge=1)
    stale_claim_seconds: float = Field(300, ge=30)
    late_grace_seconds: float = Field(21600, ge=0)
    min_recurring_seconds: float = Field(300, ge=60)
    max_consecutive_failures: int = Field(5, ge=1)


class ConfirmCfg(_M):
    ttl_seconds: int = Field(300, ge=30, le=3600)
    direct: Literal["never", "risky", "always"] = "risky"
    scheduled: Literal["never", "risky", "always"] = "always"
    recurring: Literal["never", "risky", "always"] = "always"
    slash_requires_confirmation: bool = True
    max_pending_per_user: int = Field(5, ge=1, le=50)


class AuditCfg(_M):
    enabled: bool = True
    fail_closed: bool = True
    retention_days: int = Field(0, ge=0)
    page_size: int = Field(10, ge=3, le=25)
    export_max_rows: int = Field(5000, ge=100, le=100000)


class LimitsCfg(_M):
    user_per_minute: int = Field(12, ge=1)
    guild_per_minute: int = Field(60, ge=1)
    ai_per_minute_per_guild: int = Field(30, ge=1)
    history_searches_per_minute: int = Field(6, ge=1)
    concurrent_plans_per_guild: int = Field(3, ge=1)
    actions_per_plan: int = Field(12, ge=1, le=50)
    max_request_chars: int = Field(1500, ge=100, le=4000)
    max_bulk_targets: int = Field(10, ge=1, le=50)
    max_history_fetch: int = Field(500, ge=10, le=2000)


class ContextCfg(_M):
    ttl_minutes: int = Field(30, ge=1)
    max_turns: int = Field(8, ge=1, le=30)


class MemoryCfg(_M):
    max_per_user: int = Field(50, ge=1)
    max_per_guild: int = Field(200, ge=1)
    max_chars: int = Field(400, ge=50, le=2000)
    event_retention_days: int = Field(90, ge=1)
    confirmation_retention_days: int = Field(7, ge=1)
    snapshot_retention_days: int = Field(30, ge=1)
    task_retention_days: int = Field(30, ge=1)


class PersonalityCfg(_M):
    role: str = Field("", max_length=600)
    tone: str = Field("calm, concise, natural", max_length=200)
    formality: Literal["casual", "neutral", "formal"] = "neutral"
    emoji: Literal["none", "light"] = "none"
    max_sentences: int = Field(3, ge=1, le=10)
    language: str = Field("", max_length=40)
    greeting: str = Field("", max_length=200)
    tasks: list[str] = Field(default_factory=list, max_length=10)
    avoid: list[str] = Field(default_factory=list, max_length=10)
    extra: str = Field("", max_length=800)

    @field_validator("tasks", "avoid")
    @classmethod
    def _items(cls, v):
        out = []
        for x in v:
            x = x.strip()
            if len(x) > 200:
                raise ValueError("each item must be 200 characters or fewer")
            if x:
                out.append(x)
        return out


def _default_groups() -> dict[str, list[str]]:
    return {
        "admin": ["*"],
        "moderator": ["@read", "warn_member", "remove_warning", "timeout_member", "untimeout_member",
                      "kick_member", "delete_message", "send_message", "remind", "set_nickname",
                      "query_warnings", "moderation_summary"],
        "trusted": ["remind", "recall", "remember", "forget", "list_tasks"],
        "restricted": [],
    }


class PolicyCfg(_M):
    administrator_is_admin: bool = True
    groups: dict[str, list[str]] = Field(default_factory=_default_groups)


class Escalation(_M):
    at: int = Field(ge=1)
    action: Literal["timeout", "kick"]
    duration: str = "1h"
    auto: bool = False


class ModerationCfg(_M):
    max_timeout_days: int = Field(28, ge=1, le=28)
    dm_on_warn: bool = True
    escalation: list[Escalation] = []


class GuildDefaults(_M):
    roles: dict[str, list[int]] = {}
    announcement_channel: int | None = None
    security_channel: int | None = None
    timezone: str | None = None


class PresenceCfg(_M):
    """How Relay shows up in Discord: online status plus an optional activity line."""
    status: Literal["online", "idle", "dnd", "invisible"] = "online"
    activity_type: Literal["none", "playing", "watching", "listening", "competing", "streaming", "custom"] = "none"
    text: str = Field("", max_length=128)
    url: str = Field("", max_length=200, description="stream link (Twitch or YouTube), only used for 'streaming'")
    rotate: list[str] = Field(default_factory=list, max_length=10,
                              description="extra lines to cycle through; {servers} {members} {prefix} are filled in")
    rotate_seconds: int = Field(60, ge=30, le=3600)

    @field_validator("rotate")
    @classmethod
    def _rot(cls, v):
        out = [x.strip()[:128] for x in v if isinstance(x, str) and x.strip()]
        return out

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        v = v.strip()
        if v and not re.match(r"^https://((www\.)?twitch\.tv|((www|m)\.)?youtube\.com|youtu\.be)/", v):
            raise ValueError("use a Twitch or YouTube link (https://...)")
        return v


class Config(_M):
    bot: BotCfg = BotCfg()
    groq: GroqCfg = GroqCfg()
    storage: StorageCfg = StorageCfg()
    timezone: str = "UTC"
    scheduler: SchedulerCfg = SchedulerCfg()
    confirmation: ConfirmCfg = ConfirmCfg()
    audit: AuditCfg = AuditCfg()
    limits: LimitsCfg = LimitsCfg()
    context: ContextCfg = ContextCfg()
    memory: MemoryCfg = MemoryCfg()
    personality: PersonalityCfg = PersonalityCfg()
    presence: PresenceCfg = PresenceCfg()
    policy: PolicyCfg = PolicyCfg()
    moderation: ModerationCfg = ModerationCfg()
    guilds: dict[int, GuildDefaults] = {}
    log_level: str = "INFO"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        try:
            tz_of(v)
        except TimeError as e:
            raise ValueError(str(e))
        return v

    @field_validator("log_level")
    @classmethod
    def _lvl(cls, v):
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        return v

    @field_validator("storage")
    @classmethod
    def _storage(cls, v):
        if v.mode == "turso" and not v.turso.enabled:
            raise ValueError("storage.mode is 'turso' but storage.turso.enabled is false")
        return v


class Secrets(BaseModel):
    discord_token: SecretStr
    groq_api_key: SecretStr
    turso_url: str | None = None
    turso_token: SecretStr | None = None
    audit_hmac_key: SecretStr | None = None


class Settings(BaseModel):
    cfg: Config
    secrets: Secrets
    base_dir: Path


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else ROOT / "config.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"config file {p} isn't valid JSON: {e}")
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {p} must contain a JSON object")
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_fmt(e))


def load_settings(path: str | Path | None = None, env: dict | None = None, *, require_discord: bool = True,
                  load_env_file: bool = True) -> Settings:
    if load_env_file and env is None:
        load_dotenv(ROOT / ".env")
    env = dict(os.environ) if env is None else env
    cfg = load_config(path)
    missing = []
    if require_discord and not env.get("DISCORD_TOKEN"):
        missing.append("DISCORD_TOKEN")
    if not env.get("GROQ_API_KEY"):
        missing.append("GROQ_API_KEY")
    if cfg.storage.mode == "turso":
        missing += [k for k in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN") if not env.get(k)]
    if missing:
        raise ConfigError("missing required environment variables: " + ", ".join(missing))
    sec = Secrets(
        discord_token=env.get("DISCORD_TOKEN") or "unset",
        groq_api_key=env["GROQ_API_KEY"],
        turso_url=env.get("TURSO_DATABASE_URL") or None,
        turso_token=env.get("TURSO_AUTH_TOKEN") or None,
        audit_hmac_key=env.get("AUDIT_HMAC_KEY") or None,
    )
    base = Path(path).resolve().parent if path else ROOT
    return Settings(cfg=cfg, secrets=sec, base_dir=base)


def _fmt(e: ValidationError) -> str:
    return "invalid config: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
