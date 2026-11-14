#!/usr/bin/env python3
"""ATS 静态检查：只做零误报的约定校验，零第三方依赖。

检查项：
  1. 所有 Python 文件可编译（语法错误直接拦下）
  2. 缩进不使用制表符
  3. 行尾无多余空白
  4. 文件以换行结尾
  5. print_json({"error": ...}) 的错误返回体必须带 hint 或 message

退出码 0 表示全部通过。

第 1 项用内建 compile() 而不是 py_compile：py_compile 必须把 .pyc 落到磁盘，
而 ats_preflight.py 同时含 base64 解码调用与 eval/exec 字面量（用于识别编码载荷），
落盘的 pyc 会被杀软启发式误判成 Trojan/Python.ShellLoader。compile() 只在内存里
生成 code object，语法校验语义等价，且不产生任何 pyc。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGETS = ["scripts", "tests", "api"]

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


def check_actionable_error(f):
    """错误返回体必须给出下一步：print_json({"error": ...}) 要有 hint 或 message。

    只报错误码的返回体，调用方拿到后无法判断该改什么，只能去读源码 —— 这是
    「可修但不可猜」的失败，防呆设计上等价于没有返回体。只扫字面量字典：
    动态拼出来的返回体（**kwargs、变量）不猜，避免误报。
    ats_plan.py 的错误走 emit() 写 events.jsonl、不打 stdout，本项对它自然为 0。
    """
    try:
        tree = compile(f.read_bytes(), str(f), "exec", ast.PyCF_ONLY_AST)
    except (SyntaxError, ValueError):
        return                       # 语法错误已由 check_compiles 报过，不重复报
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "print_json":
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        keys = {k.value for k in node.args[0].keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "error" not in keys:
            continue
        if "hint" in keys or "message" in keys:
            continue
        problems.append("%s:%d: print_json({\"error\": ...}) 缺 hint/message —— "
                        "错误返回体必须给出可直接照做的下一步"
                        % (f.relative_to(REPO), node.lineno))


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
        check_actionable_error(f)

    if problems:
        for p in problems:
            print("FAIL  " + p)
        print("\n%d 处问题，检查未通过" % len(problems))
        return 1

    print("OK  %d 个文件通过静态检查" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
