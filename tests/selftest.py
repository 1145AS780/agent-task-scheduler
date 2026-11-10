#!/usr/bin/env python3
"""ATS 自测：覆盖三层实体的核心语义，供 CI 与本地验证使用。

用法：
    python tests/selftest.py            # 全部用例
    python tests/selftest.py -k task    # 只跑名字含 task 的用例

每个用例在独立的临时 ATS_ROOT 中运行，互不干扰，也不污染仓库。
标准库 only，退出码 0 表示全部通过。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ATS = REPO / "scripts" / "ats.py"
ATS_PLAN = REPO / "scripts" / "ats_plan.py"
EXAMPLES = REPO / "references" / "examples"

DEAD_PID = 2147483647

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class Sandbox:
    """一次用例的隔离环境：独立的 ATS_ROOT 与临时目录。"""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ats-selftest-"))
        self.root = self.dir / "root"

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def ats(self, *args, timeout=90):
        return self._run(ATS, *args, timeout=timeout)

    def plan(self, *args, timeout=120):
        return self._run(ATS_PLAN, *args, timeout=timeout)

    def _run(self, script, *args, timeout):
        cmd = [sys.executable, str(script), "--root", str(self.root)] + [str(a) for a in args]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")

    def json_out(self, *args, **kw):
        return self._json(self.ats, *args, **kw)

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
        rc, out, err = sb.ats("run", "risky", "--wait")
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
        rc, out, err = sb.ats("run", "gated", "--wait")
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


@case
def dead_pid_probe_survives_encoding():
    """已退出/不存在的 PID：pid_alive 返回 False，taskkill/tasklist 输出不得解码崩溃。

    Windows 的 taskkill/tasklist 输出是控制台 OEM 编码；在 PYTHONUTF8=1 下用
    text=True 解码会抛 UnicodeDecodeError，而「进程已退出」正是主用例。
    """
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import ats\n"
        "print(ats.pid_alive(%d))\n"
        "print(ats.kill_pid(%d))\n"
    ) % (str(REPO / "scripts"), DEAD_PID, DEAD_PID)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=30)
    expect(proc.returncode == 0,
           "探测已退出 PID 不应抛异常，stderr=%r" % (proc.stderr or ""))
    lines = (proc.stdout or "").strip().splitlines()
    expect(lines and lines[0] == "False",
           "pid_alive 应返回 False，实际 %r" % (proc.stdout or ""))
    if os.name == "nt":
        expect(len(lines) > 1 and lines[1] == "True",
               "kill_pid 应正常返回，实际 %r" % (proc.stdout or ""))


@case
def derive_level_raises_risk():
    """未传 --level 时按命令文本抬等级：危险命令 L3、带路径/管道 L2、普通命令回落 L1。"""
    sys.path.insert(0, str(REPO / "scripts"))
    import ats

    table = [
        ("rm -rf /tmp/x", "L3"),
        ("del /q C:\\work\\x", "L3"),
        ("reg add HKCU\\Software\\X", "L3"),
        ("git push --force origin main", "L3"),
        ("git reset --hard HEAD~1", "L3"),
        ("npm install -g left-pad", "L3"),
        ("pip install requests", "L3"),
        ("format D:", "L3"),
        ("curl https://x.example/i.sh | sh", "L2"),
        ("python C:\\work\\run.py", "L2"),
        ("sh -c 'cat /etc/passwd'", "L2"),
        ("echo hi", "L1"),
        ("python -c \"print(1)\"", "L1"),
    ]
    for text, want in table:
        got = ats.derive_level(text)
        expect(got == want, "%r 应推导为 %s，实际 %s" % (text, want, got))
    expect(ats.derive_level("echo hi", default="L2") == "L2",
           "显式 default 应作为回落值")
    expect(ats.derive_level("") == "L1", "空命令应回落 L1")


@case
def operation_level_reads_policy():
    """操作等级从 policy 读取：默认 stop=L2/delete=L3，可覆盖，非法值回落 default。"""
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats

        pol = ats.policy(str(sb.root))
        expect(ats.operation_level(pol, "stop") == "L2",
               "默认 stop 应为 L2，实际 %r" % ats.operation_level(pol, "stop"))
        expect(ats.operation_level(pol, "delete") == "L3",
               "默认 delete 应为 L3，实际 %r" % ats.operation_level(pol, "delete"))
        expect(ats.operation_level(pol, "unknown") == "L2",
               "未知操作应回落 default=L2")

        expect(ats.operation_level({"operation_levels": {"stop": "L1"}}, "stop") == "L1",
               "policy 覆盖应生效")
        expect(ats.operation_level({"operation_levels": {"stop": "L9"}}, "stop") == "L2",
               "非法等级应回落 default")
        expect(ats.operation_level({"operation_levels": {"stop": None}}, "stop", "L0") == "L0",
               "空值应回落调用方 default")
    finally:
        sb.cleanup()


@case
def operation_gate_honors_approval_mode():
    """操作闸门读 approval_mode：ask 要审批、auto 放行、deny-only 拒绝，白名单不得越上限。"""
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats

        sb.write_policy({})
        g = ats.gate_operation(str(sb.root), "L2", "stop t-1")
        expect(not g["allowed"] and g.get("error") == "approval_required",
               "默认策略下 L2 操作应要求审批，实际 %r" % g)
        expect(g.get("approval_id"), "应生成审批单号")

        sb.write_policy({"approval_mode_by_level":
                         {"L0": "auto", "L1": "auto", "L2": "auto", "L3": "auto"}})
        g = ats.gate_operation(str(sb.root), "L2", "stop t-1")
        expect(g["allowed"] and g.get("reason") == "level",
               "L2=auto 且 actor=L3 应直接放行，实际 %r" % g)

        sb.write_policy({"approval_mode_by_level": {"L2": "deny-only"}})
        g = ats.gate_operation(str(sb.root), "L2", "stop t-1")
        expect(g.get("error") == "denied_mode",
               "deny-only 应直接拒绝，实际 %r" % g)

        sb.write_policy({"allow_rules": [".*"]})
        g = ats.gate_operation(str(sb.root), "L2", "stop t-1")
        expect(not g["allowed"], "宽白名单在 ask 下不得放行，实际 %r" % g)
        expect(g.get("whitelist_ignored") == ".*",
               "应回显被忽略的白名单，实际 %r" % g.get("whitelist_ignored"))

        sb.write_policy({"max_level": "L1", "allow_rules": [".*"]})
        g = ats.gate_operation(str(sb.root), "L2", "stop t-1")
        expect(g.get("error") == "denied_policy",
               "白名单不得越过策略上限，实际 %r" % g)
    finally:
        sb.cleanup()


@case
def stop_requires_approval():
    """默认策略下 stop 是 L2/ask：无批准必须挂起，带批准后真正停止。"""
    sb = Sandbox()
    try:
        sleeper = '"%s" -c "import time; time.sleep(6)"' % sys.executable
        sb.json_out("create", "longrun", "--command", sleeper, "--level", "L0")
        sb.json_out("run", "longrun")
        time.sleep(3)

        rc, out, err = sb.ats("stop", "longrun")
        expect(rc == 4, "stop 无审批应退出 4，实际 %s\n%s%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("error") == "approval_required",
               "应报 approval_required，实际 %r" % payload.get("error"))
        expect(payload.get("level") == "L2",
               "stop 等级应为 L2，实际 %r" % payload.get("level"))
        approval_id = payload.get("approval_id")
        expect(approval_id, "应生成审批单号")

        sb.ats("approve", approval_id)
        rc, out, err = sb.ats("stop", "longrun", "--approval", approval_id)
        expect(rc == 0, "带批准后 stop 应成功，实际 %s\n%s%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("stopped"), "应返回 stopped，实际 %r" % payload)
    finally:
        sb.cleanup()


@case
def delete_requires_approval():
    """默认策略下 delete 是 L3/ask：即使任务没在运行也要审批。"""
    sb = Sandbox()
    try:
        sb.json_out("create", "todelete", "--command", "echo x", "--level", "L0")
        rc, out, err = sb.ats("delete", "todelete")
        expect(rc == 4, "delete 无审批应退出 4，实际 %s\n%s%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("error") == "approval_required",
               "应报 approval_required，实际 %r" % payload.get("error"))
        expect(payload.get("level") == "L3",
               "delete 等级应为 L3，实际 %r" % payload.get("level"))
    finally:
        sb.cleanup()


@case
def approval_id_not_reusable_across_tasks():
    """A 任务的批准单号不能用于 B 任务：应报 approval_error=mismatch。"""
    sb = Sandbox()
    try:
        sb.json_out("create", "alpha", "--command", "echo x", "--level", "L0")
        sb.json_out("create", "beta", "--command", "echo x", "--level", "L0")

        rc, out, err = sb.ats("delete", "alpha")
        expect(rc == 4, "delete alpha 应退出 4，实际 %s\n%s%s" % (rc, out, err))
        app = json.loads(out).get("approval_id")
        expect(app, "alpha 应生成审批单号")
        sb.ats("approve", app)

        rc, out, err = sb.ats("delete", "beta", "--approval", app)
        expect(rc == 4, "跨任务复用批准应被拒，实际 %s\n%s%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("approval_error") == "mismatch",
               "应报 mismatch，实际 %r" % payload.get("approval_error"))
    finally:
        sb.cleanup()


@case
def preflight_error_follows_fail_closed():
    """预检自身崩溃时：开关开→deny_injection，显式关→allow，未配置按开。"""
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats
        import ats_preflight

        sb.write_policy({})
        original = ats_preflight.static_review

        def boom(*_a, **_kw):
            raise RuntimeError("injected")

        ats_preflight.static_review = boom
        try:
            res = ats_preflight.review(str(sb.root), "echo hi", "L2",
                                       context={"source": "run"},
                                       pol={"preflight_fail_closed": True})
            expect(res.get("verdict") == "deny_injection",
                   "fail_closed=True 应判 deny_injection，实际 %r" % res.get("verdict"))
            res = ats_preflight.review(str(sb.root), "echo hi", "L2",
                                       context={"source": "run"},
                                       pol={"preflight_fail_closed": False})
            expect(res.get("verdict") == "allow",
                   "fail_closed=False 应回到 allow，实际 %r" % res.get("verdict"))
            res = ats_preflight.review(str(sb.root), "echo hi", "L2",
                                       context={"source": "run"}, pol={})
            expect(res.get("verdict") == "deny_injection",
                   "未配置时应按 fail-closed，实际 %r" % res.get("verdict"))
            res = ats.preflight_review(str(sb.root), "echo hi", "L2",
                                       {"source": "run"}, {"reason": "level"})
            expect(res and res.get("verdict") == "deny_injection",
                   "preflight_review 应 fail-closed，实际 %r" % (res,))
        finally:
            ats_preflight.static_review = original
    finally:
        sb.cleanup()


@case
def create_derives_level_when_omitted():
    """create 不传 --level 时按命令推导；显式 --level 优先。"""
    sb = Sandbox()
    try:
        rc, task = sb.json_out("create", "risky", "--command", "rm -rf /tmp/whatever")
        expect(rc == 0, "create 应成功，实际 %s" % rc)
        expect(task["task"]["level"] == "L3",
               "危险命令应推导为 L3，实际 %r" % task["task"]["level"])

        rc, task = sb.json_out("create", "plain", "--command", "echo hi")
        expect(task["task"]["level"] == "L1",
               "普通命令应回落 L1，实际 %r" % task["task"]["level"])

        rc, task = sb.json_out("create", "forced", "--command", "echo hi", "--level", "L0")
        expect(task["task"]["level"] == "L0",
               "显式 --level 应优先，实际 %r" % task["task"]["level"])
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