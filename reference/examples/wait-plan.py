#!/usr/bin/env python3
"""按 next_poll 节奏轮询 Plan，直到进入终态（或暂停等审批）。

用法：
    python reference\\examples\\wait-plan.py ci-lint-test-a1
    python reference\\examples\\wait-plan.py gated --interval 2 --timeout 600
    python reference\\examples\\wait-plan.py patrol-sample --root D:\\work\\.bts

输出：进度写 stderr，最终结论写 stdout（JSON），便于管道消费。
退出码：0 = 已到终态（done/failed/cancelled）或 paused；3 = 找不到；1 = 超时。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
BTS_PLAN = SKILL_ROOT / "scripts" / "bts_plan.py"

# 对等待者而言，「暂停」也算停下来（接下来要人批审批）
HALTED = {"done", "failed", "cancelled", "paused"}


def call(ident, root):
    cmd = [sys.executable, str(BTS_PLAN)]
    if root:
        cmd += ["--root", str(root)]
    cmd += ["show", ident, "--json"]
    # 子进程 stdout 走管道时按 locale 编码（中文 Windows 为 GBK），
    # 父进程按 UTF-8 解会出现 U+FFFD 并破坏 JSON，故强制子进程输出 UTF-8。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    return proc.returncode, data


def current_status(data):
    plan = data.get("plan") or {}
    if plan.get("status"):
        return plan["status"], "plan"
    schedule = data.get("schedule") or {}
    if schedule.get("status"):
        return schedule["status"], "schedule"
    return None, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="轮询 Plan 直到终态")
    ap.add_argument("ident", help="plan_id（如 ci1-a1）或 schedule_id")
    ap.add_argument("--root", default=None, help="BTS_ROOT，默认读环境变量或 <cwd>/.bts")
    ap.add_argument("--interval", type=int, default=5, help="两次轮询间隔秒数（默认 5）")
    ap.add_argument("--timeout", type=int, default=0, help="总超时秒数，0 表示不限")
    ap.add_argument("--quiet", action="store_true", help="不打印进度")
    args = ap.parse_args(argv)

    started = time.time()
    polls = 0
    last = None

    while True:
        polls += 1
        code, data = call(args.ident, args.root)
        if code == 3 or data.get("error") == "not_found":
            print(json.dumps({"error": "not_found", "ident": args.ident},
                             ensure_ascii=False, indent=2))
            return 3

        status, scope = current_status(data)
        if status is None:
            print(json.dumps({"error": "no_plan_yet", "ident": args.ident,
                              "hint": "schedule 还没有 current_plan，先触发一次"},
                             ensure_ascii=False, indent=2))
            return 3

        if not args.quiet and status != last:
            print("  [%5.1fs] poll %d: %s=%s" % (time.time() - started, polls, scope, status),
                  file=sys.stderr)
        last = status

        if status in HALTED:
            summary = data.get("summary") or {}
            payload = {
                "ident": args.ident,
                "scope": scope,
                "status": status,
                "polls": polls,
                "elapsed": round(time.time() - started, 1),
                "counts": summary.get("counts"),
                "failed_steps": [
                    {"stage_id": s.get("stage_id"), "step_id": s.get("step_id"),
                     "status": s.get("status"), "exit_code": s.get("exit_code")}
                    for s in (summary.get("steps") or [])
                    if s.get("status") in ("failed", "timeout")
                ],
            }
            if status == "paused":
                plan = data.get("plan") or {}
                payload["paused_stage"] = plan.get("paused_stage")
                payload["paused_step"] = plan.get("paused_step")
                payload["hint"] = "先 bts.py approve <approval_id>，再 bts_plan.py resume <id> --approval <id>"
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.timeout and time.time() - started >= args.timeout:
            print(json.dumps({"ident": args.ident, "status": status, "error": "timeout",
                              "polls": polls, "elapsed": round(time.time() - started, 1)},
                             ensure_ascii=False, indent=2))
            return 1

        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    sys.exit(main())
