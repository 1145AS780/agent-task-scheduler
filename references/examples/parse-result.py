#!/usr/bin/env python3
"""把一次 ats / ats_plan 调用解析成「可分支的结论」。

用法：
    python reference\\examples\\parse-result.py task run build1 --wait
    python reference\\examples\\parse-result.py plan run_plan ci-lint-test-a1
    python reference\\examples\\parse-result.py --root D:\\work\\.ats plan show gated

输出 JSON：
    {"layer", "verdict", "next", "exit_code", "details", "raw"}

verdict 取值：
    ok_created                  创建成功（task 层 create）
    ok_running                  仍在进行（pending / starting / running）
    ok_task_succeeded / ok_task_failed / ok_task_timeout / ok_task_killed
    ok_plan_done / ok_plan_failed / ok_plan_paused / ok_plan_cancelled
    ok_query                    查询类命令成功
    bad_input                   退出码 2，参数/spec 非法
    not_found                   退出码 3，目标不存在
    conflict                    退出码 3，状态冲突
    need_approval               退出码 4，等人工批准
    denied                      退出码 4，策略上限拒绝（审批无用）
    unknown                     无法归类

退出码：与底层调用一致（0/2/3/4），便于在管道里串联。
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "task": SKILL_ROOT / "scripts" / "ats.py",
    "plan": SKILL_ROOT / "scripts" / "ats_plan.py",
}

QUERY_COMMANDS = {
    "check", "list", "show", "status", "events", "logs", "policy",
    "history", "prune",
}

# 退出码 3 里，「不存在」与「状态冲突」的区分
CONFLICT_ERRORS = {
    "state_conflict", "rerun_requires_force", "not_running", "plan_done",
    "already_cancelled", "no_current_plan", "stage_not_found", "step_not_found",
    "schedule_exists", "task_exists", "already_approved", "already_rejected",
    "already_expired",
}


def run(layer, argv, root=None):
    script = SCRIPTS[layer]
    cmd = [sys.executable, str(script)]
    if root:
        cmd += ["--root", str(root)]
    cmd += argv
    # 子进程 stdout 是管道时会按 locale（中文 Windows 上是 GBK）编码；
    # 若父进程按 UTF-8 解，中文就变成 U+FFFD 并导致 JSON 解析失败。
    # 强制子进程用 UTF-8，读端才有一致的编码约定。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {"raw": proc.stdout}
    return proc.returncode, data, proc.stderr


def classify(code, data, layer):
    """返回 (verdict, next_hint, details)。"""
    if not isinstance(data, dict):
        data = {}

    if code == 2:
        errors = data.get("errors")
        hint = "按 errors[] 逐条修 spec" if errors else "检查参数与 spec 路径"
        return "bad_input", hint, {"error": data.get("error"), "errors": errors}

    if code == 3:
        err = data.get("error") or (data.get("approvals") or [{}])[0].get("error")
        if err in CONFLICT_ERRORS:
            return "conflict", conflict_hint(err, data), {"error": err}
        if err == "missing":
            return "not_found", "id 不存在：approval 需先 run 触发一次；plan 通常带 -a<N> 后缀", {
                "error": err,
            }
        return "not_found", "用 list 找真实 id（plan 通常带 -a<N> 后缀）", {"error": err}

    if code == 4:
        err = data.get("error") or data.get("reason")
        if err == "denied_policy":
            return "denied", "改 policy.json 的 max_level 或降低 level（审批不能绕过）", {
                "error": err,
                "level": data.get("level") or data.get("required_level"),
                "policy_max": data.get("policy_max"),
            }
        if err == "approval_required":
            return "need_approval", "ats.py approve <approval_id> 后带 --approval 重发同一命令", {
                "error": err,
                "approval_id": data.get("approval_id"),
                "approval_error": data.get("approval_error"),
                "resume": data.get("resume"),
            }
        if err == "insufficient_level":
            return "denied", "批准者身份需 ≥ 该审批的要求等级：提高 ATS_ACTOR_LEVEL 后再批", {
                "error": err,
                "approval_id": data.get("approval_id"),
                "required_level": data.get("required_level"),
                "actor_level": data.get("actor_level"),
            }
        return "denied", "检查策略与身份等级", {"error": err}

    # code == 0
    if layer == "task":
        # run/create 把任务嵌在 data["task"] 里；show 直接平铺在顶层
        task = data.get("task")
        if not isinstance(task, dict):
            task = data if data.get("id") and data.get("status") else {}
        if data.get("created") is not None:
            return "ok_created", "已创建：用 run 启动，或 run --wait 同步等待", {
                "task_id": task.get("id"),
            }
        status = task.get("status")
        if status == "succeeded":
            return "ok_task_succeeded", "无需动作", {"status": status,
                                                     "exit_code": task.get("exit_code")}
        if status == "failed":
            return "ok_task_failed", "读 show --lines 80 的 stderr_tail", {
                "status": status, "exit_code": task.get("exit_code"),
                "note": task.get("note"),
            }
        if status == "timeout":
            return "ok_task_timeout", "调大 --timeout 或排查卡住点", {
                "status": status, "exit_code": task.get("exit_code"),
            }
        if status == "killed":
            return "ok_task_killed", "被 stop 终止，需要则 run --force", {"status": status}
        if status:
            return "ok_running", "仍在进行：按 next_poll 轮询 check", {
                "status": status, "pid": task.get("pid"),
            }
        return "ok_query", "查询成功", {}

    # plan 层
    status = data.get("status")
    if status == "done":
        return "ok_plan_done", "无需动作", {"counts": data.get("counts")}
    if status == "failed":
        failed = []
        for stage in data.get("stages") or []:
            if stage.get("result") == "failed":
                failed.append({"stage_id": stage.get("stage_id"),
                               "detail": stage.get("detail")})
        return "ok_plan_failed", "定位失败 stage 的 step，读其 stderr.log；必要时 rerun_task", {
            "counts": data.get("counts"), "failed_stages": failed,
        }
    if status == "paused":
        ap = data.get("approval") or {}
        return "ok_plan_paused", "ats.py approve <id> 后 resume --approval <id>", {
            "approval_id": ap.get("id"),
            "required_level": ap.get("required_level"),
            "approval_error": data.get("approval_error"),
        }
    if status == "cancelled":
        return "ok_plan_cancelled", "已被 cancel", {}
    if status == "running":
        return "unknown", "仍在执行，继续轮询", {"status": status}
    return "ok_query", "查询成功", {}


def conflict_hint(err, data):
    hints = {
        "state_conflict": "先看当前 status：运行中不能 run/delete",
        "rerun_requires_force": "加 --force 重跑终态任务",
        "not_running": "任务不在运行，无需 stop",
        "plan_done": "Plan 已结束；局部重跑用 rerun_task",
        "already_cancelled": "已被取消，无需重复",
        "no_current_plan": "先 run_plan 或 check_schedule 触发一次",
        "stage_not_found": "show 查看 stage 列表",
        "step_not_found": "show 查看该 stage 的 step 列表",
        "schedule_exists": "换 --id 或先 delete 旧实体（delete 需调用两次才能删到 schedule）",
        "task_exists": "换 id 或加 --force",
        "missing": "approval_id 不存在：确认 id 或先 run 触发一次",
        "already_approved": "已批准，直接带 --approval 执行，无需再批",
        "already_rejected": "已拒绝，审批不可翻案：重新触发一次以生成新审批",
        "already_expired": "已过期：重新触发，拿新的 approval_id",
        "expired": "审批 TTL 已过：重新触发，拿新的 approval_id",
    }
    return hints.get(err, "按 error 字段处理")


def main(argv=None):
    ap = argparse.ArgumentParser(description="解析 ats / ats_plan 的调用结果")
    ap.add_argument("--root", default=None)
    ap.add_argument("layer", choices=["task", "plan"])
    ap.add_argument("argv", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)

    if not args.argv:
        ap.error("缺少要执行的子命令，例如：parse-result.py task run build1 --wait")

    code, data, stderr = run(args.layer, args.argv, args.root)
    verdict, hint, details = classify(code, data, args.layer)

    payload = {
        "layer": args.layer,
        "command": args.argv[0],
        "verdict": verdict,
        "next": hint,
        "exit_code": code,
        "details": details,
        "raw": data,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if stderr.strip():
        print(stderr.strip(), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
