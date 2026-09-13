"""OpenAI 兼容适配器。

也是覆盖面最广的一种：DeepSeek、通义千问(兼容模式)、Moonshot/Kimi、智谱 GLM、
MiniMax、SiliconFlow、Ollama、vLLM、LM Studio、one-api/new-api、OpenRouter 等
都讲这套协议，无需翻译。
"""
from __future__ import annotations

import httpx

from .base import Adapter, finish_of, parse_sse_data


class OpenAIAdapter(Adapter):
    protocol = "openai"
    label = "OpenAI 兼容"

    def _endpoint(self, base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def chat_request(self, base_url: str, api_key: str, payload: dict):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        return self._endpoint(base_url), headers, dict(payload)

    def normalize_response(self, data: dict, model: str) -> dict:
        if not isinstance(data, dict):
            return data
        for ch in data.get("choices") or []:
            if isinstance(ch, dict) and not ch.get("finish_reason"):
                msg = ch.get("message") or {}
                ch["finish_reason"] = "tool_calls" if msg.get("tool_calls") else "stop"
        return data

    def normalize_events(self, line: str) -> list[dict]:
        obj = parse_sse_data(line)
        if obj is None:
            return []
        if obj == "__DONE__":
            return [{"type": "done"}]
        if not isinstance(obj, dict):
            return []
        out: list[dict] = []
        if obj.get("usage"):
            out.append({"type": "usage", "usage": obj["usage"]})
        if obj.get("error"):
            err = obj["error"]
            out.append({"type": "error", "message": err.get("message") if isinstance(err, dict) else str(err)})
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                out.append({"type": "content", "text": delta["content"]})
            if delta.get("reasoning_content"):
                out.append({"type": "reasoning", "text": delta["reasoning_content"]})
            for tc in delta.get("tool_calls") or []:
                fn = tc.get("function") or {}
                out.append({
                    "type": "tool_call",
                    "index": tc.get("index", 0),
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments") or "",
                })
            if ch.get("finish_reason"):
                out.append({"type": "finish", "reason": finish_of(ch["finish_reason"])})
        return out

    async def list_models(self, base_url: str, api_key: str, timeout: float = 20.0) -> list:
        base = (base_url or "").strip().rstrip("/")
        for suffix in ("/chat/completions", "/models"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(base + "/models", headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
            data = resp.json()
        return [m["id"] for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
