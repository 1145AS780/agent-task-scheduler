#!/usr/bin/env python3
"""ATS 迷你沙箱：给「改代码 -> 试新功能」一条最短路径。

定位：跑的是真实的 scripts/ats.py / ats_guard.py / ats_plan.py，不是另写一份调度器。
ATS 的复杂度大半在守卫链（封签、口令、审批绑产物、维护态），另写一份精简实现只能
验证那份实现，验证不到真实代码；所以这里只做三件真正费事的事：

  1. 一个固定沙箱根（temp/ats-mini/root）+ 固定口令，不必每次手敲 --root 与口令；
  2. 改完代码一条命令重封签（seal），info 里能直接看到「清单与代码已不一致」；
  3. reset 一键回到干净状态，不用删目录重建、也不必重新 init。

沙箱根与口令记在 temp/ats-mini/mini.json；temp/ 已在 .gitignore 里，不会进仓库。
本文件在 tests/ 下，不在封签清单范围内（清单只收 scripts/*.py）。

用法：
    python tests/mini.py new                     建沙箱（init + seal）
    python tests/mini.py new --fresh --confirm   推倒重建
    python tests/mini.py info                    沙箱状态 + 清单漂移
    python tests/mini.py seal                    改完代码后重封签
    python tests/mini.py reset --confirm         清任务/事件/回执，保留密钥与清单
    python tests/mini.py ats  <参数...>          真实 ats.py，自动带 --root 与口令
    python tests/mini.py guard <参数...>         真实 ats_guard.py
    python tests/mini.py plan <参数...>          真实 ats_plan.py

环境变量 ATS_MINI_ROOT 可换沙箱位置。退出码沿用被调用脚本的退出码。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

REPO = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO / "scripts"
STATE_DIR = REPO / "temp" / "ats-mini"
STATE_FILE = STATE_DIR / "mini.json"
DEFAULT_PASSPHRASE = "ats-mini-dev-passphrase-2026"

SCRIPTS = {"ats": "ats.py", "guard": "ats_guard.py", "plan": "ats_plan.py"}
RUNTIME_DIRS = ("tasks", "plans", "schedules", "visas", "telegraph")
RUNTIME_FILES = ("events.jsonl", "scheduler.log", "abort.json", "maintenance.json")
KEEP_FILES = ("ats_guard_key.json", "ats_guard_manifest.json", "policy.json")


def utf8_console():
    """Windows 被管道重定向时默认 cp1252，打印中文会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def read_state():
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def mini_root():
    """沙箱根：显式环境变量 > 上次 new 记下的位置 > 默认 temp/ats-mini/root。"""
    env = os.environ.get("ATS_MINI_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    state = read_state()
    if state.get("root"):
        return Path(state["root"])
    return (STATE_DIR / "root").resolve()


def passphrase():
    return read_state().get("passphrase") or DEFAULT_PASSPHRASE


def write_state(root, pw):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"root": str(root), "passphrase": pw,
                                      "updated": int(time.time())},
                                     ensure_ascii=False, indent=2),
                          encoding="utf-8")


def run_child(script, root, args, pw):
    """跑真实脚本：--root 与口令由这里补，stdout/stderr 原样透传。"""
    cmd = [sys.executable, str(SCRIPT_DIR / script), "--root", str(root)]
    cmd.extend(str(a) for a in args)
    cmd.append("--passphrase-stdin")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(cmd, input=pw + "\n", capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=str(REPO))
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_drift(root):
    """清单与当前 scripts/*.py 的差异：改过代码就必须重封签，这里给判据。"""
    manifest_path = root / "ats_guard_manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        sealed = json.loads(manifest_path.read_text(encoding="utf-8")).get("files") or {}
    except (OSError, json.JSONDecodeError):
        return {"broken": True, "changed": [], "missing": [], "added": []}
    current = {p.name: sha256_file(p) for p in sorted(SCRIPT_DIR.glob("*.py"))}
    changed = sorted(n for n in sealed if n in current and sealed[n] != current[n])
    missing = sorted(n for n in sealed if n not in current)
    added = sorted(n for n in current if n not in sealed)
    return {"broken": False, "changed": changed, "missing": missing, "added": added}


def task_summary(root):
    counts = {}
    running = 0
    for task_file in sorted((root / "tasks").glob("*/task.json")):
        try:
            task = json.loads(task_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = task.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
        if status in ("starting", "running"):
            running += 1
    return counts, running


def cmd_new(fresh=False, confirm=False, quiet=False):
    root = mini_root()
    pw = passphrase()
    if fresh:
        if not confirm:
            print("--fresh 会删掉整个沙箱（含任务历史与密钥），确认请加 --confirm")
            return 2
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
            print("已删除旧沙箱：%s" % root)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "ats_guard_key.json").is_file():
        if not quiet:
            print("[1/2] 建密钥：%s" % root)
        rc = run_child("ats_guard.py", root, ["init", "--confirm"], pw)
        if rc != 0:
            return rc
    if not quiet:
        print("[2/2] 封签")
    rc = run_child("ats_guard.py", root, ["seal", "--confirm"], pw)
    if rc != 0:
        return rc
    write_state(root, pw)
    if not quiet:
        print("")
        print("沙箱就绪：%s" % root)
        print("口令：%s（写在 %s，temp/ 不进仓库）" % (pw, STATE_FILE))
        print("下一步：python tests/mini.py ats create demo --command \"echo hi\" --level L0")
    return 0


