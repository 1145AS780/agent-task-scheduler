#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ats_maintenance.py — 停机 / 封装生命周期（维护态）。

从 ats.py 拆出的功能模块：维护态标志读写（maintenance_path / read_maintenance /
write_maintenance）、停机 unpack、维护态自测 tryrun、重封签 repack 与 Diff。

依赖方向：本模块 -> ats.py（core）。用 _core() 延迟导入，避免与 ats.py 的顶层再导出
形成循环导入。

口令的关键点：unpack 与 repack 都要 `ats.GUARD_PASSPHRASE`。它由 ats.py 的 main()
在 `__main__` 命名空间里赋值，所以必须经 ats 模块对象读取，不能在本模块另存一份
——否则读到的是 None，require_passphrase 会直接判 passphrase_missing。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

_CORE = None


def _core():
    global _CORE
    if _CORE is None:
        import ats
        _CORE = ats
    return _CORE


# ---------------------------------------------------------------- 停机 / 封装生命周期

def maintenance_path(root):
    return Path(root) / _core().MAINTENANCE_FILE


def read_maintenance(root):
    """读取维护态标志。文件损坏按「维护态存在」处理：宁紧勿松。"""
    path = maintenance_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                    # noqa: BLE001
        return {"active": True, "broken": True, "reason": "maintenance 标志无法解析"}
    if not isinstance(data, dict):
        return {"active": True, "broken": True, "reason": "maintenance 标志格式不符"}
    data.setdefault("active", True)
    return data


def write_maintenance(root, record):
    path = maintenance_path(root)
    Path(root).mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def cmd_unpack(root, args):
    """停机进维护态（--exit 退出）。

    「释放句柄」不是本命令自己 close 什么：核心脚本的只读句柄由各 supervise
    进程持有，进程退出即释放。任务停干净之后，这些文件才允许删除或改名替换
    （原地覆写本来就不受只读句柄阻止，那条只能靠新进程过 verify 兜住）。
    """
    ats = _core()
    if args.exit:
        path = maintenance_path(root)
        if not path.is_file():
            return ats.print_json({"ok": False, "error": "maintenance_not_set",
                                   "hint": "当前不在维护态，无需退出；要停机请用 unpack --confirm",
                                   "path": str(path)}, ats.EXIT_CONFLICT)
        try:
            path.unlink()
        except OSError as exc:
            return ats.print_json({"error": "maintenance_exit_failed", "path": str(path),
                                   "detail": "%s: %s" % (type(exc).__name__, exc),
                                   "hint": "删除维护态标志失败，可能是权限问题；"
                                           "也可由人手动删除该文件"}, ats.EXIT_CONFLICT)
        ats.log_event(root, "maintenance_exited")
        return ats.print_json({"ok": True, "exited": True, "path": str(path),
                               "note": "已退出维护态：下一条执行类命令会重新过看门狗校验"})

    if not args.confirm:
        return ats.print_json({"error": "confirm_required",
                               "hint": "unpack 会把运行中的任务判为 killed，不带 --confirm 不执行；"
                                       "确认要停机就加 --confirm，并由人提供口令",
                               "tutorial": ats.tutorial(
                                   "停机是不可逆动作，先确认再执行",
                                   ["%s unpack --confirm" % ats._cmd_prefix()],
                                   "command-human.md#场景5")}, ats.EXIT_USAGE)

    auth = ats.require_passphrase(root)
    if not auth.get("ok"):
        return ats.print_json({"ok": False, "error": "passphrase_required", "detail": auth,
                               "hint": "停机需要人类口令：口令只走 --passphrase-stdin，不进 argv",
                               "tutorial": ats.tutorial(
                                   "停机需要口令确认",
                                   ["%s unpack --confirm" % ats._cmd_prefix()],
                                   "command-human.md#场景5")}, ats.EXIT_DENIED)

    stopped, leftovers = [], []
    for task in ats.list_tasks(root):
        ats.refresh_task(root, task)
        if task.get("status") not in ("starting", "running"):
            continue
        left, checked = ats.stop_process_tree(task)
        if left:
            leftovers.append({"task_id": task["id"], "leftovers": left,
                              "checked": checked})
            continue
        ats.finalize_task(root, task, "killed", task.get("exit_code"),
                          note="stopped_by_unpack")
        stopped.append(task["id"])

    record = {"active": True, "at": ats.now_ts(), "iso": ats.iso(), "by": "human-cli",
              "reason": (args.reason or "").strip() or "人类停机进维护态",
              "stopped": stopped, "leftovers": leftovers}
    path = write_maintenance(root, record)
    ats.log_event(root, "maintenance_entered", stopped=stopped,
                  leftovers=len(leftovers))
    return ats.print_json({"ok": True, "maintenance": record, "path": str(path),
                           "stopped": stopped, "leftovers": leftovers,
                           "degraded": bool(leftovers),
                           "note": "维护态生效：执行类命令一律拒绝，只放行 unpack / repack / "
                                   "tryrun 与只读命令",
                           "resume": "%s repack --confirm" % ats._cmd_prefix(),
                           "tutorial": ats.tutorial(
                               "已停机进维护态，现在可以改代码并用 tryrun 自测",
                               ["%s tryrun -- \"<命令>\"" % ats._cmd_prefix(),
                                "%s repack --confirm" % ats._cmd_prefix(),
                                "%s unpack --exit" % ats._cmd_prefix()],
                               "command-human.md#场景5")})


