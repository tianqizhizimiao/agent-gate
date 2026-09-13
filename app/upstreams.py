"""账户级上游 API（渠道）。

一个用户可以配置**多个**上游 API，每个上游有自己的地址 / 密钥 / 模型列表。
下游令牌通过 ``GET /v1/models`` 看到的是该账户**所有上游模型的并集**；
调用时再按请求里的 ``model`` 自动路由到声明了该模型的上游。
"""
from __future__ import annotations

import json
import uuid

from . import database
from .gateway.provider import PROTOCOL_LABELS


def _get(row, key, default=""):
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def parse_models(raw) -> list:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def dump_models(items) -> str:
    seen, out = set(), []
    for m in items or []:
        m = str(m).strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return json.dumps(out, ensure_ascii=False)


def mask(api_key: str) -> str:
    api_key = (api_key or "").strip()
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "••••"
    return api_key[:3] + "••••" + api_key[-4:]


def to_out(row) -> dict:
    proto = _get(row, "protocol")
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "api_key_hint": mask(row["api_key"]),
        "protocol": proto,
        "protocol_label": PROTOCOL_LABELS.get(proto, "未探测") if proto else "未探测",
        "models": parse_models(row["models"]),
        "default_model": row["default_model"],
        "created_at": row["created_at"],
    }


def raw_rows(user_id: str) -> list:
    return database.query(
        "SELECT * FROM upstreams WHERE user_id = ? ORDER BY created_at", (user_id,)
    )


def list_upstreams(user_id: str) -> list:
    return [to_out(r) for r in raw_rows(user_id)]


def get(user_id: str, uid: str):
    return database.query_one(
        "SELECT * FROM upstreams WHERE id = ? AND user_id = ?", (uid, user_id)
    )


def add(user_id: str, name: str, base_url: str, api_key: str, models,
        default_model: str = "", protocol: str = "") -> str:
    uid = uuid.uuid4().hex
    database.execute(
        "INSERT INTO upstreams (id, user_id, name, base_url, api_key, protocol, models, default_model, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            uid, user_id, name or "", base_url or "", api_key or "", protocol or "",
            dump_models(models), default_model or "", database.now(),
        ),
    )
    return uid


def update(user_id: str, uid: str, fields: dict) -> bool:
    if get(user_id, uid) is None:
        return False
    allowed = ("name", "base_url", "api_key", "protocol", "models", "default_model")
    data: dict = {}
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        data[k] = dump_models(v) if k == "models" else v
    if data:
        sets = ", ".join(f"{k} = ?" for k in data)
        database.execute(f"UPDATE upstreams SET {sets} WHERE id = ?", (*data.values(), uid))
    return True


def delete(user_id: str, uid: str) -> None:
    database.execute("DELETE FROM upstreams WHERE id = ? AND user_id = ?", (uid, user_id))


def all_models(user_id: str) -> list:
    """所有上游已添加模型的并集（去重，保持添加顺序）。"""
    seen, out = set(), []
    for r in raw_rows(user_id):
        for m in parse_models(r["models"]):
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


def resolve(user_id: str, requested_model: str, legacy: dict | None = None):
    """按请求的 ``model`` 选择上游，返回 ``(base_url, api_key, model, protocol)``；无可用上游返回 None。"""
    ups = [dict(r) for r in raw_rows(user_id)]
    if not ups:
        # 兼容：该账户还没配置上游时，退回密钥自带的旧字段
        if legacy and (legacy.get("upstream_base_url") or "").strip():
            return (
                legacy["upstream_base_url"],
                legacy["upstream_api_key"],
                requested_model or legacy.get("upstream_model", ""),
                "openai",          # 旧字段没有协议信息，按 OpenAI 兼容处理
            )
        return None

    if requested_model:
        for u in ups:                                    # 精确命中某上游的模型列表
            if requested_model in parse_models(u["models"]):
                return (u["base_url"], u["api_key"], requested_model, u.get("protocol") or "openai")
        for u in ups:                                    # 命中某上游的默认模型
            if requested_model == (u["default_model"] or ""):
                return (u["base_url"], u["api_key"], requested_model, u.get("protocol") or "openai")

    u = ups[0]                                           # 未命中：用第一个上游
    models = parse_models(u["models"])
    model = requested_model or u["default_model"] or (models[0] if models else "")
    return (u["base_url"], u["api_key"], model, u.get("protocol") or "openai")
