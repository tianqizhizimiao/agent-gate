"""模板静态检查：抓「onclick 指向不存在的函数」「getElementById 取不到的 id」这类低级错误。

这类问题不会让页面报错，只会让按钮点了没反应 —— 浏览器控制台里才有 404/undefined，
很容易漏掉。所以在提交前静态扫一遍。

用法：
    python tools/check_template.py
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
STATIC_JS = ROOT / "static" / "js"
CSS = ROOT / "static" / "css" / "style.css"

FUNC_DEF = re.compile(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")
FUNC_ASSIGN = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()")
HANDLER = re.compile(r'on(?:click|change|input|submit)="([A-Za-z_$][\w$]*)\s*\(')
GET_ID = re.compile(r"""getElementById\(\s*["']([^"']+)["']\s*\)""")
HAS_ID = re.compile(r"""\bid=["']([^"']+)["']""")
CLASS_ATTR = re.compile(r"""class=["']([^"']+)["']""")


def js_symbols() -> set[str]:
    """模板 + 共享 app.js 里定义的函数名。"""
    names: set[str] = set()
    for p in list(TEMPLATES.glob("*.html")) + list(STATIC_JS.glob("*.js")):
        src = p.read_text(encoding="utf-8")
        for block in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", src):
            names |= set(FUNC_DEF.findall(block))
            names |= set(FUNC_ASSIGN.findall(block))
        if p.suffix == ".js":
            names |= set(FUNC_DEF.findall(src))
            names |= set(FUNC_ASSIGN.findall(src))
    return names


def main() -> int:
    defined = js_symbols()
    css = CSS.read_text(encoding="utf-8") if CSS.exists() else ""
    css_classes = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))

    problems = 0
    for tpl in sorted(TEMPLATES.glob("*.html")):
        src = tpl.read_text(encoding="utf-8")
        rel = tpl.relative_to(ROOT).as_posix()

        handlers = set(HANDLER.findall(src))
        missing = sorted(h for h in handlers if h not in defined)
        if missing:
            problems += len(missing)
            for m in missing:
                print(f"  [函数未定义] {rel}: {m}()")

        ids_used = set(GET_ID.findall(src))
        ids_present = set(HAS_ID.findall(src))
        ghost = sorted(i for i in ids_used if i not in ids_present)
        if ghost:
            problems += len(ghost)
            for g in ghost:
                print(f"  [id 不存在]  {rel}: #{g}")

        # 只在模板自有 class 里找明显笔误：以 agentgate 前缀/自定义类为准太严，
        # 这里只报「模板用了、CSS 里也没有、且不是工具类」的，容易误报，故跳过。
        _ = CLASS_ATTR

    if problems:
        print(f"\n发现 {problems} 个问题")
        return 1
    print("模板检查通过：事件处理函数与 id 全部有定义 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
