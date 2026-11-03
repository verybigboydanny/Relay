from relay.storage.base import COLLECTIONS


def m001() -> list[str]:
    out = []
    for c in COLLECTIONS:
        out.append(
            f"CREATE TABLE IF NOT EXISTS {c} ("
            "id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL DEFAULT 0, ts TEXT NOT NULL, "
            "actor_id INTEGER, target_id INTEGER, kind TEXT, status TEXT, ref_id TEXT, "
            "run_at TEXT, expires_at TEXT, data TEXT NOT NULL)"
        )
        out.append(f"CREATE INDEX IF NOT EXISTS idx_{c}_guild_ts ON {c}(guild_id, ts)")
        out.append(f"CREATE INDEX IF NOT EXISTS idx_{c}_guild_kind ON {c}(guild_id, kind, status)")
        out.append(f"CREATE INDEX IF NOT EXISTS idx_{c}_actor ON {c}(guild_id, actor_id)")
        out.append(f"CREATE INDEX IF NOT EXISTS idx_{c}_target ON {c}(guild_id, target_id)")
        out.append(f"CREATE INDEX IF NOT EXISTS idx_{c}_due ON {c}(status, run_at)")
        out.append(f"CREATE INDEX IF NOT EXISTS idx_{c}_expiry ON {c}(expires_at)")
        out.append(f"CREATE INDEX IF NOT EXISTS idx_{c}_ref ON {c}(ref_id)")
    out.append(
        "CREATE TABLE IF NOT EXISTS audit ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL, "
        "guild_id INTEGER NOT NULL, actor_id INTEGER, actor_type TEXT NOT NULL, action TEXT NOT NULL, "
        "target_id INTEGER, resource_type TEXT, resource_id TEXT, request_id TEXT, plan_id TEXT, task_id TEXT, "
        "authorization_result TEXT, confirmation_status TEXT, status TEXT NOT NULL, reason TEXT, "
        "details TEXT NOT NULL, error_code TEXT, duration_ms INTEGER, schema_version INTEGER NOT NULL, "
        "prev_hash TEXT NOT NULL, hash TEXT NOT NULL, UNIQUE(guild_id, prev_hash))"
    )
    for name, cols in (("guild_ts", "guild_id, ts"), ("actor", "guild_id, actor_id"), ("target", "guild_id, target_id"),
                       ("action", "guild_id, action"), ("status", "guild_id, status"), ("request", "request_id"),
                       ("plan", "plan_id"), ("task", "task_id")):
        out.append(f"CREATE INDEX IF NOT EXISTS idx_audit_{name} ON audit({cols})")
    out.append("CREATE TABLE IF NOT EXISTS audit_gate (open INTEGER NOT NULL)")
    out.append("INSERT INTO audit_gate (open) SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM audit_gate)")
    out.append(
        "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit "
        "BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END"
    )
    out.append(
        "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit "
        "WHEN (SELECT open FROM audit_gate LIMIT 1) = 0 "
        "BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END"
    )
    return out


MIGRATIONS: list[tuple[int, callable]] = [(1, m001)]
LATEST = MIGRATIONS[-1][0]
