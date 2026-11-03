from __future__ import annotations

from pathlib import Path

from relay.config import Settings
from relay.storage.base import Store
from relay.storage.json_backend import JsonStore
from relay.storage.turso_backend import TursoStore
from relay.utils.errors import ConfigError


def build_store(s: Settings) -> Store:
    key = s.secrets.audit_hmac_key.get_secret_value().encode() if s.secrets.audit_hmac_key else None
    mode = s.cfg.storage.mode
    if mode == "local":
        d = Path(s.cfg.storage.local.directory)
        if not d.is_absolute():
            d = s.base_dir / d
        return JsonStore(d, hmac_key=key)
    if mode == "turso":
        if not s.secrets.turso_url or not s.secrets.turso_token:
            raise ConfigError("turso mode needs TURSO_DATABASE_URL and TURSO_AUTH_TOKEN")
        return TursoStore(url=s.secrets.turso_url, token=s.secrets.turso_token.get_secret_value(), hmac_key=key)
    raise ConfigError(f"unknown storage mode '{mode}'")
