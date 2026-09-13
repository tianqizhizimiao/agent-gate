"""账户级上游 API 管理：增删改查 + 拉取模型列表。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import models
from .. import upstreams as up
from ..auth import get_current_user
from ..gateway import provider

router = APIRouter()


@router.get("/api/upstreams")
def list_upstreams(user: dict = Depends(get_current_user)):
    return up.list_upstreams(user["id"])


@router.post("/api/upstreams")
def create_upstream(req: models.UpstreamCreate, user: dict = Depends(get_current_user)):
    base = provider.normalize_base_url(req.base_url)
    if not base:
        raise HTTPException(400, "请填写上游地址")
    uid = up.add(user["id"], req.name, base, req.api_key, req.models,
                 req.default_model, req.protocol)
    return up.to_out(up.get(user["id"], uid))


@router.put("/api/upstreams/{uid}")
def update_upstream(uid: str, req: models.UpstreamUpdate, user: dict = Depends(get_current_user)):
    if up.get(user["id"], uid) is None:
        raise HTTPException(404, "上游不存在")
    payload = req.model_dump(exclude_unset=True)
    if payload.get("api_key") == "":          # 留空 = 不修改密钥
        payload.pop("api_key", None)
    if payload.get("base_url"):
        payload["base_url"] = provider.normalize_base_url(payload["base_url"])
    up.update(user["id"], uid, payload)
    return up.to_out(up.get(user["id"], uid))


@router.delete("/api/upstreams/{uid}")
def delete_upstream(uid: str, user: dict = Depends(get_current_user)):
    if up.get(user["id"], uid) is None:
        raise HTTPException(404, "上游不存在")
    up.delete(user["id"], uid)
    return {"ok": True}


@router.post("/api/upstreams/probe-models")
async def probe_models(req: models.ProbeModelsRequest, user: dict = Depends(get_current_user)):
    """**按用户填的地址实际发请求**探测协议，并取回模型列表（保存前用）。"""
    if not req.base_url.strip():
        return {"protocol": None, "protocol_label": "", "models": [],
                "evidence": [], "error": "请先填写上游地址"}
    result = await provider.probe_protocol(req.base_url, req.api_key)
    result["protocol_label"] = provider.PROTOCOL_LABELS.get(result.get("protocol") or "", "未识别")
    return result


@router.get("/api/upstreams/{uid}/models")
async def upstream_models(uid: str, user: dict = Depends(get_current_user)):
    """重新探测某个已保存上游的协议，取回模型列表并回显已勾选项。"""
    row = up.get(user["id"], uid)
    if row is None:
        raise HTTPException(404, "上游不存在")
    selected = up.parse_models(row["models"])
    result = await provider.probe_protocol(row["base_url"], row["api_key"])
    ids = list(result.get("models") or [])
    for m in selected:                        # 已勾选的始终回显
        if m not in ids:
            ids.insert(0, m)
    return {
        "protocol": result.get("protocol"),
        "protocol_label": provider.PROTOCOL_LABELS.get(result.get("protocol") or "", "未识别"),
        "models": ids,
        "selected": selected,
        "default_model": row["default_model"],
        "evidence": result.get("evidence"),
        "error": result.get("error"),
    }
