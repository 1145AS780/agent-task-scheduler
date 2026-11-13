#!/usr/bin/env python3
"""ATS 静态检查：只做零误报的约定校验，零第三方依赖。

检查项：
  1. 所有 Python 文件可编译（语法错误直接拦下）
  2. 缩进不使用制表符
  3. 行尾无多余空白
  4. 文件以换行结尾

退出码 0 表示全部通过。

第 1 项用内建 compile() 而不是 py_compile：py_compile 必须把 .pyc 落到磁盘，
而 ats_preflight.py 同时含 base64 解码调用与 eval/exec 字面量（用于识别编码载荷），
落盘的 pyc 会被杀软启发式误判成 Trojan/Python.ShellLoader。compile() 只在内存里
生成 code object，语法校验语义等价，且不产生任何 pyc。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGETS = ["scripts", "tests"]

problems = []


def py_files():
    out = []
    for d in TARGETS:
        base = REPO / d
        if base.is_dir():
            out.extend(sorted(base.glob("*.py")))
    return out


def check_compiles(files):
    for f in files:
        try:
            compile(f.read_bytes(), str(f), "exec")
        except SyntaxError as exc:
            problems.append("%s: 编译失败: %s" % (f.relative_to(REPO), exc))
        except ValueError as exc:
            problems.append("%s: 源码无法编译: %s" % (f.relative_to(REPO), exc))


def check_tabs(f):
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if re.match(r"^[ ]*\t", line):
            problems.append("%s:%d: 缩进含制表符" % (f.relative_to(REPO), i))


def check_trailing_ws(f):
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if line != line.rstrip():
            problems.append("%s:%d: 行尾有多余空白" % (f.relative_to(REPO), i))


def check_final_newline(f):
    if not f.read_bytes().endswith(b"\n"):
        problems.append("%s: 文件未以换行结尾" % f.relative_to(REPO))


def utf8_console():
    """把 stdout/stderr 切到 UTF-8，避免 Windows 管道下打印中文抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main():
    utf8_console()
    files = py_files()
    if not files:
        print("没有找到待检查的 Python 文件")
        return 1

    check_compiles(files)
    for f in files:
        check_tabs(f)
        check_trailing_ws(f)
        check_final_newline(f)

    if problems:
        for p in problems:
            print("FAIL  " + p)
        print("\n%d 处问题，检查未通过" % len(problems))
        return 1

    print("OK  %d 个文件通过静态检查" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
