"""协议适配器注册表。

网关对外只讲 OpenAI 格式；上游支持以下协议，由 ``upstreams.protocol`` 决定用哪个：

    openai     OpenAI 兼容（含 DeepSeek / 通义千问兼容模式 / Moonshot / GLM / Ollama / vLLM ...）
    anthropic  Anthropic Messages（Claude）
    gemini     Google Gemini（generativelanguage）
    dashscope  阿里 DashScope 原生（通义千问）
"""
from __future__ import annotations

from .anthropic import AnthropicAdapter
from .base import (Adapter, finish_of, parse_sse_data, split_system, text_of,
                   tool_calls_to_openai, usage_of)
from .dashscope import DashScopeAdapter
from .gemini import GeminiAdapter
from .openai import OpenAIAdapter

ADAPTERS: dict[str, Adapter] = {
    a.protocol: a for a in (
        OpenAIAdapter(),
        AnthropicAdapter(),
        GeminiAdapter(),
        DashScopeAdapter(),
    )
}

PROTOCOLS: list[str] = list(ADAPTERS.keys())


def get_adapter(protocol: str | None) -> Adapter:
    """取适配器；空协议按 OpenAI 兼容处理（兼容历史数据）。"""
    key = (protocol or "").strip() or "openai"
    return ADAPTERS.get(key, ADAPTERS["openai"])


def is_supported(protocol: str | None) -> bool:
    p = (protocol or "").strip()
    return (not p) or p in ADAPTERS


__all__ = [
    "Adapter", "ADAPTERS", "PROTOCOLS", "get_adapter", "is_supported",
    "finish_of", "parse_sse_data", "split_system", "text_of",
    "tool_calls_to_openai", "usage_of",
]
