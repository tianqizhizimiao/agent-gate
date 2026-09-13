"""Per-tool-group SQLite key-value storage.

Each tool group owns exactly one ``data.db`` (its own SQLite file), so groups are
fully isolated from each other.

值以 JSON 存储，``database[key]`` 对 list / dict 返回**写穿代理**：任何修改都会立刻
落盘，且支持任意深度 ——

    database["list"] = []
    database["list"].append("x")            # 立即写回
    database["tree"] = {"a": {"b": []}}
    database["tree"]["a"]["b"].append(1)    # 任意深度
    database["tree"]["a"]["c"] = 2          # 深度新增键
    database["n"] += 1                      # 标量直接读写

并发：工具处理函数跑在线程池里，所以并发是真实存在的。所有读、改、写都在同一个
``threading.RLock`` 内完成，而且**每次修改都会在锁内重新读取该 key 的最新值再写回**，
因此像 ``database["list"].append(x)`` 这种「取出来再改」的写法在多线程下也不会丢数据。

复合的「读-改-写」如果跨多个语句，用 ``with database.lock():`` 整体框住，或用原子
封装 ``database.modify()``：

    with database.lock():
        database["n"] = database["n"] + 1

    database.modify("n", lambda v: (v or 0) + 1)     # 同上，更短

注意：把整个容器取出来、在 Python 侧改很久、再让代理落盘，这种「长时间持有」的
写法仍可能覆盖期间其他线程的修改 —— 每次重新 ``database[key]`` 取即可避免。另外
每次修改都会重写整个 key，超大容器（几 MB 以上）频繁追加会比较慢。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


def _unwrap(value):
    """把代理/元组等还原成可 JSON 序列化的普通对象。"""
    if isinstance(value, _Node):
        return _unwrap(value._load())
    if isinstance(value, dict):
        return {str(k): _unwrap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_unwrap(v) for v in value]
    return value


class _Node:
    """``database[key]`` 返回的活动视图；任何修改都会立即写回存储。

    只对 list / dict 生成代理（标量直接返回原值，这样 ``database["n"] + 1``、
    ``database["s"].upper()`` 这类写法照常可用）。

    代理**不缓存值**：每次读或改都在锁内重新读取该 key 的最新内容，
    所以多线程下「取出来再改」不会互相覆盖。
    """

    __slots__ = ("_store", "_key", "_path")

    def __init__(self, store: "PluginStorage", key: str, path: tuple = ()):
        self._store = store
        self._key = key
        self._path = path          # 从该 key 的根到本节点的键路径

    # -- 内部 -------------------------------------------------------------
    def _navigate(self, root):
        cur = root
        for p in self._path:
            cur = cur[p]
        return cur

    def _load(self):
        """在锁内读取最新值并定位到本节点。"""
        return self._store._read_value(self._key, self._path)

    def _mutate(self, fn):
        """锁内：重新读取 → 修改 → 写回。整个读-改-写是原子的。"""
        with self._store._lock:
            root = self._store._read_value(self._key, ())
            result = fn(self._navigate(root))
            self._store.set(self._key, root)
            return result

    # -- 读取 -------------------------------------------------------------
    def __getitem__(self, k):
        v = self._load()[k]
        if isinstance(v, (dict, list)):
            return _Node(self._store, self._key, self._path + (k,))
        return v

    def __contains__(self, k):
        return k in self._load()

    def __len__(self):
        return len(self._load())

    def __iter__(self):
        return iter(self._load())

    def __bool__(self):
        return bool(self._load())

    def __repr__(self):
        return repr(self._load())

    def __eq__(self, other):
        return self._load() == (other._load() if isinstance(other, _Node) else other)

    def __ne__(self, other):
        return not self.__eq__(other)

    # 不定义 __hash__：与 list/dict 一致，可变的代理不可哈希

    def get(self, k, default=None):
        v = self._load().get(k, default)
        if isinstance(v, (dict, list)):
            return _Node(self._store, self._key, self._path + (k,))
        return v

    def keys(self):
        return self._load().keys()

    def values(self):
        return self._load().values()

    def items(self):
        return self._load().items()

    def index(self, *a):
        return self._load().index(*a)

    def count(self, *a):
        return self._load().count(*a)

    # -- 写入（全部在锁内「重读 → 改 → 写回」） ----------------------------
    def __setitem__(self, k, v):
        self._mutate(lambda c: c.__setitem__(k, _unwrap(v)))

    def __delitem__(self, k):
        self._mutate(lambda c: c.__delitem__(k))

    def append(self, v):
        self._mutate(lambda c: c.append(_unwrap(v)))

    def extend(self, it):
        self._mutate(lambda c: c.extend(_unwrap(it)))

    def insert(self, i, v):
        self._mutate(lambda c: c.insert(i, _unwrap(v)))

    def remove(self, v):
        self._mutate(lambda c: c.remove(v))

    def pop(self, *args):
        return self._mutate(lambda c: c.pop(*args))

    def clear(self):
        self._mutate(lambda c: c.clear())

    def sort(self, **kw):
        self._mutate(lambda c: c.sort(**kw))

    def reverse(self):
        self._mutate(lambda c: c.reverse())

    def update(self, *args, **kw):
        self._mutate(lambda c: c.update(*args, **kw))

    def setdefault(self, k, default=None):
        self._mutate(lambda c: c.setdefault(k, _unwrap(default)))
        return self[k]

    def set(self, k, v):
        self.__setitem__(k, v)

    def delete(self, k):
        self.__delitem__(k)

    def __iadd__(self, other):
        def op(c):
            c += _unwrap(other)

        self._mutate(op)
        return self


class PluginStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()      # 可重入：代理内部会嵌套调用
        self._init()

    # -- SQLite ------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS plugin_kv ("
                "  key TEXT PRIMARY KEY,"
                "  value TEXT,"
                "  updated_at REAL"
                ")"
            )
            c.commit()

    @staticmethod
    def _encode(value) -> str:
        try:
            return json.dumps({"v": _unwrap(value)}, ensure_ascii=False)
        except (TypeError, ValueError):
            # 无法 JSON 序列化的对象：退回字符串（与旧行为一致）
            return json.dumps({"v": str(value)}, ensure_ascii=False)

    @staticmethod
    def _decode(raw):
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            return raw                     # 旧格式：直接存的裸字符串
        if isinstance(obj, dict) and set(obj) == {"v"}:
            return obj["v"]
        return raw

    def _read_raw(self, key: str):
        with self._lock, self._conn() as c:
            return c.execute(
                "SELECT value FROM plugin_kv WHERE key=?", (str(key),)
            ).fetchone()

    def _read_value(self, key: str, path: tuple = ()):
        """锁内读取 key 的值并按 path 定位（供代理使用）。"""
        with self._lock:
            row = self._read_raw(key)
            if row is None:
                raise KeyError(key)
            cur = self._decode(row["value"])
            for p in path:
                cur = cur[p]
            return cur

    # -- 锁 / 原子操作 -----------------------------------------------------
    def lock(self):
        """返回可重入锁，供 ``with database.lock():`` 做复合读-改-写。"""
        return self._lock

    def modify(self, key: str, fn, default=None):
        """原子读-改-写：``fn(当前值) -> 新值``，全程持锁。"""
        with self._lock:
            row = self._read_raw(key)
            cur = default if row is None else self._decode(row["value"])
            new = fn(cur)
            self.set(key, new)
            return new

    # -- dict-like API -----------------------------------------------------
    def get(self, key: str, default=None):
        with self._lock:
            row = self._read_raw(key)
            if row is None:
                return default
            return self._view(str(key), self._decode(row["value"]))

    def __getitem__(self, key):
        with self._lock:
            row = self._read_raw(key)
            if row is None:
                raise KeyError(key)
            return self._view(str(key), self._decode(row["value"]))

    def _view(self, key: str, value):
        """容器 → 写穿代理；标量 → 原值。"""
        return _Node(self, key) if isinstance(value, (dict, list)) else value

    def set(self, key: str, value) -> None:
        raw = self._encode(value)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO plugin_kv (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(key), raw, time.time()),
            )
            c.commit()

    def setdefault(self, key: str, default=None):
        """键不存在时写入 ``default``，并返回该键的活动视图（可直接链式修改）。"""
        with self._lock:
            if self._read_raw(key) is None:
                self.set(key, default)
            return self[key]

    def __setitem__(self, key, value) -> None:
        self.set(key, value)

    def delete(self, key: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM plugin_kv WHERE key=?", (str(key),))
            c.commit()

    def __delitem__(self, key) -> None:
        self.delete(key)

    def __contains__(self, key) -> bool:
        with self._lock:
            return self._read_raw(key) is not None

    def keys(self) -> list[str]:
        with self._lock, self._conn() as c:
            return [r["key"] for r in c.execute("SELECT key FROM plugin_kv ORDER BY key").fetchall()]

    def all(self) -> dict:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT key, value FROM plugin_kv").fetchall()
        return {r["key"]: self._decode(r["value"]) for r in rows}
