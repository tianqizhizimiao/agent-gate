"""上游调用的公共部分：地址归一化、错误翻译、**协议探测**。

真正的协议差异（请求翻译 / 响应归一化 / 流式事件）都在 ``adapters/`` 里；
本模块只负责「怎么知道上游说的是哪种协议」以及若干工具函数。
"""
from __future__ import annotations

import re

import httpx

from .adapters import ADAPTERS, get_adapter

UPSTREAM_TIMEOUT = 300.0
MODELS_TIMEOUT = 30.0
PROBE_TIMEOUT = 15.0

PROTOCOL_LABELS: dict[str, str] = {p: a.label for p, a in ADAPTERS.items()}
PROTOCOL_LABELS["unknown"] = "未知协议"

# 每种协议要用哪种认证方式（探测第二阶段用）
_AUTH_BY_PROTOCOL = {
    "openai": "bearer",
    "anthropic": "anthropic",
    "gemini": "query",
    "dashscope": "bearer",
}


# --------------------------------------------------------------------------- #
# 地址
# --------------------------------------------------------------------------- #
def _base(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def normalize_base_url(url: str) -> str:
    """去掉尾部斜杠和用户可能多粘的端点后缀，得到纯基址。"""
    u = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models", "/messages"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u.rstrip("/")


def _candidate_bases(base_url: str) -> list[str]:
    """用户可能填了 /v1 也可能没填，两种都试一下。"""
    base = _base(base_url)
    out = [base]
    if base and not re.search(r"/v\d+[a-z]*$", base):   # /v1、/v1beta、/v4 都算已带版本
        out.append(base + "/v1")
    return out


# --------------------------------------------------------------------------- #
# 协议判别
# --------------------------------------------------------------------------- #
def _ids_from(payload, protocol: str) -> list:
    if not isinstance(payload, dict):
        return []
    if protocol in ("openai", "anthropic"):
        return [d["id"] for d in (payload.get("data") or [])
                if isinstance(d, dict) and d.get("id")]
    if protocol == "gemini":
        return [str(m.get("name", "")).split("/")[-1] for m in (payload.get("models") or [])
                if isinstance(m, dict)]
    return []


def classify_protocol(payload) -> str | None:
    """按响应体结构判断协议——成功响应和报错响应都能认出来。"""
    if not isinstance(payload, dict):
        return None
    # Anthropic 的错误固定是 {"type":"error","error":{...}}
    if payload.get("type") == "error":
        return "anthropic"
    data = payload.get("data")
    # OpenAI:  {"object":"list","data":[{"id":..,"object":"model"}]}
    if payload.get("object") == "list" and isinstance(data, list):
        return "openai"
    # Anthropic: {"data":[{"id":..,"type":"model"}]}
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("type") == "model":
        return "anthropic"
    # Gemini: {"models":[{"name":"models/gemini-.."}]}
    if isinstance(payload.get("models"), list):
        return "gemini"
    # DashScope 原生: {"output":{"choices":[..]}} 或 {"code":"..","message":".."}
    if isinstance(payload.get("output"), dict):
        return "dashscope"
    if isinstance(payload.get("code"), str) and payload.get("message") is not None:
        return "dashscope"
    err = payload.get("error")
    if isinstance(err, dict):
        if "status" in err and "code" in err:      # Google 风格错误体
            return "gemini"
        if err.get("type"):                        # {"error":{"message":..,"type":..}}
            return "openai"
        # 泛化的 {"error":{"message":..}}：形状不够独特，只算弱证据（见 probe_protocol）
        return "openai?"
    return None


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def _auth(style: str, api_key: str):
    headers = {"Accept": "application/json"}
    params: dict = {}
    if style == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif style == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif style == "query":
        params["key"] = api_key
    return headers, params


async def _get(url: str, style: str, api_key: str):
    headers, params = _auth(style, api_key)
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
        resp = await client.get(url, headers=headers, params=params)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return resp.status_code, payload, (resp.text or "")[:120]


async def _probe_dashscope_native(base: str, api_key: str):
    """DashScope 原生没有 /models，用一次空体的生成请求看它的错误体。"""
    url = base.rstrip("/") + "/services/aigc/text-generation/generation"
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={},
            )
    except Exception:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    if classify_protocol(payload) == "dashscope":
        return {"url": url, "auth": "bearer", "status": resp.status_code,
                "protocol": "dashscope", "note": None}
    return None


