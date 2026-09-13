"""OpenAI-compatible proxy endpoints.

``POST /v1/chat/completions`` authenticates with an AgentGate API key
(``Authorization: Bearer ag-...``), resolves the key's upstream provider and
bound tool group, then runs the ContextusAgent-style inject → upstream →
intercept → execute → loop pipeline (see ``app.gateway.chat``).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import security
from .. import upstreams
from ..gateway import provider
from ..gateway.chat import chat_service

router = APIRouter()


def _saved_models(key_row: dict) -> list:
    """解密密钥里保存的可用模型列表（JSON 数组）。"""
    raw = key_row.get("upstream_models")
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def _resolve_key(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing API key — use Authorization: Bearer ag-...")
    key = authorization.split(" ", 1)[1].strip()
    row = security.lookup_api_key(key)
    if row is None:
        raise HTTPException(401, "Invalid or disabled API key")
    return row


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(None)):
    key_row = _resolve_key(authorization)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")

    mode, value, status = await chat_service.run(body, key_row)
    if mode == "json":
        return JSONResponse(value, status_code=status)
    return StreamingResponse(value, media_type="text/event-stream", status_code=status)


@router.get("/v1/models")
async def list_models(authorization: str | None = Header(None)):
    """下游客户端拉取可用模型列表。

    返回该账户**所有上游已添加模型的并集**；若一个都没勾选，则尝试逐个向上游
    拉取；再退回密钥自带的旧字段。
    """
    key_row = _resolve_key(authorization)
    user_id = key_row["user_id"]

    models = upstreams.all_models(user_id)

    if not models:
        fetched: list = []
        for u in upstreams.raw_rows(user_id):
            try:
                fetched += await provider.list_models(
                    u["base_url"], u["api_key"], u.get("protocol") or "openai"
                )
            except Exception:
                pass
        models = list(dict.fromkeys(fetched))

    if not models:
        models = _saved_models(key_row)
    if not models and key_row["upstream_model"]:
        models = [key_row["upstream_model"]]

    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "agentgate"} for m in models],
    }
