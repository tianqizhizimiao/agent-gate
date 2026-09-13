"""Global database for users, API keys, tool groups, membership and usage.

One global SQLite database holds everything *except* per-tool-group data
(tools table + tool-call logs), which lives in each tool group's own DB for
isolation — mirroring ContextusAgent's per-knowledge-base isolation.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Optional

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,            -- username is the unique id
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS registration_tokens (
    token TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_by TEXT,
    used_at REAL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    key_hash TEXT UNIQUE NOT NULL,      -- sha256，用于鉴权查找
    key_plain TEXT NOT NULL DEFAULT '', -- 明文密钥，用于事后在控制台复制
    key_prefix TEXT NOT NULL,
    user_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    upstream_name TEXT NOT NULL DEFAULT '',
    upstream_base_url TEXT NOT NULL DEFAULT '',
    upstream_api_key TEXT NOT NULL DEFAULT '',
    upstream_model TEXT NOT NULL DEFAULT '',
    upstream_models TEXT NOT NULL DEFAULT '[]',  -- JSON 数组：可用的模型列表
    tool_group_id TEXT,             -- nullable; one api key -> at most one tool group
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (tool_group_id) REFERENCES tool_groups(id)
);

-- 账户级上游 API（渠道）：一个用户可配置多个，模型并集对下游开放
CREATE TABLE IF NOT EXISTS upstreams (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    protocol TEXT NOT NULL DEFAULT '',        -- 探测出的协议：openai/anthropic/gemini/dashscope
    models TEXT NOT NULL DEFAULT '[]',        -- JSON 数组：该上游开放的模型
    default_model TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS tool_groups (
    id TEXT PRIMARY KEY,            -- uuid, also the join id
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    folder_name TEXT NOT NULL,      -- on-disk package folder name (valid identifier)
    created_at REAL NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS tool_group_members (
    tool_group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    joined_at REAL NOT NULL,
    PRIMARY KEY (tool_group_id, user_id),
    FOREIGN KEY (tool_group_id) REFERENCES tool_groups(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    tool_group_id TEXT,
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_apikeys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_tg_owner ON tool_groups(owner_id);
CREATE INDEX IF NOT EXISTS idx_upstreams_user ON upstreams(user_id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(config.GLOBAL_DB_PATH),
        timeout=30,
        isolation_level=None,  # autocommit; we manage transactions explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """为旧库补上后续新增的列/表数据（幂等）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "upstream_models" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN upstream_models TEXT NOT NULL DEFAULT '[]'")
    if "key_plain" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_plain TEXT NOT NULL DEFAULT ''")

    ucols = {r["name"] for r in conn.execute("PRAGMA table_info(upstreams)").fetchall()}
    if "protocol" not in ucols:
        conn.execute("ALTER TABLE upstreams ADD COLUMN protocol TEXT NOT NULL DEFAULT ''")

    # 把旧的「密钥自带上游」迁移为账户级上游（同用户 + 同地址 + 同密钥则去重）
    legacy = conn.execute(
        "SELECT user_id, upstream_name, upstream_base_url, upstream_api_key, "
        "upstream_model, upstream_models FROM api_keys "
        "WHERE TRIM(COALESCE(upstream_base_url, '')) <> ''"
    ).fetchall()
    for r in legacy:
        dup = conn.execute(
            "SELECT 1 FROM upstreams WHERE user_id = ? AND base_url = ? AND api_key = ?",
            (r["user_id"], r["upstream_base_url"], r["upstream_api_key"]),
        ).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO upstreams (id, user_id, name, base_url, api_key, models, default_model, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                r["user_id"],
                r["upstream_name"] or "",
                r["upstream_base_url"],
                r["upstream_api_key"],
                r["upstream_models"] or "[]",
                r["upstream_model"] or "",
                time.time(),
            ),
        )


@contextmanager
def transaction(defer_fk: bool = False):
    """把多条语句放进一个事务；异常时整体回滚。

    ``defer_fk=True`` 会把外键检查推迟到 COMMIT —— 用于「改名」这类
    需要先更新子表、再更新父表（中间态短暂不一致）的级联操作。
    """
    conn = _connect()
    try:
        conn.execute("BEGIN")
        if defer_fk:
            conn.execute("PRAGMA defer_foreign_keys=ON")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def query(sql: str, params: Iterable[Any] | tuple = ()) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] | tuple = ()) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] | tuple = ()) -> sqlite3.Cursor:
    with get_conn() as conn:
        return conn.execute(sql, tuple(params))


def now() -> float:
    return time.time()
