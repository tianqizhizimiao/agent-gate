"""静态检查：前端调用的 (method, path) 是否都能在后端找到对应路由。

用法：python tools/check_routes.py

先把前端源码里的路径拼接统一归一化（``" + encodeURIComponent(x) + "`` 与 ``${x}`` → ``*``），
再逐个和后端路由做形状比对。这类"前端写了但后端没这个路由"的错配靠肉眼很难发现
—— ``/api/admin/users/{name}/delete`` vs ``/api/admin/users/{name}`` 就是这么漏掉的。
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# 后端路由
# --------------------------------------------------------------------------- #
ROUTE_RE = re.compile(r'@router\.(get|post|put|delete|patch)\(\s*"([^"]+)"')
BACKEND: set[tuple[str, str]] = set()
for py in (ROOT / "app").rglob("*.py"):
    for m in ROUTE_RE.finditer(py.read_text(encoding="utf-8")):
        path = re.sub(r"\{[^}]+\}", "*", m.group(2))
        BACKEND.add((m.group(1).upper(), "/".join(s for s in path.split("/") if s)))

# --------------------------------------------------------------------------- #
# 前端调用
# --------------------------------------------------------------------------- #
CALL_RE = re.compile(r'API\(\s*(["`])([^"`]*)\1\s*(?:,\s*\{(.*?)\}\s*)?\)', re.S)


def normalize_source(src: str) -> str:
    """把路径拼接压成单一直量，便于用简单正则匹配。"""
    src = re.sub(r'"\s*\+\s*([^"+]+?)\s*\+\s*"', "*", src)   # " + encodeURIComponent(x) + "
    src = re.sub(r"\$\{[^}]*\}", "*", src)                   # `...${x}...`
    return src


def norm_front(path: str) -> str:
    return "/".join(s for s in path.split("/") if s)


calls: list[tuple[str, str, str]] = []
for f in sorted((ROOT / "templates").glob("*.html")) + sorted((ROOT / "static" / "js").glob("*.js")):
    src = normalize_source(f.read_text(encoding="utf-8"))
    for m in CALL_RE.finditer(src):
        path, opts = m.group(2).strip(), m.group(3) or ""
        if not path.startswith("/") or "+" in path:
            continue
        mm = re.search(r'method:\s*"(\w+)"', opts)
        calls.append((f.name, (mm.group(1).upper() if mm else "GET"), norm_front(path)))


def matches(method: str, path: str) -> bool:
    fs = path.split("/")
    for bm, bp in BACKEND:
        if bm != method:
            continue
        bs = bp.split("/")
        if len(bs) == len(fs) and all(x == "*" or x == y for x, y in zip(bs, fs)):
            return True
    return False


bad = [(f, m, p) for f, m, p in calls if not matches(m, p)]
print(f"后端路由 {len(BACKEND)} 条 / 前端调用 {len(calls)} 处")
if bad:
    print(f"\n发现 {len(bad)} 处前端调用在后端找不到路由：")
    for f, m, p in bad:
        print(f"  {f:<18} {m:<7} {p}")
    sys.exit(1)
print("\n全部匹配 ✓")
