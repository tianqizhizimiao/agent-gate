"""协议适配器公共部分。

网关对外只讲 **OpenAI 格式**；每个适配器负责：

* 把 OpenAI 格式的请求**翻译**成自家格式（``chat_request``）
* 建立连接并拿到状态码，便于"预检"（``open_stream``）
* 把自家响应 / 流**归一化**成统一的内部事件（``normalize_response`` / ``normalize_events``）

统一事件（``normalize_events`` 的产物）：
    {"type": "content",   "text": "..."}
    {"type": "tool_call", "index": 0, "id": ..., "name": ..., "arguments": "..."}
    {"type": "finish",    "reason": "stop"|"tool_calls"|"length"|..., "usage": {...}}
"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx

DEFAULT_TIMEOUT = 300.0


def now_ts() -> int:
    return int(time.time())


def openai_chunk(model: str, delta: dict, finish=None, usage=None,
                 chunk_id: str = "chatcmpl-agentgate", created: int | None = None) -> dict:
    obj = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created or now_ts(),
        "model": model or "",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        obj["usage"] = usage
    return obj


def usage_of(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": int(prompt or 0) + int(completion or 0),
    }


def finish_of(reason: str | None, has_tools: bool = False) -> str:
    """把各家的停止原因映射成 OpenAI 的 finish_reason。"""
    r = (reason or "").lower()
    if r in ("tool_use", "tool_calls", "function_call"):
        return "tool_calls"
    if r in ("max_tokens", "length", "max_output_tokens"):
        return "length"
    if r in ("content_filter", "safety", "recitation", "blocked", "prohibited_content"):
        return "content_filter"
    if r in ("stop", "end_turn", "stop_sequence", "eos", "finished", ""):
        return "tool_calls" if has_tools else "stop"
    return "tool_calls" if has_tools else "stop"


# --------------------------------------------------------------------------- #
# 消息 / 工具的格式转换（OpenAI -> 各家）
# --------------------------------------------------------------------------- #
def split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """把 system 消息抽出来（Anthropic / Gemini 用单独的 system 字段）。"""
    system_parts, rest = [], []
    for m in messages or []:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, list):
                c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
            system_parts.append(str(c or ""))
        else:
            rest.append(m)
    return "\n\n".join(p for p in system_parts if p), rest


def text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        )
    return str(content)


# --------------------------------------------------------------------------- #
# 适配器基类
# --------------------------------------------------------------------------- #
class StreamSession:
    """一次已建立的上游流式会话（状态码可先取，再做预检）。"""

    def __init__(self, client: httpx.AsyncClient, response: httpx.Response, adapter: "Adapter"):
        self.client = client
        self.response = response
        self.adapter = adapter

    @property
    def status_code(self) -> int:
        return self.response.status_code

    async def error_text(self) -> str:
        try:
            data = await self.response.aread()
        except Exception:
            return ""
        return data.decode("utf-8", "replace")

    async def events(self) -> AsyncIterator[dict]:
        async for line in self.response.aiter_lines():
            for ev in self.adapter.normalize_events(line):
                yield ev

    async def close(self) -> None:
        try:
            await self.response.aclose()
        finally:
            await self.client.aclose()


class Adapter:
    protocol = "openai"
    label = "OpenAI 兼容"

    # ---- 需要子类实现 -------------------------------------------------
    def chat_request(self, base_url: str, api_key: str, payload: dict) -> tuple[str, dict, dict]:
        """返回 ``(url, headers, body)``。``payload`` 是 OpenAI 格式的请求体。"""
        raise NotImplementedError

    def stream_request(self, base_url: str, api_key: str, payload: dict) -> tuple[str, dict, dict]:
        return self.chat_request(base_url, api_key, {**payload, "stream": True})

    def normalize_response(self, data: dict, model: str) -> dict:
        """把自家非流式响应翻译成 OpenAI ``chat.completion``。"""
        raise NotImplementedError

    def normalize_events(self, line: str) -> list[dict]:
        """把自家 SSE 的一行翻译成统一事件列表。"""
        raise NotImplementedError

    async def list_models(self, base_url: str, api_key: str, timeout: float = 20.0) -> list:
        raise NotImplementedError

    # ---- 通用实现 -----------------------------------------------------
    async def chat(self, base_url: str, api_key: str, payload: dict,
                   timeout: float = DEFAULT_TIMEOUT) -> dict:
        url, headers, body = self.chat_request(base_url, api_key, payload)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        return self.normalize_response(data, payload.get("model", ""))

    async def open_stream(self, base_url: str, api_key: str, payload: dict,
                          timeout: float = DEFAULT_TIMEOUT) -> StreamSession:
        url, headers, body = self.stream_request(base_url, api_key, payload)
        client = httpx.AsyncClient(timeout=timeout)
        try:
            req = client.build_request("POST", url, headers=headers, json=body)
            resp = await client.send(req, stream=True)
        except Exception:
            await client.aclose()
            raise
        return StreamSession(client, resp, self)


# --------------------------------------------------------------------------- #
# SSE 行解析小工具
# --------------------------------------------------------------------------- #
def parse_sse_data(line: str):
    """从 ``data: {...}`` 行里取出 JSON；不是数据行返回 None，``[DONE]`` 返回特殊标记。"""
    s = (line or "").strip()
    if not s:
        return None
    if s.startswith("event:"):
        return None
    if s.startswith("data:"):
        s = s[5:].strip()
    if s == "[DONE]":
        return "__DONE__"
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def tool_calls_to_openai(tool_calls: list[dict]) -> list[dict]:
    """统一成 OpenAI 的 tool_calls 结构。"""
    out = []
    for i, tc in enumerate(tool_calls or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False)
        out.append({
            "index": i,
            "id": tc.get("id") or f"call_{i}",
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": args},
        })
    return out
