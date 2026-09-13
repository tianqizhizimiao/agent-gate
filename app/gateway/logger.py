"""Per-tool-group tool-call logger.

Each tool group keeps its own ``logs.db`` (inside the group's package folder
under ``.agentgate/``). Only tool invocations are recorded — normal calls and
exceptions — and entries older than the retention window (1 hour by default)
are pruned on every write and read. Nothing else (prompts, chat content,
upstream traffic) is ever logged here.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_DETAIL_LIMIT = 2000


class ToolLogStore:
    def __init__(self, path: str | Path, retention_seconds: int = 3600):
        self.path = Path(path)
        self.retention = int(retention_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS tool_logs ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  created_at REAL NOT NULL,"
                "  tool_name TEXT NOT NULL,"
                "  status TEXT NOT NULL,"      # 'normal' | 'exception'
                "  duration_ms INTEGER NOT NULL,"
                "  detail TEXT NOT NULL DEFAULT ''"
                ")"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON tool_logs(created_at)")
            c.commit()

    def _prune(self, c) -> None:
        cutoff = time.time() - self.retention
        c.execute("DELETE FROM tool_logs WHERE created_at < ?", (cutoff,))

    def add(self, tool_name: str, status: str, duration_ms: int, detail: str) -> None:
        if detail and len(detail) > _DETAIL_LIMIT:
            detail = detail[:_DETAIL_LIMIT] + "…"
        with self._conn() as c:
            self._prune(c)
            c.execute(
                "INSERT INTO tool_logs (created_at, tool_name, status, duration_ms, detail) VALUES (?,?,?,?,?)",
                (time.time(), tool_name, status, int(duration_ms), detail or ""),
            )
            c.commit()

    def list(self, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            self._prune(c)
            rows = c.execute(
                "SELECT created_at, tool_name, status, duration_ms, detail FROM tool_logs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            c.commit()
        return [dict(r) for r in rows]
