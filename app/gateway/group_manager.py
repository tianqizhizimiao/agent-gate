"""Group runtime cache.

Each tool group has one ``GroupRuntime`` holding its package loader, persistent
KV store, tool-call log store, and the currently loaded tools/prompt. Runtimes
are built lazily from the ``tool_groups`` table and cached by group id.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .. import config, database
from .logger import ToolLogStore
from .plugin_manager import GroupPackageLoader
from .storage import PluginStorage

_SYS_DIR_NAME = ".agentgate"


class GroupRuntime:
    def __init__(self, group_id, folder_name, package_dir, storage, logs, loader):
        self.group_id = group_id
        self.folder_name = folder_name
        self.package_dir = Path(package_dir)
        self.storage = storage
        self.logs = logs
        self.loader = loader
        self.tools = None
        self.prompt = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()

    async def ensure_loaded(self, caller_name: str = "system"):
        async with self._lock:
            if self.tools is None or self.loader.needs_reload():
                try:
                    tools, prompt = await asyncio.to_thread(self.loader.load, caller_name)
                    self.tools = tools
                    self.prompt = prompt
                    self.last_error = None
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    raise
        return self


class GroupManager:
    def __init__(self):
        self._runtimes: dict[str, GroupRuntime] = {}
        self._lock = asyncio.Lock()

    def _build(self, row) -> GroupRuntime:
        folder_name = row["folder_name"]
        package_dir = config.TOOLGROUPS_DIR / folder_name
        package_dir.mkdir(parents=True, exist_ok=True)
        sys_dir = package_dir / _SYS_DIR_NAME
        storage = PluginStorage(sys_dir / "data.db")
        logs = ToolLogStore(sys_dir / "logs.db", config.LOG_RETENTION_SECONDS)
        loader = GroupPackageLoader(row["id"], package_dir, storage)
        return GroupRuntime(row["id"], folder_name, package_dir, storage, logs, loader)

    async def get(self, group_id: str):
        async with self._lock:
            rt = self._runtimes.get(group_id)
            if rt is not None:
                return rt
            row = database.query_one("SELECT * FROM tool_groups WHERE id = ?", (group_id,))
            if row is None:
                return None
            rt = self._build(row)
            self._runtimes[group_id] = rt
            return rt

    def invalidate(self, group_id: str) -> None:
        self._runtimes.pop(group_id, None)


manager = GroupManager()