def _tail_text(text, limit=None):
    ats = _core()
    if limit is None:
        limit = ats.TRYRUN_OUTPUT_LIMIT
    text = text or ""
    return text if len(text) <= limit else "…" + text[-limit:]


def cmd_tryrun(root, args):
    """维护态下跑命令做自测：次数不限，但只在这个窗口里可用。

    它跳过看门狗准入 —— 维护态本来就是为「核心脚本已改动、verify 必失败」而开，
    不放行就没人能自测。窗口由人类带口令开启，风险由人显式承担；窗口外一律拒绝。
    """
    ats = _core()
    record = read_maintenance(root)
    if not record:
        return ats.print_json({"ok": False, "error": "maintenance_required",
                               "hint": "tryrun 只在维护态可用：先由人执行 unpack --confirm",
                               "tutorial": ats.tutorial(
                                   "tryrun 需要先停机进维护态",
                                   ["%s unpack --confirm" % ats._cmd_prefix()],
                                   "command-human.md#场景5")}, ats.EXIT_CONFLICT)

    words = list(args.command or [])
    if words and words[0] == "--":
        words = words[1:]
    if not words:
        return ats.print_json({"error": "command_required",
                               "hint": ats.hint_command(
                                   "用法是把完整命令行放在 -- 之后原样执行，例如",
                                   "tryrun", "--", '"python -V"')},
                              ats.EXIT_USAGE)

    cwd = Path(args.cwd) if args.cwd else Path(root)
    if not cwd.is_dir():
        return ats.print_json({"error": "cwd_not_found", "cwd": str(cwd),
                               "hint": "tryrun 的工作目录不存在，先确认 --cwd 拼写"},
                              ats.EXIT_CONFLICT)

    shell = not args.no_shell
    cmdline = " ".join(words) if shell else words
    timeout = args.timeout or ats.TRYRUN_TIMEOUT_DEFAULT
    shown = cmdline if shell else " ".join(words)
    started = time.time()
    try:
        proc = subprocess.run(cmdline, shell=shell, cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        duration = int((time.time() - started) * 1000)
        ats.log_event(root, "tryrun_timeout", cmd=shown, timeout=timeout)
        return ats.print_json({"ok": False, "error": "tryrun_timeout", "cmd": shown,
                               "timeout": timeout, "duration_ms": duration,
                               "cwd": str(cwd),
                               "hint": ats.hint_command(
                                   "命令超时被终止（默认上限 %d 秒）；确实需要更长时间就调大 --timeout 重跑，例如"
                                   % ats.TRYRUN_TIMEOUT_DEFAULT,
                                   "tryrun", "--", '"python -V"',
                                   "--timeout", "600")},
                              ats.EXIT_CONFLICT)
    except (OSError, subprocess.SubprocessError) as exc:
        return ats.print_json({"ok": False, "error": "tryrun_spawn_failed", "cmd": shown,
                               "detail": "%s: %s" % (type(exc).__name__, exc),
                               "hint": "命令没能启动：检查可执行文件是否存在、路径是否写错"},
                              ats.EXIT_CONFLICT)

    duration = int((time.time() - started) * 1000)
    ok = proc.returncode == 0
    ats.log_event(root, "tryrun_executed", cmd=shown, exit_code=proc.returncode,
                  duration_ms=duration)
    return ats.print_json({"ok": ok, "exit_code": proc.returncode, "cmd": shown,
                           "duration_ms": duration, "cwd": str(cwd),
                           "stdout_tail": _tail_text(proc.stdout),
                           "stderr_tail": _tail_text(proc.stderr),
                           "maintenance": {"at": record.get("at"),
                                           "reason": record.get("reason")},
                           "note": "维护态下自测不限次数；改完由人执行 repack --confirm 重建基线"},
                          ats.EXIT_OK if ok else ats.EXIT_CONFLICT)


def _repack_diff(root):
    """对比清单记录与当前文件，给出 changed / missing / added 三张名单。"""
    ats = _core()
    manifest = ats._read_guard_manifest(root)
    if manifest is None:
        return {"manifest": None, "changed": [], "missing": [], "added": [],
                "unchanged": [],
                "note": "清单缺失，无法比对基线；repack --confirm 会直接重建"}
    files = manifest.get("files") or {}
    here = Path(__file__).resolve().parent
    changed, missing, unchanged = [], [], []
    for name, expected in sorted(files.items()):
        path = here / name
        if not path.is_file():
            missing.append(name)
            continue
        actual = ats._sha256_file(path)
        (unchanged if actual == expected else changed).append(name)
    known = set(files)
    added = sorted(p.name for p in here.glob("*.py") if p.name not in known)
    return {"manifest": str(ats._guard_manifest_path(root)), "changed": changed,
            "missing": missing, "added": added, "unchanged": unchanged}


def cmd_repack(root, args):
    """维护态下重建封签基线：不带 --confirm 只出 Diff，带 --confirm 才 seal。

    它与 `pack`（加载指令包）是两件事，名字刻意错开，避免同名词义混淆。
    """
    ats = _core()
    record = read_maintenance(root)
    if not record:
        return ats.print_json({"ok": False, "error": "maintenance_required",
                               "hint": "repack 只在维护态可用：先由人执行 %s；"
                                       "不在维护态时重建基线请直接用 %s"
                                       % (ats._cmd_prefix() + " unpack --confirm",
                                          ats.guard_command("seal", "--confirm")),
                               "tutorial": ats.tutorial(
                                   "repack 需要先停机进维护态",
                                   ["%s unpack --confirm" % ats._cmd_prefix()],
                                   "command-human.md#场景5")}, ats.EXIT_CONFLICT)

    diff = _repack_diff(root)
    if not args.confirm:
        return ats.print_json({"ok": True, "confirmed": False, "diff": diff,
                               "note": "只出 Diff，未写盘。确认无误后由人加 --confirm 并给口令",
                               "resume": "%s repack --confirm" % ats._cmd_prefix()},
                              ats.EXIT_OK)

    auth = ats.require_passphrase(root)
    if not auth.get("ok"):
        return ats.print_json({"ok": False, "error": "passphrase_required", "detail": auth,
                               "diff": diff,
                               "hint": "重建基线会把当前文件全部写进清单，必须由人带口令执行",
                               "tutorial": ats.tutorial(
                                   "重封签需要口令确认",
                                   ["%s repack --confirm" % ats._cmd_prefix()],
                                   "command-human.md#场景5")}, ats.EXIT_DENIED)

    rc, payload = ats._run_guard(["seal", "--confirm"], root, ats.GUARD_PASSPHRASE)
    if rc != ats.EXIT_OK or not payload.get("ok"):
        return ats.print_json({"ok": False, "error": "repack_failed", "diff": diff,
                               "guard": payload,
                               "hint": "seal 未通过：看 guard 字段的 error；口令不对或密钥缺失时"
                                       "先按其中的 next_action 处理，维护态保持不动"},
                              rc if rc else ats.EXIT_DENIED)

    path = maintenance_path(root)
    cleared = False
    try:
        if path.is_file():
            path.unlink()
            cleared = True
    except OSError as exc:
        ats.log_event(root, "maintenance_exit_failed", detail=str(exc))
        return ats.print_json({"ok": False, "error": "maintenance_exit_failed",
                               "sealed": payload, "path": str(path),
                               "hint": "基线已重建，但维护态标志删不掉；由人手动删除该文件后"
                                       "再执行任何命令"},
                              ats.EXIT_CONFLICT)
    ats.log_event(root, "maintenance_repacked", cleared=cleared,
                  changed=diff.get("changed"), added=diff.get("added"))
    return ats.print_json({"ok": True, "confirmed": True, "diff": diff,
                           "sealed": payload, "maintenance_cleared": cleared,
                           "note": "基线已重建，维护态已退出：下一条执行类命令按新基线校验"})