def ensure_sandbox(quiet=True):
    """首次用 ats/guard/plan 时自动建沙箱，省掉「先跑 new」这一步。"""
    root = mini_root()
    if (root / "ats_guard_key.json").is_file() and (root / "ats_guard_manifest.json").is_file():
        return root, 0
    print("沙箱不存在，自动新建：%s" % root, file=sys.stderr)
    rc = cmd_new(quiet=quiet)
    return root, rc


def cmd_info():
    root = mini_root()
    print("沙箱根   : %s" % root)
    if not root.is_dir():
        print("状态     : 未创建（跑 python tests/mini.py new）")
        return 1
    key = root / "ats_guard_key.json"
    manifest = root / "ats_guard_manifest.json"
    print("密钥     : %s" % ("有" if key.is_file() else "无"))
    sealed_at = None
    drift = None
    if manifest.is_file():
        try:
            sealed_at = json.loads(manifest.read_text(encoding="utf-8")).get("sealed_iso")
        except (OSError, json.JSONDecodeError):
            sealed_at = None
        drift = manifest_drift(root)
    print("清单     : %s" % ("有（封签于 %s）" % sealed_at if manifest.is_file() else "无"))
    if drift is None:
        print("代码漂移 : 无清单可比（先跑 seal）")
    elif drift["broken"]:
        print("代码漂移 : 清单损坏，跑 seal 重建")
    elif drift["changed"] or drift["missing"] or drift["added"]:
        parts = []
        if drift["changed"]:
            parts.append("改过 " + "、".join(drift["changed"]))
        if drift["missing"]:
            parts.append("缺 " + "、".join(drift["missing"]))
        if drift["added"]:
            parts.append("新增 " + "、".join(drift["added"]))
        print("代码漂移 : 有（%s）-> 必须重封签：python tests/mini.py seal" % "；".join(parts))
    else:
        print("代码漂移 : 无（清单与当前 scripts/*.py 一致）")
    print("策略     : %s" % ("有（policy.json）" if (root / "policy.json").is_file() else "无（用默认）"))
    counts, running = task_summary(root)
    total = sum(counts.values())
    detail = " / ".join("%s %d" % (k, counts[k]) for k in sorted(counts))
    print("任务     : 共 %d%s" % (total, ("（%s）" % detail) if detail else ""))
    print("在跑     : %d" % running)
    print("中止标志 : %s" % ("有（abort.json）" if (root / "abort.json").is_file() else "无"))
    print("维护态   : %s" % ("有（maintenance.json）" if (root / "maintenance.json").is_file() else "无"))
    receipts = root / "telegraph" / "Receive.txt"
    if receipts.is_file():
        lines = receipts.read_text(encoding="utf-8", errors="replace").count("\n")
        print("回执板   : %d 行" % lines)
    else:
        print("回执板   : 无")
    print("下一步   : python tests/mini.py ats create demo --command \"echo hi\" --level L0")
    return 0


def cmd_seal():
    root, rc = ensure_sandbox(quiet=False)
    if rc != 0:
        return rc
    return run_child("ats_guard.py", root, ["seal", "--confirm"], passphrase())


def cmd_reset(confirm=False):
    root = mini_root()
    if not root.is_dir():
        print("沙箱不存在，无需 reset")
        return 0
    if not confirm:
        print("reset 会删掉任务历史、事件流、回执板与签证，保留密钥/清单/策略；"
              "确认请加 --confirm")
        return 2
    removed = []
    for name in RUNTIME_DIRS:
        path = root / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name + "/")
    for name in RUNTIME_FILES:
        path = root / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    kept = [n for n in KEEP_FILES if (root / n).is_file()]
    print("已清除：%s" % ("、".join(removed) if removed else "（本来就是干净的）"))
    print("已保留：%s" % ("、".join(kept) if kept else "（无）"))
    return 0


def usage():
    print(__doc__.strip())


def main(argv):
    utf8_console()
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "new":
        return cmd_new(fresh="--fresh" in rest, confirm="--confirm" in rest)
    if sub == "info":
        return cmd_info()
    if sub == "seal":
        return cmd_seal()
    if sub == "reset":
        return cmd_reset(confirm="--confirm" in rest)
    if sub in SCRIPTS:
        root, rc = ensure_sandbox()
        if rc != 0:
            return rc
        return run_child(SCRIPTS[sub], root, rest, passphrase())
    print("未知子命令：%s" % sub, file=sys.stderr)
    usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
