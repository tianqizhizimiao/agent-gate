"""Prompt injection — mirrors ContextusAgent's PromptInjector.

A tool group's ``__init__.py`` calls ``prompt(text)`` to register injected
system prompt fragments. ``inject()`` prepends the combined text in front of
the conversation (merging with an existing leading system message).
"""
from __future__ import annotations


class PromptInjector:
    def __init__(self):
        self._parts: list[str] = []

    def add(self, text) -> None:
        if text is None:
            return
        text = str(text)
        if text and text not in self._parts:
            self._parts.append(text)

    def inject(self, messages: list[dict]) -> list[dict]:
        if not self._parts:
            return messages
        combined = "\n\n".join(self._parts)
        msgs = [dict(m) for m in messages]
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = combined + "\n\n" + (msgs[0].get("content") or "")
        else:
            msgs.insert(0, {"role": "system", "content": combined})
        return msgs

    @property
    def text(self) -> str:
        return "\n\n".join(self._parts)

    def __bool__(self) -> bool:
        return bool(self._parts)
