"""扫描仓库里所有文本文件，找「被按 GBK 解码后又以 UTF-8 写回」的损坏指纹。

指纹：
  1. 私用区字符 U+E000–U+F8FF（正常源码里绝不该出现）
  2. U+FFFD 替换字符
  3. 典型的 UTF-8→GBK 乱码汉字（锟斤拷 / 閻 / 鐟 / 閸 ... ）
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", "venv", ".piptmp", ".pipcache", "node_modules", "data"}
SELF = pathlib.Path(__file__).resolve()          # 本文件含检测用的乱码字，跳过自己

# 乱码汉字：这些字在正常中文里极少单独出现
MOJI = set("锟斤拷閻鐟閸缂佺鎴浣鍩涔娑鐎閿娴鏂鐢鍥銆鐨涓鏄鍜瑕鎬閫閺妞瀹鎵缁鍑閽闁婢濞")

bad: list[tuple[str, str, str]] = []
for p in sorted(ROOT.rglob("*")):
    if not p.is_file() or p.resolve() == SELF:
        continue
    if any(part in SKIP_DIRS for part in p.parts):
        continue
    if p.suffix.lower() not in {".py", ".html", ".js", ".css", ".md", ".json", ".txt", ".example"}:
        continue
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        bad.append((str(p.relative_to(ROOT)), "不是合法 UTF-8", str(e)[:60]))
        continue
    pua = [c for c in text if 0xE000 <= ord(c) <= 0xF8FF]
    if pua:
        bad.append((str(p.relative_to(ROOT)), f"私用区字符 {len(pua)} 个", repr("".join(pua[:8]))))
        continue
    if "\ufffd" in text:
        bad.append((str(p.relative_to(ROOT)), "含替换字符 U+FFFD", ""))
        continue
    hits = [c for c in text if c in MOJI]
    if hits:
        bad.append((str(p.relative_to(ROOT)), f"疑似乱码汉字 {len(hits)} 个", "".join(hits[:12])))

if bad:
    print(f"发现 {len(bad)} 个文件疑似损坏：\n")
    for name, why, extra in bad:
        print(f"  {name:<34} {why:<22} {extra}")
    sys.exit(1)
print("全部文件编码正常，未发现乱码指纹 ✓")
