#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ats_telegraph.py — 战情看板（回执板的终端视图）。

从 ats.py 拆出的功能模块：只把 <root>/telegraph/Receive.txt 渲染成看板文本并打到
stderr。回执的写入（receipt_path / append_receipt / 追加锁）仍留在 ats.py，本模块只读。

依赖方向：本模块 -> ats.py（core）。用 _core() 延迟导入，避免与 ats.py 的顶层再导出
形成循环导入：ats.py 在模块顶层 `from ats_telegraph import ...`，若本模块也在顶层
`import ats`，那么「先导入本模块」时 ats 尚未初始化完，会拿到半成品。
"""

from __future__ import annotations

import sys

_CORE = None


def _core():
    global _CORE
    if _CORE is None:
        import ats
        _CORE = ats
    return _CORE


def board_text(root, limit=None):
    """战情看板正文；没有任何回执时返回空串（首次使用不该刷一屏空表）。"""
    ats = _core()
    if limit is None:
        limit = ats.BOARD_LIMIT
    receipts = ats.read_receipts(root)
    if not receipts:
        return ""
    if len(receipts) > limit:
        head = "==== 战情看板（共 %d 条，只显示最后 %d 条）====" % (len(receipts), limit)
    else:
        head = "==== 战情看板（共 %d 条）====" % len(receipts)
    rows = ["Line %-4d %s" % (r["line"], r["text"]) for r in receipts[-limit:]]
    # 只给相对路径：看板会进终端与粘贴记录，绝对路径会把本机目录结构一起带出去
    return "\n".join([head] + rows
                     + ["回执文件：%s/%s" % (ats.TELEGRAPH_DIR, ats.RECEIPT_FILE)])


def emit_board(root):
    """把战情看板打到 stderr：stdout 留给纯 JSON，人眼在同一终端里仍先看到看板。"""
    text = board_text(root)
    if not text:
        return
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
