"""下游 API 密钥的生成、校验与查找。

命名规则：
  * 默认（不填自定义）：``sk-<48 位随机字母数字>``
  * 手动定义：``sk-<自定义内容>-<4 位随机字母数字>``

密钥同时以 **哈希**（用于鉴权查找）和 **明文**（用于事后在控制台复制）保存。
明文只存在本地 SQLite 里，请保护好 ``data/`` 目录。
"""
from __future__ import annotations

import hashlib
import re
import secrets
import string

from . import database

PREFIX = "sk-"
_ALPHABET = string.ascii_letters + string.digits
RANDOM_LEN = 48          # 默认随机部分的长度
SUFFIX_LEN = 4           # 自定义模式下追加的随机长度
CUSTOM_MAX = 40
_CUSTOM_RE = re.compile(r"^[A-Za-z0-9._-]{%d,%d}$" % (2, CUSTOM_MAX))


def random_tail(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def normalize_custom(custom: str) -> str:
    """去掉首尾空白与多余的短横线。"""
    return (custom or "").strip().strip("-.")


def validate_custom(custom: str) -> str | None:
    """校验用户自定义部分；返回中文错误信息，合法则返回 None。"""
    custom = normalize_custom(custom)
    if not custom:
        return None
    if not _CUSTOM_RE.match(custom):
        return f"自定义部分只能包含字母、数字、点、下划线和短横线，长度 2–{CUSTOM_MAX}"
    return None


def build_key(custom: str = "") -> str:
    custom = normalize_custom(custom)
    if custom:
        return f"{PREFIX}{custom}-{random_tail(SUFFIX_LEN)}"
    return f"{PREFIX}{random_tail(RANDOM_LEN)}"


def generate_api_key(custom: str = "") -> tuple[str, str, str]:
    """返回 ``(完整密钥, 哈希, 前缀)``。"""
    full = build_key(custom)
    key_hash = hashlib.sha256(full.encode("utf-8")).hexdigest()
    key_prefix = full[:12]
    return full, key_hash, key_prefix


def hash_key(full: str) -> str:
    return hashlib.sha256(full.encode("utf-8")).hexdigest()


def key_exists(key_hash: str) -> bool:
    return database.query_one("SELECT 1 FROM api_keys WHERE key_hash = ?", (key_hash,)) is not None


def generate_unique_key(custom: str = "", attempts: int = 8) -> tuple[str, str, str]:
    """生成一个哈希不冲突的密钥（自定义模式下随机后缀冲突时自动重试）。"""
    for _ in range(attempts):
        full, key_hash, key_prefix = generate_api_key(custom)
        if not key_exists(key_hash):
            return full, key_hash, key_prefix
    raise RuntimeError("生成密钥失败：多次重试仍冲突，请换一个自定义内容")


def lookup_api_key(full_key: str) -> dict | None:
    """Resolve a raw API key (from the Authorization header) to its DB row."""
    if not full_key:
        return None
    key_hash = hash_key(full_key)
    row = database.query_one("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,))
    if row is None:
        return None
    d = dict(row)
    # Only usable if the owning user is active.
    user = database.query_one("SELECT active FROM users WHERE id = ?", (d["user_id"],))
    if user is None or not user["active"]:
        return None
    return d
