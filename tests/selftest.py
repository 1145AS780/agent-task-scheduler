#!/usr/bin/env python3
"""BTS 自测：覆盖三层实体的核心语义，供 CI 与本地验证使用。

用法：
    python tests/selftest.py            # 全部用例
    python tests/selftest.py -k task    # 只跑名字含 task 的用例

每个用例在独立的临时 BTS_ROOT 中运行，互不干扰，也不污染仓库。
标准库 only，退出码 0 表示全部通过。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BTS = REPO / "scripts" / "bts.py"
BTS_PLAN = REPO / "scripts" / "bts_plan.py"
EXAMPLES = REPO / "references" / "examples"

DEAD_PID = 2147483647

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class Sandbox:
    """一次用例的隔离环境：独立的 BTS_ROOT 与临时目录。"""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bts-selftest-"))
        self.root = self.dir / "root"

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def bts(self, *args, timeout=90):
        return self._run(BTS, *args, timeout=timeout)

    def plan(self, *args, timeout=120):
        return self._run(BTS_PLAN, *args, timeout=timeout)

    def _run(self, script, *args, timeout):
        cmd = [sys.executable, str(script), "--root", str(self.root)] + [str(a) for a in args]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")

    def json_out(self, *args, **kw):
        return self._json(self.bts, *args, **kw)

    def json_out_plan(self, *args, **kw):
        return self._json(self.plan, *args, **kw)

    def _json(self, runner, *args, **kw):
        rc, out, err = runner(*args, **kw)
        try:
            return rc, json.loads(out)
        except json.JSONDecodeError:
            raise AssertionError("输出不是合法 JSON (rc=%s)\nstdout=%r\nstderr=%r" % (rc, out, err))

    def write_policy(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "policy.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ---------------------------------------------------------------- 用例

@case
def task_lifecycle():
    """L0 任务：create -> run -> succeeded，退出码 0。"""
    sb = Sandbox()
    try:
        rc, task = sb.json_out("create", "hello", "--command", "echo hi", "--level", "L0")
        expect(rc == 0, "create 退出码应为 0，实际 %s" % rc)
        expect(task["task"]["status"] == "pending", "新建任务应为 pending")

        rc, run = sb.json_out("run", "hello", "--wait")
        expect(rc == 0, "run 退出码应为 0，实际 %s" % rc)

        rc, st = sb.json_out("status", "hello")
        expect(st["status"] == "succeeded", "任务应 succeeded，实际 %s" % st["status"])
        expect(st["exit_code"] == 0, "exit_code 应为 0，实际 %s" % st["exit_code"])
    finally:
        sb.cleanup()


@case
def task_failure_code():
    """命令非零退出时，任务 failed 且保留真实退出码。"""
    sb = Sandbox()
    try:
        code = 3
        sb.json_out("create", "boom", "--command",
                    '"%s" -c "import sys; sys.exit(%d)"' % (sys.executable, code),
                    "--level", "L0")
        sb.json_out("run", "boom", "--wait")
        rc, st = sb.json_out("status", "boom")
        expect(st["status"] == "failed", "应 failed，实际 %s" % st["status"])
        expect(st["exit_code"] == code, "exit_code 应回传 %d，实际 %s" % (code, st["exit_code"]))
    finally:
        sb.cleanup()


@case
def permission_denied_by_cap():
    """策略上限低于所需等级时直接拒绝：denied_policy，退出码 4。"""
    sb = Sandbox()
    try:
        sb.write_policy({"max_level": "L1", "actor_level": "L3"})
        sb.json_out("create", "risky", "--command", "echo nope", "--level", "L2")
        rc, out, err = sb.bts("run", "risky", "--wait")
        expect(rc == 4, "越权应退出 4，实际 %s\n%s%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("error") == "denied_policy",
               "应报 denied_policy，实际 %r" % payload.get("error"))
    finally:
        sb.cleanup()


@case
def permission_needs_approval():
    """所需等级高于身份上限且未超策略上限时，挂起为 approval_required。"""
    sb = Sandbox()
    try:
        sb.write_policy({"max_level": "L3", "actor_level": "L1"})
        sb.json_out("create", "gated", "--command", "echo gated", "--level", "L2")
        rc, out, err = sb.bts("run", "gated", "--wait")
        expect(rc == 4, "待审批应退出 4，实际 %s\n%s%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("error") == "approval_required",
               "应报 approval_required，实际 %r" % payload.get("error"))
        expect(payload.get("approval_id"), "应生成审批单号")
    finally:
        sb.cleanup()


@case
def plan_dependency_propagation():
    """依赖失败向下游传播：失败 1、级联跳过 2、独立分支照常成功 1。"""
    sb = Sandbox()
    try:
        spec = EXAMPLES / "plan-deps-retry.json"
        expect(spec.exists(), "缺少示例 %s" % spec)
        rc, sub = sb.json_out_plan("submit_plan", "--spec", spec)
        expect(rc == 0, "submit_plan 应成功，实际 %s" % rc)
        plan_id = sub["plan_id"]

        rc, run = sb.json_out_plan("run_plan", plan_id)
        expect(rc == 0, "run_plan 应正常返回，实际 %s" % rc)
        counts = run["counts"]
        expect(counts.get("failed") == 1, "应有 1 个 failed，实际 %r" % counts)
        expect(counts.get("skipped") == 2, "应有 2 个 skipped（级联），实际 %r" % counts)
        expect(counts.get("succeeded") == 1, "应有 1 个 succeeded（独立分支），实际 %r" % counts)
    finally:
        sb.cleanup()


@case
def supervisor_lost_is_detected():
    """supervisor 真死且无退出码时，仍判 supervisor_lost（不能被修复吞掉）。"""
    sb = Sandbox()
    try:
        sb.json_out("create", "lost", "--command", "echo x", "--level", "L0")
        p = sb.root / "tasks" / "lost" / "task.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d.update({"status": "running", "supervisor_pid": DEAD_PID,
                  "pid": DEAD_PID - 1, "finalized": False})
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        ec = sb.root / "tasks" / "lost" / "exit_code"
        if ec.exists():
            ec.unlink()

        rc, st = sb.json_out("status", "lost")
        raw = json.loads(p.read_text(encoding="utf-8"))
        expect(st["status"] == "failed", "应 failed，实际 %s" % st["status"])
        expect(raw.get("note") == "supervisor_lost",
               "应标记 supervisor_lost，实际 %r" % raw.get("note"))
    finally:
        sb.cleanup()


@case
def finished_task_not_misreported():
    """supervisor 已写退出码但进程已退出时，不得误判为 supervisor_lost。"""
    sb = Sandbox()
    try:
        sb.json_out("create", "race", "--command", "echo x", "--level", "L0")
        p = sb.root / "tasks" / "race" / "task.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d.update({"status": "running", "supervisor_pid": DEAD_PID,
                  "pid": DEAD_PID - 1, "finalized": False})
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        (sb.root / "tasks" / "race" / "exit_code").write_text("0", encoding="utf-8")

        rc, st = sb.json_out("status", "race")
        expect(st["status"] == "succeeded", "应 succeeded，实际 %s" % st["status"])
        expect(st["exit_code"] == 0, "exit_code 应为 0，实际 %s" % st["exit_code"])
    finally:
        sb.cleanup()


# ---------------------------------------------------------------- 入口

def utf8_console():
    """把 stdout/stderr 切到 UTF-8。

    Windows 上被管道重定向时默认用 cp1252，打印中文会抛 UnicodeEncodeError，
    表现为用例全过却退出码非零。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv):
    utf8_console()
    keyword = None
    if "-k" in argv:
        i = argv.index("-k")
        if i + 1 < len(argv):
            keyword = argv[i + 1]

    selected = [c for c in CASES if not keyword or keyword in c.__name__]
    if not selected:
        print("没有匹配的用例: %r" % keyword)
        return 1

    failed = []
    for fn in selected:
        name = fn.__name__
        try:
            fn()
        except Exception as exc:
            failed.append((name, exc))
            print("FAIL  %s\n      %s" % (name, str(exc).replace("\n", "\n      ")))
        else:
            print("PASS  %s" % name)

    print("\n%d/%d 通过" % (len(selected) - len(failed), len(selected)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
