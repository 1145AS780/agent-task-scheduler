#!/usr/bin/env python3
"""双语文档对齐检查：README.md 与 README.zh-CN.md 的结构与技术内容必须一致。

两份 README 是同一份文档的中英对照，译文本来就该不同，但下面这些必须逐一对齐：
  1. 块序列（标题 / 代码块 / 表格 / 图片 / 链接）的类型与顺序
  2. 标题层级序列（H1 / H2 / H3 的数量与顺序）
  3. 带语言标记的代码块（bash / json）去注释后的正文——注释会被翻译，代码不会
  4. 表格的行数与列数
  5. 图片路径（忽略 .zh-CN 语言后缀）
  6. 链接目标（README.md 与 README.zh-CN.md 视为等价）

无语言标记的代码块（仓库结构树）承载译文说明，只校验存在与顺序，不比对正文。

只检查、不改文件；退出码 0 表示两份文档对齐。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAIRS = (("README.md", "README.zh-CN.md"),)

CODE_LANGS = {"bash", "json", "shell", "sh", "console", "powershell", "py", "python"}

HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
IMG_RE = re.compile(r'<img[^>]*?src="([^"]+)"')
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

problems = []


def utf8_console():
    """把 stdout/stderr 切到 UTF-8，避免 Windows 管道下打印中文抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def parse(text):
    """按文档顺序抽出结构块，跳过代码块内部的行。"""
    blocks = []
    lines = text.splitlines()
    i = 0
    in_fence = False
    fence_lang = ""
    buf = []
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                fence_lang = stripped[3:].strip().lower()
                buf = []
            else:
                in_fence = False
                blocks.append({"kind": "fence", "lang": fence_lang,
                               "body": "\n".join(buf)})
            i += 1
            continue
        if in_fence:
            buf.append(line)
            i += 1
            continue
        m = HEAD_RE.match(line)
        if m:
            blocks.append({"kind": "head", "level": len(m.group(1)),
                           "text": m.group(2).strip()})
            i += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            rows = []
            while i < len(lines):
                t = lines[i].strip()
                if t.startswith("|") and t.endswith("|"):
                    rows.append(t)
                    i += 1
                else:
                    break
            blocks.append({"kind": "table", "rows": len(rows),
                           "cols": rows[0].count("|") - 1})
            continue
        for src in IMG_RE.findall(line):
            blocks.append({"kind": "img", "src": src})
        for dst in LINK_RE.findall(line):
            blocks.append({"kind": "link", "dst": dst})
        i += 1
    return blocks


def norm_fence(body):
    """去掉整行注释与行尾注释后比对正文——注释是译文，代码不是。"""
    out = []
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s{2,}#.*$", "", line).rstrip())
    return "\n".join(out).strip()


def norm_img(src):
    return re.sub(r"\.zh-CN(?=\.)", "", src)


def norm_link(dst):
    return "README" if dst in ("README.md", "README.zh-CN.md") else dst


def pick(blocks, kind):
    return [b for b in blocks if b["kind"] == kind]


def report(label, a, b):
    """逐项比对同一种结构块；不一致时指出第一处差异下标。"""
    if len(a) != len(b):
        problems.append("%s 数量不一致：en=%d zh=%d" % (label, len(a), len(b)))
    for idx in range(min(len(a), len(b))):
        if a[idx] != b[idx]:
            problems.append("%s 第 %d 项不一致：\n  en: %r\n  zh: %r"
                            % (label, idx, a[idx], b[idx]))
    return len(a), len(b)


def check_pair(en_name, zh_name):
    en = parse((REPO / en_name).read_text(encoding="utf-8"))
    zh = parse((REPO / zh_name).read_text(encoding="utf-8"))

    report("块序列", [b["kind"] for b in en], [b["kind"] for b in zh])
    report("标题层级", [b["level"] for b in pick(en, "head")],
           [b["level"] for b in pick(zh, "head")])

    code_en = [norm_fence(b["body"]) for b in pick(en, "fence")
               if b["lang"] in CODE_LANGS]
    code_zh = [norm_fence(b["body"]) for b in pick(zh, "fence")
               if b["lang"] in CODE_LANGS]
    report("代码块正文", code_en, code_zh)

    report("表格形状", [(b["rows"], b["cols"]) for b in pick(en, "table")],
           [(b["rows"], b["cols"]) for b in pick(zh, "table")])
    report("图片路径", [norm_img(b["src"]) for b in pick(en, "img")],
           [norm_img(b["src"]) for b in pick(zh, "img")])
    report("链接目标", [norm_link(b["dst"]) for b in pick(en, "link")],
           [norm_link(b["dst"]) for b in pick(zh, "link")])

    print("%s <-> %s" % (en_name, zh_name))
    print("  结构块 %d / 标题 %d / 代码块 %d / 表格 %d / 图片 %d / 链接 %d"
          % (len(en), len(pick(en, "head")), len(pick(en, "fence")),
             len(pick(en, "table")), len(pick(en, "img")),
             len(pick(en, "link"))))
    return len(en)


def main():
    utf8_console()
    missing = [n for pair in PAIRS for n in pair if not (REPO / n).is_file()]
    if missing:
        print("缺少文件：%s" % "、".join(missing))
        return 1

    total = 0
    for en_name, zh_name in PAIRS:
        total += check_pair(en_name, zh_name)

    if problems:
        for p in problems:
            print("FAIL  " + p)
        print("\n%d 处不一致，双语文档已漂移" % len(problems))
        return 1

    print("\nOK  双语文档结构对齐（%d 个结构块）" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
