"""Google Gemini 适配器（generativelanguage）。

与 OpenAI 的主要差异：
  * 端点是 ``/{version}/models/{model}:generateContent``（流式加 ``?alt=sse``）
  * 认证用 ``x-goog-api-key`` 头
  * 消息是 ``contents[].parts[]``，角色只有 ``user`` / ``model``
  * 系统提示词放 ``systemInstruction``
  * 工具调用是 ``parts[].functionCall``，结果回填 ``parts[].functionResponse``
"""
from __future__ import annotations

import json

import httpx

from .base import Adapter, finish_of, now_ts, parse_sse_data, split_system, text_of, usage_of


def _strip(models_prefix: str) -> str:
    m = (models_prefix or "").strip()
    if m.startswith("models/"):
        m = m[len("models/"):]
    return m


class GeminiAdapter(Adapter):
    protocol = "gemini"
    label = "Google Gemini"

    # ---- 请求翻译 ------------------------------------------------------
    def _endpoint(self, base_url: str, model: str, stream: bool) -> str:
        base = (base_url or "").strip().rstrip("/")
        verb = "streamGenerateContent" if stream else "generateContent"
        url = f"{base}/models/{_strip(model)}:{verb}"
        if stream:
            url += "?alt=sse"
        return url

    def chat_request(self, base_url: str, api_key: str, payload: dict):
        stream = bool(payload.get("stream"))
        url = self._endpoint(base_url, payload.get("model", ""), stream)
        headers = {"x-goog-api-key": api_key or "", "Content-Type": "application/json"}
        return url, headers, self._translate(payload)

    def _translate(self, payload: dict) -> dict:
        system, msgs = split_system(payload.get("messages") or [])
        contents: list[dict] = []
        for m in msgs:
            role = m.get("role")
            if role == "tool":
                contents.append({"role": "function", "parts": [{
                    "functionResponse": {
                        "name": m.get("name") or "tool",
                        "response": {"result": text_of(m.get("content"))},
                    }
                }]})
            elif role == "assistant":
                parts: list[dict] = []
                txt = text_of(m.get("content"))
                if txt:
                    parts.append({"text": txt})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if not isinstance(args, dict):
                        args = {"value": args}
                    parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                contents.append({"role": "user", "parts": [{"text": text_of(m.get("content"))}]})

        body: dict = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        gen: dict = {}
        if payload.get("temperature") is not None:
            gen["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            gen["topP"] = payload["top_p"]
        mt = payload.get("max_tokens") or payload.get("max_completion_tokens")
        if mt:
            gen["maxOutputTokens"] = int(mt)
        if gen:
            body["generationConfig"] = gen

        tools = payload.get("tools")
        if tools:
            body["tools"] = [{"functionDeclarations": [{
                "name": (t.get("function") or {}).get("name", ""),
                "description": (t.get("function") or {}).get("description", ""),
                "parameters": (t.get("function") or {}).get("parameters")
                              or {"type": "object", "properties": {}},
            } for t in tools]}]
            tc = payload.get("tool_choice")
            mode = "AUTO"
            if tc == "required":
                mode = "ANY"
            elif tc == "none":
                mode = "NONE"
            body["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
        return body

    # ---- 响应归一化 ----------------------------------------------------
    def _candidate(self, data: dict) -> dict:
        cands = data.get("candidates") or []
        return cands[0] if cands and isinstance(cands[0], dict) else {}

    def normalize_response(self, data: dict, model: str) -> dict:
        cand = self._candidate(data)
        text, tool_calls = [], []
        for p in ((cand.get("content") or {}).get("parts")) or []:
            if not isinstance(p, dict):
                continue
            if p.get("text"):
                text.append(p["text"])
            fc = p.get("functionCall")
            if fc:
                tool_calls.append({
                    "id": f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                    },
                })
        msg: dict = {"role": "assistant", "content": "".join(text) or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        um = data.get("usageMetadata") or {}
        return {
            "id": "chatcmpl-agentgate",
            "object": "chat.completion",
            "created": now_ts(),
            "model": model,
            "choices": [{
                "index": 0,
                "message": msg,
                "finish_reason": finish_of(cand.get("finishReason"), bool(tool_calls)),
            }],
            "usage": usage_of(um.get("promptTokenCount"), um.get("candidatesTokenCount")),
        }

    def normalize_events(self, line: str) -> list[dict]:
        obj = parse_sse_data(line)
        if obj is None or not isinstance(obj, dict):
            return []
        cand = self._candidate(obj)
        out: list[dict] = []
        if obj.get("promptFeedback", {}).get("blockReason"):
            return [{"type": "error",
                     "message": f"Gemini 拦截了该请求：{obj['promptFeedback']['blockReason']}"}]
        for i, p in enumerate(((cand.get("content") or {}).get("parts")) or []):
            if not isinstance(p, dict):
                continue
            if p.get("text"):
                out.append({"type": "content", "text": p["text"]})
            fc = p.get("functionCall")
            if fc:
                out.append({
                    "type": "tool_call", "index": i, "id": None,
                    "name": fc.get("name"),
                    "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                })
        um = obj.get("usageMetadata")
        if um:
            out.append({"type": "usage",
                        "usage": usage_of(um.get("promptTokenCount"), um.get("candidatesTokenCount"))})
        if cand.get("finishReason"):
            out.append({"type": "finish", "reason": finish_of(cand["finishReason"])})
        return out

    async def list_models(self, base_url: str, api_key: str, timeout: float = 20.0) -> list:
        base = (base_url or "").strip().rstrip("/")
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(base + "/models", headers={"x-goog-api-key": api_key or ""})
            resp.raise_for_status()
            data = resp.json()
        return [_strip(m.get("name", "")) for m in (data.get("models") or []) if isinstance(m, dict)]
