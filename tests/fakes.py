from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class FakeResult:
    columns: list
    rows: list
    rows_affected: int = 0
    last_insert_rowid: int | None = None


class FakeLibsql:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        self.closed_flag = False

    def _run(self, sql, args):
        cur = self.db.execute(sql, args or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [tuple(r) for r in cur.fetchall()] if cur.description else []
        return FakeResult(cols, rows, cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)

    async def execute(self, stmt, args=None):
        return self._run(stmt, args)

    async def batch(self, stmts):
        out = []
        self.db.execute("BEGIN")
        try:
            for s in stmts:
                sql, args = (s, []) if isinstance(s, str) else s
                out.append(self._run(sql, args))
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")
        return out

    async def close(self):
        self.closed_flag = True
