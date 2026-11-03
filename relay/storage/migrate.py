from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path

from relay.config import load_settings
from relay.storage.base import COLLECTIONS, Store
from relay.storage.json_backend import JsonStore
from relay.storage.turso_backend import TursoStore
from relay.utils.errors import ConfigError, StorageError

PAGE = 500


@dataclass
class Report:
    copied: dict = field(default_factory=dict)
    skipped_same: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    audit: dict = field(default_factory=dict)
    audit_conflicts: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.conflicts and not self.audit_conflicts


async def migrate(src: Store, dst: Store, *, dry_run: bool = False, guild_id: int | None = None) -> Report:
    rep = Report()
    for coll in COLLECTIONS:
        offset, n_copy, n_same = 0, 0, 0
        while True:
            rows = await src.find(coll, guild_id=guild_id, order="id", limit=PAGE, offset=offset)
            if not rows:
                break
            offset += len(rows)
            for r in rows:
                existing = await dst.get(coll, r["id"], r["guild_id"])
                if existing is None:
                    if not dry_run:
                        await dst.put(coll, copy.deepcopy(r))
                    n_copy += 1
                elif existing == r:
                    n_same += 1
                else:
                    rep.conflicts.append((coll, r["id"], r["guild_id"]))
        rep.copied[coll] = n_copy
        rep.skipped_same[coll] = n_same
    guilds = [guild_id] if guild_id is not None else await src.guild_ids()
    for gid in guilds:
        rows = [r async for r in src.audit_rows(gid)]
        if not rows:
            continue
        have = await dst.query_audit(guild_id=gid, limit=1)
        if have:
            same = [r async for r in dst.audit_rows(gid)] == rows
            if not same:
                rep.audit_conflicts.append(gid)
            rep.audit[gid] = 0
            continue
        if dry_run:
            rep.audit[gid] = len(rows)
            continue
        try:
            rep.audit[gid] = await dst.import_audit(gid, rows)
        except StorageError as e:
            rep.audit_conflicts.append((gid, str(e)))
    return rep


def format_report(rep: Report, dry_run: bool) -> str:
    lines = ["Dry run, nothing written." if dry_run else "Migration finished."]
    for c in COLLECTIONS:
        if rep.copied.get(c) or rep.skipped_same.get(c):
            lines.append(f"  {c}: {rep.copied.get(c, 0)} {'to copy' if dry_run else 'copied'}, {rep.skipped_same.get(c, 0)} already identical")
    for gid, n in rep.audit.items():
        lines.append(f"  audit guild {gid}: {n} entries {'to import' if dry_run else 'imported'}")
    for c in rep.conflicts:
        lines.append(f"  CONFLICT {c[0]} {c[1]} (guild {c[2]}): destination differs; left untouched")
    for c in rep.audit_conflicts:
        lines.append(f"  CONFLICT audit {c}: destination already has different audit entries; left untouched")
    lines.append("No conflicts." if rep.clean else "Resolve the conflicts above before relying on the destination.")
    return "\n".join(lines)


async def _open(kind: str, settings) -> Store:
    key = settings.secrets.audit_hmac_key.get_secret_value().encode() if settings.secrets.audit_hmac_key else None
    if kind == "local":
        d = Path(settings.cfg.storage.local.directory)
        s: Store = JsonStore(d if d.is_absolute() else settings.base_dir / d, hmac_key=key)
    else:
        if not settings.secrets.turso_url or not settings.secrets.turso_token:
            raise ConfigError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are required for turso")
        s = TursoStore(url=settings.secrets.turso_url, token=settings.secrets.turso_token.get_secret_value(), hmac_key=key)
    await s.open()
    return s


async def amain(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Copy Relay data between local JSON and Turso. The source is never modified.")
    ap.add_argument("--from", dest="src", choices=["local", "turso"], required=True)
    ap.add_argument("--to", dest="dst", choices=["local", "turso"], required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--guild", type=int, default=None, help="only migrate one guild")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.src == a.dst:
        print("source and destination are the same backend", file=sys.stderr)
        return 2
    try:
        settings = load_settings(a.config, require_discord=False)
        src, dst = await _open(a.src, settings), await _open(a.dst, settings)
    except (ConfigError, StorageError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        rep = await migrate(src, dst, dry_run=a.dry_run, guild_id=a.guild)
        print(format_report(rep, a.dry_run))
        return 0 if rep.clean else 1
    finally:
        await src.close()
        await dst.close()


def main() -> None:
    sys.exit(asyncio.run(amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