async def probe_protocol(base_url: str, api_key: str) -> dict:
    """**通过实际发请求**判断上游说的是哪种协议，并顺便取回模型列表。

    两步：
      1) 对每个候选基址 × 三种认证方式请求 ``/models``，按**响应体结构**
         （成功响应或报错响应都算）识别协议；认不出再试 DashScope 原生端点；
      2) 再用**该协议对应的认证方式**重新请求一次，取回模型列表并确认密钥。
    """
    evidence: list[dict] = []
    last_error: str | None = None
    found: tuple[str, str] | None = None
    weak: tuple[str, str] | None = None      # 弱证据候选（泛化错误体，非 404）

    for base in _candidate_bases(base_url):
        if not base:
            continue
        for style in ("bearer", "anthropic", "query"):
            url = base.rstrip("/") + "/models"
            try:
                status, payload, text = await _get(url, style, api_key)
            except Exception as e:
                last_error = friendly_error(e)
                evidence.append({"url": url, "auth": style, "status": None,
                                 "protocol": None, "note": last_error})
                continue
            proto = classify_protocol(payload)
            if proto == "openai?":
                # 泛化错误体：只有非 404（说明端点存在、只是认证/参数问题）才记作弱证据
                if weak is None and status != 404:
                    weak = (base, "openai")
                proto = None
            evidence.append({"url": url, "auth": style, "status": status,
                             "protocol": proto, "note": None if proto else text})
            if proto:
                found = (base, proto)
                break
        if found:
            break

        # /models 认不出来时，试一下 DashScope 原生端点
        ev = await _probe_dashscope_native(base, api_key)
        if ev:
            evidence.append(ev)
            found = (base, "dashscope")
            break

    weak_fallback = False
    if found is None and weak is not None:
        found, weak_fallback = weak, True

    if found is None:
        if not evidence or all(e["status"] is None for e in evidence):
            return {"protocol": None, "models": [], "evidence": evidence,
                    "error": last_error or "无法连接该地址"}
        return {"protocol": None, "models": [], "evidence": evidence,
                "error": "无法识别协议：地址可能不是 API 根路径，或该服务商不兼容已支持的协议"}

    base, proto = found
    label = PROTOCOL_LABELS.get(proto, proto)
    # 第二阶段：换成该协议匹配的认证方式复探
    style = _AUTH_BY_PROTOCOL.get(proto, "bearer")
    url = base.rstrip("/") + "/models"
    try:
        status, payload, text = await _get(url, style, api_key)
    except Exception as e:
        if proto == "dashscope":
            return {"protocol": proto, "models": [], "evidence": evidence, "error": None}
        return {"protocol": proto, "models": [], "evidence": evidence,
                "error": f"已识别协议为 {label}，但请求失败：{friendly_error(e)}"}

    if status < 400:
        return {"protocol": proto, "models": _ids_from(payload, proto),
                "evidence": evidence, "error": None}
    if proto == "dashscope":
        # 原生接口没有 /models，第二阶段 404 属正常
        return {"protocol": proto, "models": [], "evidence": evidence, "error": None}

    hint = "密钥无效或未填写" if status in (401, 403) else f"上游返回 {status}"
    prefix = "未能确证协议，暂按 OpenAI 兼容处理；" if weak_fallback else f"已识别协议为 {label}，但"
    return {"protocol": proto, "models": [], "evidence": evidence,
            "error": f"{prefix}{hint}"}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
async def list_models(base_url: str, api_key: str, protocol: str = "openai") -> list:
    return await get_adapter(protocol).list_models(base_url, api_key, MODELS_TIMEOUT)


def friendly_error(e: Exception) -> str:
    """Turn an upstream exception into a short Chinese hint."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return "上游拒绝了请求（401）：上游密钥无效或未填写"
        if code == 403:
            return "上游拒绝了请求（403）：无权限"
        if code == 404:
            return "上游没有该接口（404）：可能协议选错了，或上游地址填错"
        if code == 429:
            return "上游限流（429）：请稍后重试"
        body = (e.response.text or "")[:200]
        return f"上游返回 {code}：{body}"
    if isinstance(e, httpx.ConnectError):
        return "无法连接上游：请检查上游地址与网络"
    if isinstance(e, httpx.TimeoutException):
        return "连接上游超时"
    text = str(e)
    if "Illegal header value" in text:
        return "上游密钥为空或含非法字符，请在「上游 API」中补全密钥"
    if "InvalidURL" in text or "invalid URL" in text:
        return "上游地址不合法，请检查是否包含 http(s):// 前缀"
    return text
