"""Package loader for a tool group.

A tool group's on-disk folder is a Python package (it has ``__init__.py``).
Unlike ContextusAgent — which imports *every* ``.py`` in a plugins directory as
a separate plugin — AgentGate imports the folder as a single package: only
``__init__.py`` runs, and it explicitly imports whatever helper modules it
needs via relative imports (``from . import helper``).

The loader injects ``prompt``, ``tool``, ``database`` and ``name`` into the module
globals before executing ``__init__.py``, and re-executes the package whenever
a ``.py`` file changes (hot reload), mirroring ContextusAgent's PluginManager.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .plugin_api import PluginContext, NameProxy, make_prompt_fn, make_tool_decorator, set_context
from .prompt import PromptInjector
from .tools import ToolInterceptor


class GroupPackageLoader:
    def __init__(self, group_id: str, package_dir: Path, storage):
        self.group_id = group_id
        self.package_dir = Path(package_dir)
        self.storage = storage
        self._mtime: float | None = None

    def _module_name(self) -> str:
        return self.package_dir.name

    def _compute_mtime(self) -> float:
        # Only .py files drive reloads (data files / the .agentgate db do not).
        return max(
            (p.stat().st_mtime for p in self.package_dir.rglob("*.py") if p.is_file()),
            default=0.0,
        )

    def needs_reload(self) -> bool:
        return self._compute_mtime() != self._mtime

    def load(self, caller_name: str = "system"):
        tools = ToolInterceptor()
        prompt = PromptInjector()
        ctx = PluginContext(tools, prompt, self.storage, caller_name)
        set_context(ctx)

        init_path = self.package_dir / "__init__.py"
        if not init_path.exists():
            self._mtime = self._compute_mtime()
            return tools, prompt

        mod_name = self._module_name()
        parent = str(self.package_dir.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)

        # Drop any previously loaded version of this package and its submodules.
        for key in list(sys.modules):
            if key == mod_name or key.startswith(mod_name + "."):
                sys.modules.pop(key, None)

        spec = importlib.util.spec_from_file_location(
            mod_name,
            str(init_path),
            submodule_search_locations=[str(self.package_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot create import spec for package {mod_name!r}")

        module = importlib.util.module_from_spec(spec)
        module.__package__ = mod_name
        # Inject the plugin API into the package namespace before execution.
        module.__dict__["prompt"] = make_prompt_fn()
        module.__dict__["tool"] = make_tool_decorator()
        module.__dict__["database"] = self.storage
        module.__dict__["db"] = self.storage          # 兼容别名（旧的写法仍可用）
        module.__dict__["name"] = NameProxy()
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        self._mtime = self._compute_mtime()
        return tools, prompt
