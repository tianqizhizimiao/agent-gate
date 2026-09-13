"""Plugin API exposed to a tool group's ``__init__.py``.

When a package is loaded, the loader injects these names into the module
globals so authors can write::

    prompt("You can call get_weather ...")

    @tool
    def get_weather(city: str) -> str:
        \"\"\"Return the weather for a city.
        :param city: city name
        \"\"\"
        ...

    # persistent per-group KV store (one SQLite db per group)
    database["last_city"] = city
    database["history"].append(city)      # list/dict 的修改会自动写回
    # identity of the current caller (the API key owner's username)
    who = str(name)

These mirror ContextusAgent's ``plugin.register`` / ``plugin.addPrompt`` /
``plugin.db`` / ``plugin.name`` but with a cleaner decorator-style surface.

Resolution is per-context: a ``PluginContext`` (tools, prompt, database, caller name)
is bound via a ContextVar both at package load time and around each tool
handler invocation (including in worker threads).
"""
from __future__ import annotations

import contextvars

_ctx: contextvars.ContextVar["PluginContext"] = contextvars.ContextVar("agentgate_plugin_ctx")


class PluginContext:
    __slots__ = ("tools", "prompt", "database", "name")

    def __init__(self, tools, prompt, database, name: str):
        self.tools = tools
        self.prompt = prompt
        self.database = database
        self.name = name


def set_context(ctx: PluginContext) -> None:
    _ctx.set(ctx)


def get_context() -> PluginContext:
    return _ctx.get()


def make_prompt_fn():
    def prompt(text):
        get_context().prompt.add(text)

    return prompt


def make_tool_decorator():
    def tool(_fn=None, *, name=None, description=None):
        def deco(fn):
            get_context().tools.register(fn, name=name, description=description)
            return fn

        if callable(_fn):
            return deco(_fn)
        return deco

    return tool


class NameProxy:
    """Dynamic caller-name proxy. ``str(name)`` resolves to the current caller."""

    def __str__(self) -> str:
        try:
            return str(get_context().name)
        except Exception:
            return "system"

    def __repr__(self) -> str:
        return self.__str__()

    def __eq__(self, other) -> bool:
        return str(self) == str(other)

    def __hash__(self) -> int:
        return hash(str(self))


def run_handler(ctx: PluginContext, handler, arguments: dict):
    """Run a tool handler in the current thread with the plugin context bound."""
    set_context(ctx)
    return handler(**arguments)
