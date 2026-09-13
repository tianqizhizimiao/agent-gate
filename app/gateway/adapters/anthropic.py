"""Anthropic Messages 适配器（Claude）。

与 OpenAI 的主要差异：
  * 端点是 ``/v1/messages``，用 ``x-api-key`` + ``anthropic-version`` 认证
  * 系统提示词是顶层 ``system`` 字段，不在 messages 里
  * ``max_tokens`` 必填
  * 工具用 ``input_schema``；工具的调用/结果用 content block 表示
  * 流式事件是 ``content_block_delta`` / ``message_delta`` 这类带类型的 event
"""
from __future__ import annotations

import json

import httpx

from .base import Adapter, finish_of, now_ts, parse_sse_data, split_system, text_of, usage_of

VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


class AnthropicAdapter(Adapter):
    protocol = "anthropic"
    label = "Anthropic Messages"

    # ---- 请求翻译 ------------------------------------------------------
    def _endpoint(self, base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if base.endswith("/messages"):
            return base
        return base + "/messages"

    def _headers(self, api_key: str) -> dict:
        return {
            "x-api-key": api_key or "",
            "anthropic-version": VERSION,
            "Content-Type": "application/json",
        }

    def chat_request(self, base_url: str, api_key: str, payload: dict):
        return self._endpoint(base_url), self._headers(api_key), self._translate(payload)

    def _translate(self, payload: dict) -> dict:
        system, msgs = split_system(payload.get("messages") or [])
        body: dict = {
            "model": payload.get("model", ""),
            "max_tokens": int(payload.get("max_tokens")
                              or payload.get("max_completion_tokens")
                              or DEFAULT_MAX_TOKENS),
            "messages": self._messages(msgs),
        }
        if payload.get("stream"):
            body["stream"] = True
        if system:
            body["system"] = system
        if payload.get("temperature") is not None:
            body["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            body["top_p"] = payload["top_p"]
        if payload.get("stop"):
            s = payload["stop"]
            body["stop_sequences"] = s if isinstance(s, list) else [s]

        tools = payload.get("tools")
        if tools:
            body["tools"] = [{
                "name": (t.get("function") or {}).get("name", ""),
                "description": (t.get("function") or {}).get("description", ""),
                "input_schema": (t.get("function") or {}).get("parameters")
                                or {"type": "object", "properties": {}},
            } for t in tools]
            tc = payload.get("tool_choice")
            if tc == "required":
                body["tool_choice"] = {"type": "any"}
            elif tc == "none":
                pass
            elif isinstance(tc, dict) and (tc.get("function") or {}).get("name"):
                body["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}
            else:
                body["tool_choice"] = {"type": "auto"}
        return body

    def _messages(self, msgs: list[dict]) -> list[dict]:
        out: list[dict] = []
        i = 0
        while i < len(msgs):
            m = msgs[i]
            role = m.get("role")
            if role == "tool":
                # 连续的工具结果合并成一条 user 消息（Anthropic 要求）
                blocks = []
                while i < len(msgs) and msgs[i].get("role") == "tool":
                    t = msgs[i]
                    blocks.append({
                        "type": "tool_result",
                        "tool_use_id": t.get("tool_call_id", ""),
                        "content": text_of(t.get("content")) or "(empty)",
                    })
                    i += 1
                out.append({"role": "user", "content": blocks})
                continue
            if role == "assistant":
                blocks = []
                txt = text_of(m.get("content"))
                if txt:
                    blocks.append({"type": "text", "text": txt})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    raw = fn.get("arguments")
                    try:
                        inp = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except Exception:
                        inp = {}
                    if not isinstance(inp, dict):
                        inp = {"value": inp}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or "toolu_0",
                        "name": fn.get("name", ""),
                        "input": inp,
                    })
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            else:
                out.append({"role": "user", "content": text_of(m.get("content"))})
            i += 1
        return out

    # ---- 响应归一化 ----------------------------------------------------
    def normalize_response(self, data: dict, model: str) -> dict:
        text, tool_calls = [], []
        for b in data.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                tool_calls.append({
                    "id": b.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    },
                })
        msg: dict = {"role": "assistant", "content": "".join(text) or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        u = data.get("usage") or {}
        return {
            "id": data.get("id") or "chatcmpl-agentgate",
            "object": "chat.completion",
            "created": now_ts(),
            "model": model or data.get("model", ""),
            "choices": [{
                "index": 0,
                "message": msg,
                "finish_reason": finish_of(data.get("stop_reason"), bool(tool_calls)),
            }],
            "usage": usage_of(u.get("input_tokens"), u.get("output_tokens")),
        }

    def normalize_events(self, line: str) -> list[dict]:
        obj = parse_sse_data(line)
        if obj is None or not isinstance(obj, dict):
            return []
        t = obj.get("type")
        out: list[dict] = []
        if t == "message_start":
            u = (obj.get("message") or {}).get("usage") or {}
            if u:
                out.append({"type": "usage", "usage": usage_of(u.get("input_tokens"), u.get("output_tokens"))})
        elif t == "content_block_start":
            cb = obj.get("content_block") or {}
            if cb.get("type") == "tool_use":
                out.append({"type": "tool_call", "index": obj.get("index", 0),
                            "id": cb.get("id"), "name": cb.get("name"), "arguments": ""})
        elif t == "content_block_delta":
            d = obj.get("delta") or {}
            if d.get("type") == "text_delta":
                out.append({"type": "content", "text": d.get("text", "")})
            elif d.get("type") == "input_json_delta":
                out.append({"type": "tool_call", "index": obj.get("index", 0),
                            "arguments": d.get("partial_json", "")})
            elif d.get("type") == "thinking_delta":
                out.append({"type": "reasoning", "text": d.get("thinking", "")})
        elif t == "message_delta":
            d = obj.get("delta") or {}
            u = obj.get("usage") or {}
            if u:
                out.append({"type": "usage", "usage": usage_of(u.get("input_tokens"), u.get("output_tokens"))})
            if d.get("stop_reason"):
                out.append({"type": "finish", "reason": finish_of(d["stop_reason"])})
        elif t == "message_stop":
            out.append({"type": "done"})
        elif t == "error":
            err = obj.get("error") or {}
            out.append({"type": "error", "message": err.get("message") or "Anthropic 返回错误"})
        return out

    async def list_models(self, base_url: str, api_key: str, timeout: float = 20.0) -> list:
        base = (base_url or "").strip().rstrip("/")
        if base.endswith("/messages"):
            base = base[: -len("/messages")]
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(base + "/models", headers=self._headers(api_key))
            resp.raise_for_status()
            data = resp.json()
        return [m["id"] for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
