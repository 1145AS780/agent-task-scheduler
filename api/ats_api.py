#!/usr/bin/env python3
"""ATS 编排 API：把既有 CLI 组合成可直接调用的对象，不复制任何权限判断。

这一层存在的理由只有一个：**编排逻辑会频繁变，闸门逻辑不该跟着变**。
`ats.py` 位于 `scripts/` 下，被看门狗的 core_files() 全量摘要覆盖
（`SCRIPT_DIR.glob("*.py")`，不递归），改一行就要重新封签；
而轮询、等待、重试、批量这类编排代码是日常高频改动。
把编排搬到这里，`scripts/` 保持不动，封签基线就不会被日常开发反复作废。

本文件位于 `api/`，与 `scripts/` 平级，因此**不在哈希范围内**。这不是疏漏，
是设计：代价是它自身不受摘要保护，所以下面三条自我约束必须守住，
否则「不受保护」会从便利变成漏洞。

自我约束（改这个文件的人先读这三条）：
  1. 只以子进程调用 CLI，**绝不 import ats.py / ats_plan.py**。
     看门狗握手只发生在 ats.py 的 main() 里（全仓仅一处调用点）；
     `import ats` 然后直接调 handler 会绕过 verify、只读句柄、策略冻结，
     等于把闸门整个拆掉。
  2. 不内置口令、不缓存口令、不代批。`approve` 的口令每次由调用方传入，
     只经 stdin 交给 CLI（argv 会留在进程列表与 shell 历史里）。
  3. 不重实现任何权限判断。等级、审批、预检一律由 CLI 决定；
     本层只搬运结果。一旦有人把 gate 逻辑写进来，安全边界就被挪出了看门狗。

口令为什么绕不过：`approve` 第一句就是 require_passphrase，走
`ats_guard.py auth` 对密钥文件里的 PBKDF2 校验；而密钥文件里存的是
salt + hash，不是口令本身。所以一个不受摘要保护的 API 层，最坏也只能
「提交一个仍须人批的任务」，不能自己批、不能重封签。

用法：
    from ats_api import AtsClient
    c = AtsClient(root=r"D:\\repo\\.ats")
    r = c.create("python --version")
    print(c.run_and_wait(r.task_id, wait_approval=300).data)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

CONTRACT = 1

DEFAULT_TIMEOUT = 120
DEFAULT_INTERVAL = 2.0

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_DENIED = 4

EXIT_CLI_TIMEOUT = 124
EXIT_CLI_SPAWN = 125

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "killed"})
RUNNING_STATUSES = frozenset({"starting", "running"})

NEEDS_HUMAN_ERRORS = frozenset({"approval_required", "denied_injection"})

_UNSET = object()

_DEFAULT_TAIL_LINES = 200


@dataclass
class Result:
    """一次 CLI 调用的结果。ok 以退出码为准，不靠猜字段。"""

    action: str
    exit_code: int
    data: dict = field(default_factory=dict)
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK and not self.data.get("error")

    @property
    def error(self):
        return self.data.get("error")

    @property
    def needs_human(self) -> bool:
        return self.error in NEEDS_HUMAN_ERRORS

    @property
    def approval_id(self):
        return self.data.get("approval_id")

    @property
    def task(self) -> dict:
        task = self.data.get("task")
        return task if isinstance(task, dict) else {}

    @property
    def task_id(self):
        return self.data.get("task_id") or self.task.get("id")

    def raise_for_error(self):
        if not self.ok:
            raise AtsError(self)
        return self

    def as_dict(self) -> dict:
        return {"action": self.action, "ok": self.ok, "exit_code": self.exit_code,
                "error": self.error, "needs_human": self.needs_human,
                "approval_id": self.approval_id, "task_id": self.task_id,
                "data": self.data, "stderr": self.stderr}


class AtsError(Exception):
    """CLI 返回非 0 或带 error 字段时抛出；原始结果留在 .result 上。"""

    def __init__(self, result):
        self.result = result
        super().__init__("%s failed: exit=%s error=%s"
                         % (result.action, result.exit_code, result.error))

    @property
    def code(self):
        return self.result.error


class AtsClient:
    """ATS 命令行客户端。所有方法都是「组一次 CLI 调用」，没有第二套状态。"""

    def __init__(self, root=None, ats_dir=None, python=None,
                 timeout=DEFAULT_TIMEOUT, interval=DEFAULT_INTERVAL):
        base = Path(__file__).resolve().parents[1]
        self.ats_dir = Path(ats_dir) if ats_dir else base / "scripts"
        self.ats = self.ats_dir / "ats.py"
        self.plan = self.ats_dir / "ats_plan.py"
        self.root = str(root) if root else None
        self.python = python or sys.executable or "python"
        self.timeout = timeout
        self.interval = interval

    def available(self) -> dict:
        """只查文件是否存在，不跑任何命令；用于启动前的快速体检。"""
        missing = [str(p) for p in (self.ats, self.plan) if not p.is_file()]
        return {"ok": not missing, "contract": CONTRACT,
                "ats": str(self.ats), "plan": str(self.plan),
                "root": self.root, "missing": missing}

    def _env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _exec(self, script, argv, stdin_line=None, timeout=_UNSET):
        """跑一次 CLI，返回 (rc, stdout, stderr)。stdout 原样，不做解析。"""
        timeout = self.timeout if timeout is _UNSET else timeout
        cmd = [self.python, str(script)]
        if self.root:
            cmd += ["--root", self.root]
        cmd += [str(a) for a in argv]
        kwargs = {"capture_output": True, "env": self._env(), "timeout": timeout}
        try:
            if stdin_line is None:
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, **kwargs)
            else:
                proc = subprocess.run(cmd, input=(stdin_line + "\n").encode("utf-8"),
                                      **kwargs)
        except subprocess.TimeoutExpired as exc:
            return EXIT_CLI_TIMEOUT, _decode(exc.stdout), "CLI 超时（%ss）" % timeout
        except OSError as exc:
            return EXIT_CLI_SPAWN, "", "无法启动 CLI: %s" % exc
        return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)

    def _json(self, action, script, argv, stdin_line=None, timeout=_UNSET) -> Result:
        rc, out, err = self._exec(script, argv, stdin_line=stdin_line, timeout=timeout)
        out = out.strip()
        if not out:
            return Result(action, rc, {"error": "empty_output", "exit_code": rc},
                          err[-2000:])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return Result(action, rc, {"error": "invalid_json", "exit_code": rc,
                                       "stdout_tail": out[-2000:]}, err[-2000:])
        if not isinstance(data, dict):
            return Result(action, rc, {"error": "invalid_json_payload", "exit_code": rc},
                          err[-2000:])
        return Result(action, rc, data, err[-2000:])

    def create(self, command, task_id=None, cwd=None, level=None, timeout=None,
               description=None, env=None, no_shell=False, force=False) -> Result:
        argv = ["create"]
        if task_id:
            argv.append(task_id)
        argv += ["--command", command]
        if cwd:
            argv += ["--cwd", cwd]
        if level:
            argv += ["--level", level]
        if timeout is not None:
            argv += ["--timeout", str(int(timeout))]
        if description:
            argv += ["--description", description]
        for item in (env or []):
            argv += ["--env", item]
        if no_shell:
            argv.append("--no-shell")
        if force:
            argv.append("--force")
        return self._json("create", self.ats, argv)

    def run(self, task_id, approval=None, wait=False, force=False, cli_timeout=_UNSET) -> Result:
        """启动任务。wait=True 时 CLI 会阻塞到任务结束，故默认不限 CLI 超时。"""
        argv = ["run", task_id]
        if approval:
            argv += ["--approval", approval]
        if wait:
            argv.append("--wait")
        if force:
            argv.append("--force")
        if wait and cli_timeout is _UNSET:
            cli_timeout = None
        return self._json("run", self.ats, argv, timeout=cli_timeout)

    def stop(self, task_id, approval=None, force=False) -> Result:
        argv = ["stop", task_id]
        if approval:
            argv += ["--approval", approval]
        if force:
            argv.append("--force")
        return self._json("stop", self.ats, argv)

    def status(self, task_id) -> Result:
        return self._json("status", self.ats, ["status", task_id, "--json"])

    def check(self, task_id=None) -> Result:
        """同步并刷新任务状态；CLI 的 check 始终输出 JSON，无需 --json。"""
        argv = ["check"] + ([task_id] if task_id else [])
        return self._json("check", self.ats, argv)

    def list_tasks(self, status=None, pack=None) -> Result:
        argv = ["list", "--json"]
        if status:
            argv += ["--status", status]
        if pack:
            argv += ["--pack", pack]
        return self._json("list", self.ats, argv)

    def show(self, task_id, lines=20) -> Result:
        return self._json("show", self.ats,
                          ["show", task_id, "--lines", str(int(lines))])

    def logs(self, lines=20) -> Result:
        return self._json("logs", self.ats,
                          ["logs", "--json", "--lines", str(int(lines))])

    def events(self, lines=_DEFAULT_TAIL_LINES) -> Result:
        """只读事件流尾部。

        必须带 --lines：不带时 CLI 会推进全局 events_state.json 游标
        （cmd_events 的默认分支），那是 UI 与其他消费者共用的进度，
        API 轮询若走默认分支就会把别人的事件吃掉。
        """
        return self._json("events", self.ats,
                          ["events", "--json", "--lines", str(int(lines))])

    def approve(self, approval_id, passphrase, note=None, override=False) -> Result:
        """批准单条审批。口令每次传入，只经 stdin，不进 argv、不缓存。"""
        if not passphrase:
            return Result("approve", EXIT_USAGE,
                          {"error": "passphrase_required",
                           "hint": "口令不能为空；它只经 stdin 交给 CLI，不由本层保存"})
        argv = ["approve", approval_id, "--passphrase-stdin"]
        if note:
            argv += ["--note", note]
        if override:
            argv.append("--override")
        return self._json("approve", self.ats, argv, stdin_line=passphrase)

    def reject(self, approval_id, passphrase, note=None) -> Result:
        if not passphrase:
            return Result("reject", EXIT_USAGE,
                          {"error": "passphrase_required",
                           "hint": "口令不能为空；它只经 stdin 交给 CLI，不由本层保存"})
        argv = ["approve", approval_id, "--reject", "--passphrase-stdin"]
        if note:
            argv += ["--note", note]
        return self._json("reject", self.ats, argv, stdin_line=passphrase)

    def approve_all(self, passphrase, note=None) -> Result:
        if not passphrase:
            return Result("approve_all", EXIT_USAGE,
                          {"error": "passphrase_required",
                           "hint": "口令不能为空；它只经 stdin 交给 CLI，不由本层保存"})
        argv = ["approve", "--all", "--passphrase-stdin"]
        if note:
            argv += ["--note", note]
        return self._json("approve_all", self.ats, argv, stdin_line=passphrase)

    def wait_for_approval(self, approval_id, timeout=600, interval=None,
                          lines=_DEFAULT_TAIL_LINES) -> Result:
        """轮询事件流，等人把这条审批决掉。**本层绝不代批**。

        判据是 approval_resolved 事件里的 approval_id + status，
        这是 CLI 唯一可读的审批决议通道（approval 子命令只提供 prune）。
        """
        interval = self.interval if interval is None else interval
        deadline = None if timeout is None else time.monotonic() + timeout
        last_error = None
        while True:
            res = self.events(lines=lines)
            if not res.ok:
                last_error = res.error or "events_unavailable"
            for ev in reversed(res.data.get("events") or []):
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") != "approval_resolved":
                    continue
                if ev.get("approval_id") != approval_id:
                    continue
                status = ev.get("status")
                payload = {"approval_id": approval_id, "status": status,
                           "resolved_by": ev.get("resolved_by"),
                           "note": ev.get("note"), "resume": ev.get("resume"),
                           "task_id": ev.get("task_id"),
                           "approval": {"status": status,
                                        "resolved_by": ev.get("resolved_by"),
                                        "note": ev.get("note")}}
                code = EXIT_OK if status == "approved" else EXIT_DENIED
                if status != "approved":
                    payload["error"] = "approval_%s" % status
                    payload["hint"] = "审批未通过；按 resume 或 note 处理后重试"
                return Result("wait_for_approval", code, payload)
            if deadline is not None and time.monotonic() >= deadline:
                return Result("wait_for_approval", EXIT_CLI_TIMEOUT,
                              {"error": "approval_timeout", "approval_id": approval_id,
                               "timeout": timeout, "last_error": last_error,
                               "hint": "审批仍在待决；等人批准后再调 run(approval=<id>)"})
            time.sleep(interval)

    def wait_task(self, task_id, timeout=None, interval=None) -> Result:
        """轮询 check 直到任务进入终态。next_poll=0 时不空转。"""
        interval = self.interval if interval is None else interval
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            res = self.check(task_id)
            status = res.task.get("status")
            if is_terminal(status):
                return res
            if not res.ok and res.error not in ("empty_output", "invalid_json"):
                return res
            if deadline is not None and time.monotonic() >= deadline:
                payload = {"error": "task_timeout", "task_id": task_id,
                           "status": status, "timeout": timeout,
                           "hint": "任务仍在运行；用 status 或 show 继续观察"}
                return Result("wait_task", EXIT_CLI_TIMEOUT, payload, res.stderr)
            wait = interval
            try:
                next_poll = float(res.data.get("next_poll") or 0)
            except (TypeError, ValueError):
                next_poll = 0
            if next_poll > 0:
                wait = max(wait, min(next_poll, 5.0))
            time.sleep(wait)

    def run_and_wait(self, task_id, approval=None, wait_approval=None,
                     task_timeout=None, interval=None, force=False) -> Result:
        """启动并在一个调用里收敛：撞审批墙时按需等待人工批准，再续跑。

        wait_approval=None 表示不等人，直接返回 needs_human 载荷，
        由调用方决定下一步。给一个秒数则最多等这么久。
        本层始终不代批：批准要么来自 UI / CLI，要么来自显式 approve()。
        """
        interval = self.interval if interval is None else interval
        first = self.run(task_id, approval=approval, wait=False, force=force)
        if first.ok:
            return self.wait_task(task_id, timeout=task_timeout, interval=interval)
        if not first.needs_human or wait_approval is None:
            return first
        aid = first.approval_id
        if not aid:
            return first
        resolved = self.wait_for_approval(aid, timeout=wait_approval, interval=interval)
        if not resolved.ok:
            merged = dict(first.data)
            merged.update(resolved.data)
            merged["needs_human"] = True
            return Result("run_and_wait", resolved.exit_code, merged, first.stderr)
        resumed = self.run(task_id, approval=aid, wait=False)
        if not resumed.ok:
            return resumed
        return self.wait_task(task_id, timeout=task_timeout, interval=interval)

    def plan_call(self, *argv) -> Result:
        """ats_plan.py 的透传入口：argv 原样交给 CLI，本层不解释语义。

        透传而不是包装，是为了不在 api/ 里长出第二份 Plan 状态机；
        参数与返回结构以 `python scripts/ats_plan.py <action> --help` 为准。
        """
        if not argv:
            return Result("plan", EXIT_USAGE,
                          {"error": "missing_plan_action",
                           "hint": "至少给一个子命令，如 plan_call('list', '--json')"})
        return self._json("plan:" + str(argv[0]), self.plan, list(argv))


def _decode(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def is_terminal(status) -> bool:
    return status in TERMINAL_STATUSES


def is_running(status) -> bool:
    return status in RUNNING_STATUSES


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = None
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 < len(argv):
            root = argv[i + 1]
    client = AtsClient(root=root)
    payload = client.available()
    if payload["ok"]:
        listed = client.list_tasks()
        payload["tasks"] = listed.data.get("count")
        payload["list_error"] = listed.error
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK if payload["ok"] else EXIT_CLI_SPAWN


if __name__ == "__main__":
    sys.exit(main())
