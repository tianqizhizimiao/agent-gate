"""阿里 DashScope 原生适配器（通义千问）。

与 OpenAI 的主要差异：
  * 端点是 ``/api/v1/services/aigc/text-generation/generation``
  * 请求体是 ``{"model","input":{"messages":[...]},"parameters":{...}}``
  * 流式要加请求头 ``X-DashScope-SSE: enable``，并在 parameters 里开 ``incremental_output``
  * 响应是 ``{"output":{"choices":[{"message":{...},"finish_reason":...}]},"usage":{...}}``

> 通义千问其实**更推荐用兼容模式地址** ``https://dashscope.aliyuncs.com/compatible-mode/v1``，
> 那是标准 OpenAI 协议，无需翻译。本适配器用于必须走原生接口的场景。
"""
from __future__ import annotations

import httpx

from .base import (Adapter, finish_of, now_ts, parse_sse_data,
                   tool_calls_to_openai, usage_of)

SSE_HEADER = "X-DashScope-SSE"


class DashScopeAdapter(Adapter):
    protocol = "dashscope"
    label = "阿里 DashScope 原生"

    def _endpoint(self, base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if base.endswith("/generation"):
            return base
        return base + "/services/aigc/text-generation/generation"

    def chat_request(self, base_url: str, api_key: str, payload: dict):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        stream = bool(payload.get("stream"))
        if stream:
            headers[SSE_HEADER] = "enable"

        params: dict = {"result_format": "message"}
        if stream:
            params["incremental_output"] = True
        if payload.get("temperature") is not None:
            params["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            params["top_p"] = payload["top_p"]
        mt = payload.get("max_tokens") or payload.get("max_completion_tokens")
        if mt:
            params["max_tokens"] = int(mt)
        if payload.get("tools"):
            params["tools"] = payload["tools"]           # DashScope 沿用 OpenAI 的工具定义
            tc = payload.get("tool_choice")
            if isinstance(tc, str):
                params["tool_choice"] = tc
            elif isinstance(tc, dict):
                params["tool_choice"] = "auto"
        if payload.get("stop"):
            params["stop"] = payload["stop"]

        body = {
            "model": payload.get("model", ""),
            "input": {"messages": list(payload.get("messages") or [])},
            "parameters": params,
        }
        return self._endpoint(base_url), headers, body

    # ---- 响应归一化 ----------------------------------------------------
    @staticmethod
    def _choice(data: dict) -> dict:
        choices = (data.get("output") or {}).get("choices") or []
        return choices[0] if choices and isinstance(choices[0], dict) else {}

    def normalize_response(self, data: dict, model: str) -> dict:
        if isinstance(data, dict) and data.get("code") and data.get("message"):
            raise RuntimeError(f"DashScope 错误 {data.get('code')}: {data.get('message')}")
        ch = self._choice(data)
        msg = ch.get("message") or {}
        out_msg: dict = {"role": "assistant", "content": msg.get("content") or None}
        if msg.get("reasoning_content"):
            out_msg["reasoning_content"] = msg["reasoning_content"]
        if msg.get("tool_calls"):
            out_msg["tool_calls"] = tool_calls_to_openai(msg["tool_calls"])
        u = data.get("usage") or {}
        return {
            "id": data.get("request_id") or "chatcmpl-agentgate",
            "object": "chat.completion",
            "created": now_ts(),
            "model": model,
            "choices": [{
                "index": 0,
                "message": out_msg,
                "finish_reason": finish_of(ch.get("finish_reason"), bool(msg.get("tool_calls"))),
            }],
            "usage": usage_of(u.get("input_tokens"), u.get("output_tokens")),
        }

    def normalize_events(self, line: str) -> list[dict]:
        obj = parse_sse_data(line)
        if obj is None:
            return []
        if obj == "__DONE__":
            return [{"type": "done"}]
        if not isinstance(obj, dict):
            return []
        if obj.get("code") and obj.get("message"):
            return [{"type": "error", "message": f"DashScope 错误 {obj['code']}: {obj['message']}"}]
        ch = self._choice(obj)
        msg = ch.get("message") or {}
        out: list[dict] = []
        if msg.get("reasoning_content"):
            out.append({"type": "reasoning", "text": msg["reasoning_content"]})
        if msg.get("content"):
            out.append({"type": "content", "text": msg["content"]})
        for tc in tool_calls_to_openai(msg.get("tool_calls") or []):
            out.append({"type": "tool_call", "index": tc["index"], "id": tc["id"],
                        "name": tc["function"]["name"], "arguments": tc["function"]["arguments"]})
        u = obj.get("usage")
        if u:
            out.append({"type": "usage", "usage": usage_of(u.get("input_tokens"), u.get("output_tokens"))})
        if ch.get("finish_reason"):
            out.append({"type": "finish", "reason": finish_of(ch["finish_reason"])})
        return out

    async def list_models(self, base_url: str, api_key: str, timeout: float = 20.0) -> list:
        # DashScope 原生接口没有 /models；模型名需要用户手填
        return []
