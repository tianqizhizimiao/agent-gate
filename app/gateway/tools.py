"""Tool registry + interceptor — mirrors ContextusAgent's ToolInterceptor.

* ``register(fn, name, description)`` derives an OpenAI function-tool JSON
  schema from the function's type hints and docstring.
* ``inject_tools(client_tools, tool_choice)`` merges server tools with the
  client's tools (server tools take priority on name collisions) and defaults
  ``tool_choice`` to ``auto`` when tools are present.
* ``extract_tool_calls`` parses an assistant message into structured calls.
* All-or-nothing interception is implemented in ``chat.py`` via ``is_server``:
  if every tool call in a response targets a server tool, the gateway executes
  them itself; otherwise the response is passed through untouched.
"""
from __future__ import annotations

import inspect
import json
import re
import typing

_PRIMITIVE = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _map_type(tp) -> dict:
    if tp in _PRIMITIVE:
        return {"type": _PRIMITIVE[tp]}
    if tp is inspect.Parameter.empty or tp is None or tp is type(None):
        return {"type": "string"}
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if tp is list or origin is list:
        inner = args[0] if args else str
        return {"type": "array", "items": _map_type(inner)}
    if tp is dict or origin is dict:
        return {"type": "object"}
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _map_type(non_none[0])
    return {"type": "string"}


def _parse_doc(fn) -> tuple[str, dict]:
    doc = inspect.getdoc(fn) or ""
    lines = doc.splitlines()
    summary = lines[0].strip() if lines else ""
    params: dict[str, str] = {}
    for line in lines:
        m = re.match(r"\s*:param\s+(\w+)\s*:\s*(.*)", line)
        if m:
            params[m.group(1)] = m.group(2).strip()
    return summary, params


class ToolInterceptor:
    def __init__(self):
        self._tools: dict[str, dict] = {}  # name -> {spec, handler}

    # -- registration ----------------------------------------------------
    def register(self, fn, name: str | None = None, description: str | None = None) -> str:
        sig = inspect.signature(fn)
        summary, pdocs = _parse_doc(fn)
        props: dict[str, dict] = {}
        required: list[str] = []
        for pname, p in sig.parameters.items():
            if pname in ("self", "cls"):
                continue
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
            sch = _map_type(ann)
            if pname in pdocs:
                sch["description"] = pdocs[pname]
            props[pname] = sch
            if p.default is inspect.Parameter.empty:
                required.append(pname)
        tname = name or fn.__name__
        tdesc = description or summary or tname
        spec = {
            "type": "function",
            "function": {
                "name": tname,
                "description": tdesc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }
        self._tools[tname] = {"spec": spec, "handler": fn}
        return tname

    # -- inspection ------------------------------------------------------
    def specs(self) -> list[dict]:
        return [t["spec"] for t in self._tools.values()]

    def list_tools(self) -> list[dict]:
        return [
            {
                "name": t["spec"]["function"]["name"],
                "description": t["spec"]["function"]["description"],
                "parameters": t["spec"]["function"]["parameters"],
            }
            for t in self._tools.values()
        ]

    def names(self) -> set[str]:
        return set(self._tools)

    def is_server(self, name: str) -> bool:
        return name in self._tools

    def handler_for(self, name: str):
        return self._tools[name]["handler"]

    # -- injection -------------------------------------------------------
    def inject_tools(self, client_tools, tool_choice=None):
        server_names = self.names()
        merged: list[dict] = []
        if client_tools:
            for ct in client_tools:
                cname = (ct.get("function") or {}).get("name")
                if cname and cname in server_names:
                    continue  # server tools win on collision
                merged.append(ct)
        merged = self.specs() + merged
        choice = tool_choice
        if merged and (choice is None or choice == "none"):
            choice = "auto"
        if not merged:
            choice = None
        return merged, choice

    # -- extraction ------------------------------------------------------
    def extract_tool_calls(self, assistant_msg: dict) -> list[dict]:
        calls = assistant_msg.get("tool_calls") or []
        out: list[dict] = []
        for c in calls:
            fn = c.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = {}
            out.append({"id": c.get("id"), "name": fn.get("name"), "arguments": args})
        return out
