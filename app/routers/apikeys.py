"""API key management: create/list/delete + bind to a tool group."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException

from .. import database, models, security
from ..auth import get_current_user
from ..gateway import provider

router = APIRouter()


def _safe(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _parse_models(raw) -> list:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def _dump_models(items) -> str:
    seen, out = set(), []
    for m in items or []:
        m = str(m).strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return json.dumps(out, ensure_ascii=False)


def _row_to_out(r) -> models.ApiKeyOut:
    return models.ApiKeyOut(
        id=r["id"],
        key_prefix=r["key_prefix"],
        key=_safe(r, "key_plain", "") or "",
        label=r["label"],
        upstream_name=r["upstream_name"],
        upstream_base_url=r["upstream_base_url"],
        upstream_model=r["upstream_model"],
        upstream_models=_parse_models(_safe(r, "upstream_models")),
        tool_group_id=r["tool_group_id"],
        created_at=r["created_at"],
    )


@router.get("/api/apikeys", response_model=list[models.ApiKeyOut])
def list_keys(user: dict = Depends(get_current_user)):
    rows = database.query(
        "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
    )
    return [_row_to_out(r) for r in rows]


@router.post("/api/apikeys", response_model=models.ApiKeyCreated)
def create_key(req: models.ApiKeyCreate, user: dict = Depends(get_current_user)):
    err = security.validate_custom(req.custom_key)
    if err:
        raise HTTPException(400, err)
    try:
        full, key_hash, key_prefix = security.generate_unique_key(req.custom_key)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    kid = uuid.uuid4().hex
    now = database.now()
    models_list = _parse_models(req.upstream_models)
    if not models_list and req.upstream_model:
        models_list = [req.upstream_model]   # 至少把默认模型放进可选用列表
    database.execute(
        "INSERT INTO api_keys (id, key_hash, key_plain, key_prefix, user_id, label, upstream_name, "
        "upstream_base_url, upstream_api_key, upstream_model, upstream_models, tool_group_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            kid, key_hash, full, key_prefix, user["id"], req.label, req.upstream_name,
            req.upstream_base_url, req.upstream_api_key, req.upstream_model,
            _dump_models(models_list), None, now,
        ),
    )
    return models.ApiKeyCreated(
        id=kid,
        key_prefix=key_prefix,
        key=full,
        label=req.label,
        upstream_name=req.upstream_name,
        upstream_base_url=req.upstream_base_url,
        upstream_model=req.upstream_model,
        upstream_models=models_list,
        tool_group_id=None,
        created_at=now,
        full_key=full,
    )


@router.delete("/api/apikeys/{kid}")
def delete_key(kid: str, user: dict = Depends(get_current_user)):
    row = database.query_one(
        "SELECT id FROM api_keys WHERE id = ? AND user_id = ?", (kid, user["id"])
    )
    if row is None:
        raise HTTPException(404, "API key not found")
    database.execute("DELETE FROM api_keys WHERE id = ?", (kid,))
    return {"ok": True}


@router.put("/api/apikeys/{kid}/toolgroup")
def bind_toolgroup(kid: str, req: models.ApiKeyBind, user: dict = Depends(get_current_user)):
    row = database.query_one(
        "SELECT id FROM api_keys WHERE id = ? AND user_id = ?", (kid, user["id"])
    )
    if row is None:
        raise HTTPException(404, "API key not found")
    tg = req.tool_group_id
    if tg:
        member = database.query_one(
            "SELECT 1 FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?",
            (tg, user["id"]),
        )
        if member is None:
            raise HTTPException(403, "You are not a member of this tool group")
    database.execute(
        "UPDATE api_keys SET tool_group_id = ? WHERE id = ?", (tg, kid)
    )
    return {"ok": True}


@router.put("/api/apikeys/{kid}")
def update_key(kid: str, req: models.ApiKeyUpdate, user: dict = Depends(get_current_user)):
    """部分更新密钥配置（备注 / 上游 / 默认模型 / 可用模型列表）。"""
    row = database.query_one(
        "SELECT * FROM api_keys WHERE id = ? AND user_id = ?", (kid, user["id"])
    )
    if row is None:
        raise HTTPException(404, "API key not found")
    allowed = (
        "label", "upstream_name", "upstream_base_url",
        "upstream_api_key", "upstream_model", "upstream_models",
    )
    fields = {
        k: v
        for k, v in req.model_dump(exclude_unset=True, exclude_none=True).items()
        if k in allowed
    }
    if "upstream_models" in fields:
        fields["upstream_models"] = _dump_models(fields["upstream_models"])
    # 只改默认模型时，把它并入可用模型列表
    if fields.get("upstream_model") and "upstream_models" not in fields:
        current = _parse_models(_safe(row, "upstream_models"))
        if fields["upstream_model"] not in current:
            current.append(fields["upstream_model"])
            fields["upstream_models"] = _dump_models(current)
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        database.execute(f"UPDATE api_keys SET {sets} WHERE id = ?", (*fields.values(), kid))
    return {"ok": True}


@router.get("/api/apikeys/{kid}/models")
async def key_models(kid: str, user: dict = Depends(get_current_user)):
    """拉取该密钥上游的模型列表，并返回已选中的模型（供仪表盘勾选）。"""
    row = database.query_one(
        "SELECT * FROM api_keys WHERE id = ? AND user_id = ?", (kid, user["id"])
    )
    if row is None:
        raise HTTPException(404, "API key not found")

    configured = row["upstream_model"]
    selected = _parse_models(_safe(row, "upstream_models"))
    if configured and configured not in selected:
        selected = [configured] + selected

    err = None
    ids: list = []
    try:
        fetched = await provider.list_models(row["upstream_base_url"], row["upstream_api_key"])
        ids = [m["id"] for m in fetched]
    except Exception as e:
        err = provider.friendly_error(e)

    # 已选中的模型始终出现在列表里（即使上游未返回），以便正确回显勾选状态
    for m in selected:
        if m not in ids:
            ids.insert(0, m)
    if not ids and configured:
        ids = [configured]

    return {"models": ids, "selected": selected, "configured": configured, "error": err}
