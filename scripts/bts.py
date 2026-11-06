#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""background-task-scheduler — Task 层执行器 (v2.7)

命令：
  create <id> --command "..." [--cwd DIR] [--level L0..L3] [--timeout SEC]
                [--env K=V ...] [--no-shell] [--spec FILE] [--force]
  run <id> [--approval AP] [--wait]
  download --url URL... | --file MANIFEST [--out DIR] [--name NAME]
                [--sha256 HEX] [--size N] [--header "K: V"] [--proxy URL]
                [--concurrency N] [--rate BPS] [--retry N] [--backoff SEC]
                [--no-resume] [--overwrite] [--insecure] [--level L] [--wait]
  torrent --magnet LINK | --torrent FILE [--out DIR] [--seed-time MIN]
                [--max-download BPS] [--max-upload BPS] [--level L] [--timeout SEC] [--wait]
  supervise <id>                 # 内部命令，由 run 派生，勿手动调用
  check [id]                     # 同步运行中任务状态（超时/退出/监督进程丢失）
  list [--status S] [--json]
  show <id> [--lines N]
  status <id> [--json]
  stop <id> [--approval AP] [--force]
  delete <id> [--approval AP]
  approve <id>... | --all [--reject] [--note TEXT]
  approval prune [--dry-run] [--older-than SEC]
  events [--lines N] [--json]
  logs [--lines N] [--plain] [--json]
  policy [--json]

退出码：0 正常 / 2 参数或确认缺失 / 3 不存在或状态冲突 / 4 权限不足

设计约束：
  * 所有状态落在 BTS_ROOT（默认 <cwd>/.bts），可用环境变量覆盖。
  * Task 层事件统一写 <root>/events.jsonl；Plan 层事件写
    <root>/schedules/<schedule_id>/events.jsonl（由 bts_plan.py 触发）。
  * 审批记录 <root>/approvals/<approval_id>.json，命令原文绑定 + 有效期。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------- 常量

LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
LEVELS = ("L0", "L1", "L2", "L3")

DEFAULT_MAX_LEVEL = "L3"
DEFAULT_ACTOR_LEVEL = "L3"
DEFAULT_TASK_LEVEL = "L1"
DEFAULT_APPROVAL_TTL = 86400          # 审批有效期（秒），<=0 表示不过期
KEEP_RUNS_DEFAULT = 5
LOG_ROTATE_BYTES = 2 * 1024 * 1024
STALL_SECONDS_DEFAULT = 300
DEFAULT_TIMEOUT = 0                   # 0 = 不限时

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_DENIED = 4

POLICY_DEFAULTS = {
    "max_level": DEFAULT_MAX_LEVEL,
    "actor_level": DEFAULT_ACTOR_LEVEL,
    "allow_rules": [],
    "approval_ttl_seconds": DEFAULT_APPROVAL_TTL,
    "keep_runs_per_plan": KEEP_RUNS_DEFAULT,
    "stall_seconds": STALL_SECONDS_DEFAULT,
}

POLICY_ENV = {
    "max_level": "BTS_MAX_LEVEL",
    "actor_level": "BTS_ACTOR_LEVEL",
    "allow_rules": "BTS_ALLOW_RULES",
    "approval_ttl_seconds": "BTS_APPROVAL_TTL",
    "keep_runs_per_plan": "BTS_KEEP_RUNS",
    "stall_seconds": "BTS_STALL",
}

INT_POLICY_KEYS = ("approval_ttl_seconds", "keep_runs_per_plan", "stall_seconds")


# ---------------------------------------------------------------- 基础 IO

def bts_root(root: str | None = None) -> Path:
    if root:
        return Path(root)
    env = os.environ.get("BTS_ROOT")
    if env:
        return Path(env)
    return Path.cwd() / ".bts"


def now_ts() -> int:
    return int(time.time())


def iso(ts: int | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_ts() if ts is None else ts))


def jread(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def jwrite(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def append_jsonl(path, record):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return p


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 策略与日志

def policy(root, data=None):
    """读取策略：默认值 < policy.json < 环境变量。

    返回 dict，键固定为 POLICY_DEFAULTS 的六个字段。
    """
    pol = dict(POLICY_DEFAULTS)
    pol["allow_rules"] = []
    stored = jread(Path(root) / "policy.json", {})
    if isinstance(stored, dict):
        for key in POLICY_DEFAULTS:
            if stored.get(key) is not None:
                pol[key] = stored[key]
    if isinstance(data, dict):
        for key in POLICY_DEFAULTS:
            if data.get(key) is not None:
                pol[key] = data[key]
    for key, env in POLICY_ENV.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        if key == "allow_rules":
            pol[key] = [x.strip() for x in raw.split("||") if x.strip()]
        elif key in INT_POLICY_KEYS:
            try:
                pol[key] = int(raw)
            except ValueError:
                pass
        else:
            pol[key] = str(raw)
    rules = pol.get("allow_rules")
    if isinstance(rules, str):
        rules = [x.strip() for x in rules.split(",") if x.strip()]
    pol["allow_rules"] = list(rules or [])
    for key in INT_POLICY_KEYS:
        try:
            pol[key] = int(pol[key])
        except (TypeError, ValueError):
            pol[key] = POLICY_DEFAULTS[key]
    for key in ("max_level", "actor_level"):
        pol[key] = str(pol[key]).upper()
        if pol[key] not in LEVEL_RANK:
            pol[key] = POLICY_DEFAULTS[key]
    return pol


def level_rank(level) -> int:
    return LEVEL_RANK.get(str(level).upper(), 0)


def lower_level(a, b) -> str:
    return a if level_rank(a) <= level_rank(b) else b


def whitelist_match(root, text):
    """返回命中的白名单规则；未命中返回 None。规则为正则不区分大小写。"""
    for rule in policy(root).get("allow_rules") or []:
        try:
            if re.search(rule, text or "", re.IGNORECASE):
                return rule
        except re.error:
            continue
    return None


def log_event(root, msg, level="info", **fields):
    """追加一条调度器日志到 <root>/scheduler.log（JSONL），超过阈值自动轮转。"""
    path = Path(root) / "scheduler.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            rotated = path.with_name(path.name + ".1")
            try:
                os.replace(path, rotated)
            except OSError:
                pass
    except OSError:
        pass
    record = {"ts": now_ts(), "iso": iso(), "level": level, "msg": msg}
    record.update({k: v for k, v in fields.items() if v is not None})
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return record


def append_event_line(path, record):
    append_jsonl(path, record)
    return record


def event_stream(root) -> Path:
    return Path(root) / "events.jsonl"


def emit_event(root, record):
    """Task 层事件投递到全局事件流。"""
    payload = {"ts": now_ts(), "iso": iso()}
    payload.update(record)
    return append_event_line(event_stream(root), payload)


def schedule_event_stream(root, schedule_id) -> Path:
    return Path(root) / "schedules" / str(schedule_id) / "events.jsonl"


def announce_approval(root, record):
    """审批创建事件投递：带 schedule_id 的走 Plan 层事件流，否则走全局。"""
    payload = {
        "ts": now_ts(),
        "iso": iso(),
        "type": "approval_created",
        "approval_id": record.get("id"),
        "required_level": record.get("required_level"),
        "source": record.get("source"),
        "task_id": record.get("task_id"),
        "schedule_id": record.get("schedule_id"),
        "plan_id": record.get("plan_id"),
        "stage_id": record.get("stage_id"),
        "step_id": record.get("step_id"),
        "expires_at": record.get("expires_at"),
        "resume": record.get("resume"),
    }
    sid = record.get("schedule_id")
    target = schedule_event_stream(root, sid) if sid else event_stream(root)
    return append_event_line(target, payload)


# ---------------------------------------------------------------- 审批

def approvals_dir(root) -> Path:
    return Path(root) / "approvals"


def new_approval_id() -> str:
    return "ap-" + uuid.uuid4().hex[:8]


def load_approval(root, approval_id):
    if not approval_id:
        return None
    return jread(approvals_dir(root) / ("%s.json" % approval_id))


def save_approval(root, record):
    return jwrite(approvals_dir(root) / ("%s.json" % record["id"]), record)


def approval_expired(record) -> bool:
    if not record:
        return False
    exp = record.get("expires_at")
    if not exp:
        return False
    try:
        return now_ts() >= int(exp)
    except (TypeError, ValueError):
        return False


def create_approval(root, command, required_level, context=None, ttl=None, resume=None):
    context = dict(context or {})
    pol = policy(root)
    if ttl is None:
        ttl = int(pol.get("approval_ttl_seconds", DEFAULT_APPROVAL_TTL))
    created = now_ts()
    record = {
        "id": new_approval_id(),
        "command": command,
        "required_level": str(required_level).upper(),
        "status": "pending",
        "created_at": created,
        "created_iso": iso(created),
        "expires_at": (created + int(ttl)) if int(ttl) > 0 else None,
        "resolved_at": None,
        "resolved_iso": None,
        "resolved_by": None,
        "note": None,
        "source": context.get("source", "operation"),
        "schedule_id": context.get("schedule_id"),
        "plan_id": context.get("plan_id"),
        "stage_id": context.get("stage_id"),
        "step_id": context.get("step_id"),
        "task_id": context.get("task_id"),
        "resume": resume or context.get("resume"),
    }
    save_approval(root, record)
    log_event(root, "approval_created",
              approval_id=record["id"],
              required_level=record["required_level"],
              source=record["source"],
              schedule_id=record["schedule_id"],
              plan_id=record["plan_id"],
              stage_id=record["stage_id"],
              step_id=record["step_id"],
              task_id=record["task_id"])
    return record


def approval_check(root, approval_id, command):
    """返回 {"ok", "error", "record"}；error ∈ missing/pending/rejected/expired/mismatch。"""
    record = load_approval(root, approval_id)
    if not record:
        return {"ok": False, "error": "missing", "record": None}
    status = record.get("status")
    error = None
    if status == "rejected":
        error = "rejected"
    elif status == "expired":
        error = "expired"
    elif status == "approved" and approval_expired(record):
        error = "expired"
    elif status != "approved":
        error = "pending"
    elif record.get("command") != command:
        error = "mismatch"
    return {"ok": error is None, "error": error, "record": record}


def approval_allows(root, approval_id, command) -> bool:
    return approval_check(root, approval_id, command)["ok"]


def mark_approval_expired(root, record):
    if record.get("status") == "pending" and approval_expired(record):
        record["status"] = "expired"
        record["resolved_at"] = now_ts()
        record["resolved_iso"] = iso()
        record["resolved_by"] = "system"
        save_approval(root, record)
        return True
    return False


# ---------------------------------------------------------------- 权限门

def permission_gate(root, command, approval_id=None, context=None):
    """Task/Step 执行前的权限门。

    context 可含：required_level / task_id / schedule_id / plan_id / stage_id /
    step_id / source / whitelist_text。
    返回 {"allowed", "reason", "required_level", "actor_level", "policy_max",
          "effective_level", "whitelist", "approval_id", "approval_error", "approval"}。
    reason ∈ whitelist / level / approval / denied_policy / approval_required
    """
    context = dict(context or {})
    pol = policy(root)
    required = str(context.get("required_level") or DEFAULT_TASK_LEVEL).upper()
    actor = str(pol.get("actor_level") or DEFAULT_ACTOR_LEVEL).upper()
    cap = str(pol.get("max_level") or DEFAULT_MAX_LEVEL).upper()
    result = {
        "required_level": required,
        "actor_level": actor,
        "policy_max": cap,
        "effective_level": lower_level(required, lower_level(actor, cap)),
        "whitelist": None,
        "approval_id": approval_id,
        "approval_error": None,
        "approval": None,
        "allowed": False,
        "reason": None,
    }
    rule = whitelist_match(root, context.get("whitelist_text") or command)
    if rule:
        result.update(allowed=True, reason="whitelist", whitelist=rule)
        return result
    if level_rank(required) > level_rank(cap):
        result.update(allowed=False, reason="denied_policy")
        return result
    if level_rank(required) <= level_rank(actor):
        result.update(allowed=True, reason="level")
        return result
    check = approval_check(root, approval_id, command)
    result["approval"] = check["record"]
    result["approval_error"] = check["error"]
    if check["ok"]:
        result.update(allowed=True, reason="approval")
        return result
    result.update(allowed=False, reason="approval_required")
    return result


def gate_operation(root, level, label, approval_id=None, context=None):
    """操作型（stop/delete/cancel/prune/rerun）权限门。

    与 permission_gate 共用白名单/身份上限/策略上限逻辑；失败时自动创建审批并投递事件。
    """
    context = dict(context or {})
    context.setdefault("source", "operation")
    pol = policy(root)
    actor = str(pol.get("actor_level") or DEFAULT_ACTOR_LEVEL).upper()
    cap = str(pol.get("max_level") or DEFAULT_MAX_LEVEL).upper()
    level = str(level).upper()

    rule = whitelist_match(root, context.get("whitelist_text") or label)
    if rule:
        return {"allowed": True, "reason": "whitelist", "whitelist": rule, "label": label}
    if level_rank(level) > level_rank(cap):
        return {"allowed": False, "reason": "denied_policy", "error": "denied_policy",
                "label": label, "level": level}
    if level_rank(level) <= level_rank(actor):
        return {"allowed": True, "reason": "level", "label": label, "level": level}

    check = approval_check(root, approval_id, label)
    if check["ok"]:
        return {"allowed": True, "reason": "approval", "label": label,
                "approval": check["record"], "level": level}
    record = create_approval(root, label, level, context=context, resume=context.get("resume"))
    announce_approval(root, record)
    return {"allowed": False, "reason": "approval_required", "error": "approval_required",
            "label": label, "level": level, "approval_error": check["error"],
            "approval_id": record["id"], "approval": record}


# ---------------------------------------------------------------- Task 存储

def tasks_dir(root) -> Path:
    return Path(root) / "tasks"


def task_dir(root, task_id) -> Path:
    return tasks_dir(root) / str(task_id)


def load_task(root, task_id):
    return jread(task_dir(root, task_id) / "task.json")


def save_task(root, task):
    task["updated_at"] = now_ts()
    return jwrite(task_dir(root, task["id"]) / "task.json", task)


def new_task_id() -> str:
    return "t-" + uuid.uuid4().hex[:6]


def list_tasks(root):
    out = []
    base = tasks_dir(root)
    if not base.exists():
        return out
    for p in sorted(base.iterdir()):
        if p.is_dir():
            task = jread(p / "task.json")
            if task:
                out.append(task)
    return out


# ---------------------------------------------------------------- 运行锁

RUN_LOCK_TTL = 300                    # 秒；超过此龄且持有者已死的锁文件视为崩溃残留


def _create_lock_file(path):
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "ts": now_ts()}).encode("utf-8"))
    finally:
        os.close(fd)
    return str(path)


def acquire_run_lock(path, ttl=RUN_LOCK_TTL):
    """原子建锁文件，保证「判定 + 建锁」跨进程不可分割。返回 (lock_path, None) 或 (None, detail)。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in (1, 2):
        try:
            return _create_lock_file(p), None
        except FileExistsError:
            holder, age = {}, -1
            try:
                age = now_ts() - int(p.stat().st_mtime)
            except OSError:
                pass
            try:
                holder = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                holder = {}
            holder_pid = holder.get("pid")
            # 内容尚未写完（无 pid）时按存活处理，只交给 age 判定，避免误删刚建的锁
            alive = pid_alive(holder_pid) if holder_pid else True
            if not ((age > ttl) or (not alive)) or _attempt == 2:
                return None, {"reason": "run_lock_busy", "holder": holder, "age": age}
            try:
                p.unlink()                # 崩溃残留：清理后重试一次
            except OSError:
                return None, {"reason": "run_lock_busy", "holder": holder, "age": age}
    return None, {"reason": "run_lock_busy", "holder": {}, "age": -1}


def release_run_lock(path):
    if not path:
        return
    p = Path(path)
    try:
        holder = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        holder = {}
    if holder.get("pid") not in (None, os.getpid()):
        return                        # 已被他人接管，不动
    try:
        p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 进程工具

def kill_pid(pid):
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
        return True
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            return False
    return True


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/NH"],
                             capture_output=True, text=True)
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- Task 状态同步

def finalize_task(root, task, status, exit_code=None, timed_out=False, note=None):
    task["status"] = status
    task["exit_code"] = exit_code
    task["ended_at"] = now_ts()
    task["ended_iso"] = iso()
    if timed_out:
        task["timed_out"] = True
    if note:
        task["note"] = note
    task["finalized"] = True
    save_task(root, task)
    tdir = task_dir(root, task["id"])
    try:
        (tdir / "exit_code").write_text(str(exit_code if exit_code is not None else -1),
                                        encoding="utf-8")
    except OSError:
        pass
    emit_event(root, {
        "type": "task_end",
        "task_id": task["id"],
        "status": status,
        "exit_code": exit_code,
        "timed_out": bool(timed_out),
        "duration": (task.get("ended_at") or 0) - (task.get("started_at") or 0),
        "command": task.get("command"),
    })
    log_event(root, "task_end", task_id=task["id"], status=status,
              exit_code=exit_code, timed_out=bool(timed_out), note=note)
    return task


def refresh_task(root, task):
    """同步单个运行中任务；返回 (task, changed)。"""
    if not task:
        return task, False
    if task.get("status") not in ("starting", "running"):
        return task, False
    tdir = task_dir(root, task["id"])

    code_file = tdir / "exit_code"
    if code_file.exists() and not task.get("finalized"):
        try:
            code = int(code_file.read_text(encoding="utf-8").strip() or "-1")
        except ValueError:
            code = -1
        status = "succeeded" if code == 0 else "failed"
        finalize_task(root, task, status, code)
        return task, True

    timeout = int(task.get("timeout") or 0)
    started = task.get("started_at")
    if timeout > 0 and started and now_ts() - int(started) > timeout:
        kill_pid(task.get("pid"))
        kill_pid(task.get("supervisor_pid"))
        finalize_task(root, task, "timeout", -1, timed_out=True)
        return task, True

    sup = task.get("supervisor_pid")
    if not task.get("finalized") and sup and not pid_alive(sup):
        finalize_task(root, task, "failed", None,
                      note="supervisor_lost")
        return task, True
    return task, False


def refresh_all(root, only_id=None):
    changed = []
    tasks = list_tasks(root)
    if only_id:
        tasks = [t for t in tasks if t.get("id") == only_id]
    for task in tasks:
        task, did = refresh_task(root, task)
        if did:
            changed.append(task)
    return changed


# ---------------------------------------------------------------- 命令实现

def cmd_create(root, args):
    spec = {}
    if args.spec:
        spec_path = Path(args.spec)
        if not spec_path.exists():
            print(json.dumps({"error": "spec_not_found", "spec": str(spec_path)}, ensure_ascii=False))
            return EXIT_USAGE
        spec = jread(spec_path, {}) or {}
        if not isinstance(spec, dict):
            print(json.dumps({"error": "spec_invalid"}, ensure_ascii=False))
            return EXIT_USAGE

    task_id = args.id or spec.get("id") or new_task_id()
    tdir = task_dir(root, task_id)
    if (tdir / "task.json").exists() and not args.force:
        print(json.dumps({"error": "task_exists", "task_id": task_id}, ensure_ascii=False))
        return EXIT_CONFLICT

    command = args.command or spec.get("command")
    if isinstance(command, list):
        command = " ".join(str(x) for x in command)
    if not command:
        print(json.dumps({"error": "missing_command"}, ensure_ascii=False))
        return EXIT_USAGE

    command, post_hook = split_post_hook(command)
    if post_hook and post_hook.get("error"):
        print(json.dumps({"error": "bad_hook",
                          "hint": "钩子写法：<命令> @enc <转换器参数>；@enc 前后都要有内容"},
                         ensure_ascii=False))
        return EXIT_USAGE

    env = dict(spec.get("env") or {})
    for item in args.env or []:
        if "=" not in item:
            print(json.dumps({"error": "bad_env", "value": item}, ensure_ascii=False))
            return EXIT_USAGE
        key, value = item.split("=", 1)
        env[key] = value

    level = str(args.level or spec.get("level") or DEFAULT_TASK_LEVEL).upper()
    if level not in LEVEL_RANK:
        print(json.dumps({"error": "bad_level", "level": level}, ensure_ascii=False))
        return EXIT_USAGE

    timeout = args.timeout if args.timeout is not None else spec.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = int(timeout or 0)
    except (TypeError, ValueError):
        print(json.dumps({"error": "bad_timeout"}, ensure_ascii=False))
        return EXIT_USAGE

    shell = spec.get("shell", True)
    if args.no_shell:
        shell = False

    cwd = args.cwd or spec.get("cwd")
    if cwd:
        cwd = str(Path(cwd).expanduser())
        if not Path(cwd).exists():
            print(json.dumps({"error": "bad_cwd", "cwd": cwd}, ensure_ascii=False))
            return EXIT_USAGE

    now = now_ts()
    task = {
        "id": task_id,
        "command": command,
        "cwd": cwd,
        "level": level,
        "timeout": timeout,
        "env": env,
        "shell": bool(shell),
        "description": spec.get("description") or args.description,
        "tags": list(spec.get("tags") or []),
        "status": "pending",
        "created_at": now,
        "created_iso": iso(now),
        "updated_at": now,
        "started_at": None,
        "ended_at": None,
        "pid": None,
        "supervisor_pid": None,
        "exit_code": None,
        "approval_id": None,
        "finalized": False,
        **({"post_hook": post_hook} if post_hook else {}),
    }
    tdir.mkdir(parents=True, exist_ok=True)
    save_task(root, task)
    emit_event(root, {"type": "task_created", "task_id": task_id, "level": level,
                      "command": command})
    log_event(root, "task_created", task_id=task_id, level=level, command=command)
    return print_json({"task": task, "created": True})


def split_pack(text):
    """按未转义的英文逗号切分指令包；支持 \\, 与 \\\\ 转义。"""
    items, buf, i = [], [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in (",", "\\"):
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == ",":
            items.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    items.append("".join(buf))
    return [s.strip() for s in items]


# ---------------------------------------------------------------- @enc 后缀钩子

HOOK_PATTERN = re.compile(r"(?:^|\s)@enc(?=\s|$)")


def split_post_hook(command):
    """拆出命令末尾的 @enc 钩子：'<主命令> @enc <转换器参数>'。

    返回 (主命令, {"type": "enc", "args": ...})；写法不完整时返回 (原命令, {"error": "bad_hook"})。
    """
    if not command:
        return command, None
    m = HOOK_PATTERN.search(command)
    if not m:
        return command, None
    main = command[:m.start()].strip()
    args = command[m.end():].strip()
    if not main or not args:
        return command, {"error": "bad_hook"}
    return main, {"type": "enc", "args": args}


def resolve_encoder(task):
    """定位 encoding_converter.py：BTS_ENCODER_PATH > <cwd>/tools > 当前目录/tools。"""
    env_path = os.environ.get("BTS_ENCODER_PATH")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    bases = [Path(task["cwd"])] if task.get("cwd") else []
    bases.append(Path.cwd())
    for base in bases:
        cand = base / "tools" / "encoding_converter.py"
        if cand.is_file():
            return cand
    return None


def run_post_hook(root, tdir, task, hook):
    """主命令成功后的 @enc 转换步骤；结果写 hook-enc.log 并发事件。"""
    started = now_ts()
    log_path = tdir / "hook-enc.log"
    encoder = resolve_encoder(task)
    result = {"type": "enc", "args": hook.get("args"), "status": "failed",
              "exit_code": None, "log": str(log_path), "started_at": started}

    if encoder is None:
        result["error_type"] = "encoder_not_found"
        try:
            log_path.write_text(
                "encoder_not_found: 未找到 tools/encoding_converter.py；"
                "可用环境变量 BTS_ENCODER_PATH 指定转换器路径\n", encoding="utf-8")
        except OSError:
            pass
    else:
        cmdline = '"%s" "%s" %s' % (sys.executable, encoder, hook["args"])
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (task.get("env") or {}).items()})
        timeout = int(task.get("timeout") or 0)
        try:
            code = None
            with log_path.open("w", encoding="utf-8", errors="replace") as fh:
                proc = subprocess.Popen(cmdline, cwd=task.get("cwd") or None, env=env,
                                        stdin=subprocess.DEVNULL, shell=True,
                                        stdout=fh, stderr=subprocess.STDOUT)
                try:
                    code = proc.wait(timeout=timeout if timeout > 0 else None)
                except subprocess.TimeoutExpired:
                    kill_pid(proc.pid)
                    try:
                        code = proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        code = -1
                    result["error_type"] = "hook_timeout"
            result["exit_code"] = code
            result["status"] = "ok" if code == 0 else "failed"
        except OSError as e:
            result["error_type"] = "hook_spawn_failed"
            result["message"] = str(e)

    result["ended_at"] = now_ts()
    emit_event(root, {"type": "task_post_hook", "task_id": task["id"], "hook": "enc",
                      "status": result["status"], "exit_code": result.get("exit_code"),
                      "error_type": result.get("error_type")})
    log_event(root, "task_post_hook", task_id=task["id"], hook="enc",
              status=result["status"], exit_code=result.get("exit_code"),
              error_type=result.get("error_type"), log=str(log_path))
    return result


def cmd_pack(root, args):
    """把逗号分隔的多条命令一次性登记为独立 Task，只写文本，不执行。"""
    raw = args.commands or ""
    if not raw.strip():
        return print_json({"error": "pack_empty", "hint": "指令包内容不能为空"}, EXIT_USAGE)
    items = split_pack(raw)
    empty = [i + 1 for i, s in enumerate(items) if not s]
    if empty:
        return print_json({"error": "pack_empty_item", "indexes": empty,
                           "hint": "逗号之间必须有命令；命令内部的逗号写成 \\, "}, EXIT_USAGE)

    parsed, bad_hook = [], []
    for index, item in enumerate(items, 1):
        command, hook = split_post_hook(item)
        if hook and hook.get("error"):
            bad_hook.append(index)
            continue
        parsed.append((command, hook))
    if bad_hook:
        return print_json({"error": "bad_hook_item", "indexes": bad_hook,
                           "hint": "钩子写法：<命令> @enc <转换器参数>；@enc 前后都要有内容"},
                          EXIT_USAGE)

    level = str(args.level or DEFAULT_TASK_LEVEL).upper()
    if level not in LEVEL_RANK:
        return print_json({"error": "bad_level", "level": level}, EXIT_USAGE)
    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT
    try:
        timeout = int(timeout or 0)
    except (TypeError, ValueError):
        return print_json({"error": "bad_timeout"}, EXIT_USAGE)
    cwd = args.cwd
    if cwd:
        cwd = str(Path(cwd).expanduser())
        if not Path(cwd).exists():
            return print_json({"error": "bad_cwd", "cwd": cwd}, EXIT_USAGE)

    pack_id = args.pack_id or ("pk-" + uuid.uuid4().hex[:6])
    total = len(items)
    results = []
    now = now_ts()
    for index, (command, hook) in enumerate(parsed, 1):
        task_id = new_task_id()
        task = {
            "id": task_id,
            "kind": "pack",
            "command": command,
            "cwd": cwd,
            "level": level,
            "timeout": timeout,
            "env": {},
            "shell": True,
            "description": args.description or ("pack %s %d/%d" % (pack_id, index, total)),
            "tags": ["pack", pack_id],
            "pack": {"pack_id": pack_id, "index": index, "total": total},
            "status": "pending",
            "created_at": now,
            "created_iso": iso(now),
            "updated_at": now,
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "supervisor_pid": None,
            "exit_code": None,
            "approval_id": None,
            "finalized": False,
            **({"post_hook": hook} if hook else {}),
        }
        task_dir(root, task_id).mkdir(parents=True, exist_ok=True)
        save_task(root, task)
        emit_event(root, {"type": "task_created", "task_id": task_id, "level": level,
                          "command": command, "pack_id": pack_id,
                          "pack_index": index, "pack_total": total})
        results.append({"id": task_id, "index": index, "command": command,
                        "level": level, "status": "pending"})
    log_event(root, "pack_loaded", pack_id=pack_id, count=total, level=level)
    return print_json({"pack_id": pack_id, "count": total, "executed": False,
                       "note": "指令包只登记命令文本，未启动任何任务",
                       "tasks": results})


def cmd_run(root, args):
    payload, code = _start_task(root, args.task_id, args.approval, args.wait, args.force)
    return print_json(payload, code)


def _start_task(root, task_id, approval=None, wait=False, force=False):
    tdir = task_dir(root, task_id)
    lock_path, lock_err = acquire_run_lock(tdir / "run.lock")
    if lock_path is None:
        # 并发到达时只有抢到锁的进程能推进状态，其余直接拒绝，避免重复拉起 supervisor
        log_event(root, "task_start_rejected", task_id=task_id, reason="run_lock_busy",
                  holder=lock_err.get("holder"), age=lock_err.get("age"))
        return {"error": "state_conflict", "task_id": task_id,
                "reason": "run_lock_busy", "lock": lock_err}, EXIT_CONFLICT
    try:
        task = load_task(root, task_id)          # 锁内重读，不用锁外的旧快照
        if not task:
            return {"error": "not_found", "task_id": task_id}, EXIT_CONFLICT
        refresh_task(root, task)
        if task.get("status") not in ("pending", "failed", "timeout", "succeeded", "killed"):
            return {"error": "state_conflict", "task_id": task["id"],
                    "status": task.get("status")}, EXIT_CONFLICT
        if task.get("status") in ("failed", "timeout", "killed", "succeeded") and not force:
            return {"error": "rerun_requires_force", "task_id": task["id"],
                    "status": task.get("status")}, EXIT_CONFLICT

        gate = permission_gate(root, task["command"], approval,
                               {"required_level": task.get("level") or DEFAULT_TASK_LEVEL,
                                "source": "run", "task_id": task["id"]})
        if not gate["allowed"]:
            payload = {"error": "approval_required" if gate["reason"] == "approval_required" else gate["reason"],
                       "task_id": task["id"], "level": gate["required_level"],
                       "effective_level": gate["effective_level"],
                       "actor_level": gate["actor_level"],
                       "approval_id": None, "approval_error": gate["approval_error"]}
            if gate["reason"] == "approval_required":
                rec = create_approval(root, task["command"], gate["required_level"],
                                      {"source": "run", "task_id": task["id"]},
                                      resume=("python %s run %s --approval <approval_id>"
                                              % (Path(__file__).name, task["id"])))
                announce_approval(root, rec)
                payload["approval_id"] = rec["id"]
                payload["approval"] = rec
                payload["resume"] = rec["resume"]
                log_event(root, "task_blocked", task_id=task["id"], reason="approval_required",
                          approval_id=rec["id"])
            else:
                log_event(root, "task_blocked", task_id=task["id"], reason=gate["reason"])
            return payload, EXIT_DENIED

        (tdir / "stdout.log").parent.mkdir(parents=True, exist_ok=True)
        (tdir / "stdout.log").write_text("", encoding="utf-8")
        (tdir / "stderr.log").write_text("", encoding="utf-8")
        for stale_file in ("exit_code", "pid"):
            try:
                (tdir / stale_file).unlink()
            except OSError:
                pass

        task.update({
            "status": "starting",
            "started_at": now_ts(),
            "started_iso": iso(),
            "ended_at": None,
            "exit_code": None,
            "finalized": False,
            "approval_id": approval,
            "pid": None,
            "supervisor_pid": None,
        })
        save_task(root, task)
    finally:
        release_run_lock(lock_path)
    emit_event(root, {"type": "task_start", "task_id": task["id"],
                      "level": task.get("level"), "command": task["command"],
                      "approval_id": approval, "gate": gate["reason"]})
    log_event(root, "task_start", task_id=task["id"], level=task.get("level"),
              gate=gate["reason"], approval_id=approval)

    if wait:
        _supervise(root, task["id"])
        task = load_task(root, task["id"])
        return {"task": task, "waited": True}, EXIT_OK

    log_path = tdir / "supervisor.log"
    # 必须显式带 --root：子进程 cwd 也是 root，缺省会把 BTS_ROOT 解析成 <root>/.bts
    sup_argv = [sys.executable, str(Path(__file__).resolve()),
                "--root", str(root), "supervise", task["id"]]
    sup_pid, mode = _spawn_supervisor(sup_argv, root, log_path)
    task = load_task(root, task["id"])
    task["supervisor_pid"] = sup_pid
    task["detach_mode"] = mode
    save_task(root, task)
    return {"task_id": task["id"], "status": task.get("status"),
            "supervisor_pid": sup_pid, "detach_mode": mode, "detached": True}, EXIT_OK


def _spawn_supervisor(argv, root, log_path):
    """启动脱离式 supervisor，返回 (pid, mode)。

    Windows 宿主终端常用 Job Object（KILL_ON_JOB_CLOSE）包裹子进程：
    - CREATE_BREAKAWAY_FROM_JOB 在允许 breakaway 的 Job 里直接脱离（正常终端）
    - 被拒（WinError 5）说明 Job 不允许 breakaway，普通 detached 进程会在发起命令
      结束时整棵被回收；此时改用 WMI Win32_Process.Create，由 WmiPrvSE 创建进程，
      天然在该 Job 之外
    """
    if os.name != "nt":
        with log_path.open("a", encoding="utf-8") as fh:
            proc = subprocess.Popen(argv, cwd=str(root), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=fh, close_fds=True)
        return proc.pid, "detached"

    detached = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with log_path.open("a", encoding="utf-8") as fh:
        try:
            proc = subprocess.Popen(argv, cwd=str(root), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=fh,
                                    creationflags=detached | 0x01000000, close_fds=True)
            return proc.pid, "breakaway"
        except OSError:
            wmi_pid = _spawn_via_wmi(list(argv) + ["--log", str(log_path)], root)
            if wmi_pid:
                return wmi_pid, "wmi"
            proc = subprocess.Popen(argv, cwd=str(root), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=fh,
                                    creationflags=detached, close_fds=True)
            return proc.pid, "detached"


def _spawn_via_wmi(argv, cwd):
    """用 WMI Win32_Process.Create 创建进程并返回 pid；失败返回 None。

    命令行走环境变量传入，避免把用户路径拼进 PowerShell 字符串。
    """
    env = os.environ.copy()
    env["BTS_SPAWN_CMD"] = subprocess.list2cmdline([str(x) for x in argv])
    env["BTS_SPAWN_CWD"] = str(cwd)
    script = (
        "$ErrorActionPreference='Stop';"
        "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine=$env:BTS_SPAWN_CMD;CurrentDirectory=$env:BTS_SPAWN_CWD};"
        "if($r.ReturnValue -eq 0){Write-Output ('BTS_PID=' + $r.ProcessId)}"
        "else{Write-Output ('BTS_RC=' + $r.ReturnValue)}"
    )
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=30, env=env,
                              creationflags=0x08000000)   # CREATE_NO_WINDOW
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r"BTS_PID=(\d+)", done.stdout or "")
    return int(found.group(1)) if found else None


def _supervise(root, task_id):
    task = load_task(root, task_id)
    if not task:
        return None
    tdir = task_dir(root, task_id)
    command = task["command"]
    shell = bool(task.get("shell"))
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in (task.get("env") or {}).items()})
    cwd = task.get("cwd") or None
    timeout = int(task.get("timeout") or 0)

    argv = task.get("argv")
    with (tdir / "stdout.log").open("a", encoding="utf-8", errors="replace") as out, \
            (tdir / "stderr.log").open("a", encoding="utf-8", errors="replace") as err:
        if argv:
            # 原生 argv 任务（download）：不经过 shell，避免 Windows 路径引号问题
            proc = subprocess.Popen([str(x) for x in argv], cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        else:
            proc = subprocess.Popen(command, cwd=cwd, env=env, shell=shell,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        task = load_task(root, task_id) or task
        task["pid"] = proc.pid
        task["supervisor_pid"] = os.getpid()
        task["status"] = "running"
        task["started_at"] = task.get("started_at") or now_ts()
        save_task(root, task)
        try:
            (tdir / "pid").write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass
        timed_out = False
        code = None
        try:
            code = proc.wait(timeout=timeout if timeout > 0 else None)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_pid(proc.pid)
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = -1
        task = load_task(root, task_id) or task
        if task.get("finalized"):
            return task
        if timed_out:
            return finalize_task(root, task, "timeout", code, timed_out=True)
        status = "succeeded" if code == 0 else "failed"
        if status == "succeeded" and task.get("post_hook"):
            task["post_hook"] = run_post_hook(root, tdir, task, task["post_hook"])
            save_task(root, task)
            if task["post_hook"].get("status") != "ok":
                status = "failed"
                code = task["post_hook"].get("exit_code") or 1
        return finalize_task(root, task, status, code)


def cmd_supervise(root, args):
    if getattr(args, "log", None):
        # WMI 启动时没有可继承的 stdout 句柄，由 supervisor 自己接住输出
        try:
            sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", errors="replace")
        except OSError:
            pass
    task = _supervise(root, args.task_id)
    return print_json({"task": task}) if task else EXIT_CONFLICT


# ---------------------------------------------------------------- 下载器

DOWNLOAD_MAX_ITEMS = 500


def _parse_headers(values):
    headers = {}
    for raw in values or []:
        if ":" not in raw:
            return None, "bad_header"
        key, value = raw.split(":", 1)
        if not key.strip():
            return None, "bad_header"
        headers[key.strip()] = value.strip()
    return headers, None


def _load_manifest(path):
    """读取清单：.json（对象或数组）/ .txt（每行 URL[制表符 文件名]）/ .csv。

    返回 (outputs, items, error)。
    """
    p = Path(path)
    if not p.exists():
        return None, None, "manifest_not_found"
    suffix = p.suffix.lower()
    if suffix == ".json":
        data = jread(p)
        if isinstance(data, list):
            return None, data, None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return None, None, "manifest_invalid"
        return data.get("outputs"), data["items"], None
    rows = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if suffix == ".csv":
            cells = [c.strip().strip('"') for c in line.split(",")]
            if not cells or cells[0].lower() == "url":
                continue
            rows.append({"url": cells[0],
                         "save_as": cells[1] if len(cells) > 1 and cells[1] else None})
        else:
            parts = line.split("\t", 1)
            rows.append({"url": parts[0].strip(),
                         "save_as": parts[1].strip() if len(parts) > 1 else None})
    if not rows:
        return None, None, "manifest_empty"
    return None, rows, None


def _effective_name(item):
    """条目的最终文件名：显式 save_as，否则从 URL 末段推断（与 worker 的 guess_name 一致）。"""
    name = item.get("save_as")
    if name:
        return str(name)
    url = str(item["url"]).split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return url.rsplit("/", 1)[-1] or "download.bin"


def _dedupe_items(items):
    """去掉完全重复的请求；不同 URL 撞同名文件则报错，避免并发覆盖。

    返回 (items, duplicates, error)。
    """
    seen_keys = set()
    owners = {}
    kept = []
    duplicates = 0
    for item in items:
        name = _effective_name(item)
        key = (str(item["url"]), name)
        if key in seen_keys:
            duplicates += 1
            continue
        if name in owners and owners[name] != str(item["url"]):
            return None, 0, "name_conflict:%s" % name
        owners[name] = str(item["url"])
        seen_keys.add(key)
        kept.append(item)
    return kept, duplicates, None


def _build_download_spec(args):
    """把 CLI 参数与清单合并成 worker spec。返回 (spec, error)。"""
    if args.name and len(args.url or []) != 1:
        return None, "name_requires_single_url"
    outputs = args.out
    items = []
    if args.file:
        m_outputs, m_items, err = _load_manifest(args.file)
        if err:
            return None, err
        outputs = outputs or m_outputs
        for row in m_items:
            if not isinstance(row, dict) or not row.get("url"):
                return None, "item_missing_url"
            item = {"url": str(row["url"]), "save_as": row.get("save_as") or None}
            for key in ("sha256", "size", "headers"):
                if row.get(key):
                    item[key] = row[key]
            items.append(item)
    for url in args.url or []:
        items.append({"url": url, "save_as": args.name})
    if not items:
        return None, "missing_url"
    for item in items:
        if not str(item["url"]).lower().startswith(("http://", "https://")):
            return None, "bad_url"
    items, duplicates, err = _dedupe_items(items)
    if err:
        return None, err
    if len(items) > DOWNLOAD_MAX_ITEMS:
        return None, "too_many_items"
    single = len(items) == 1
    if (args.sha256 or args.size) and not single:
        return None, "digest_requires_single_url"
    if single:
        if args.sha256:
            items[0]["sha256"] = args.sha256.lower()
        if args.size:
            items[0]["size"] = int(args.size)
    headers, err = _parse_headers(args.header)
    if err:
        return None, err
    outputs = str(Path(outputs or ".").expanduser().resolve())
    spec = {
        "outputs": outputs,
        "items": items,
        "deduped": duplicates,
        "concurrency": max(1, int(args.concurrency or 1)),
        "rate_limit": max(0, int(args.rate or 0)),
        "retry": max(0, int(args.retry or 0)),
        "retry_backoff": max(0, int(args.backoff or 0)),
        "io_timeout": max(1, int(args.io_timeout or 30)),
        "resume": not args.no_resume,
        "overwrite": bool(args.overwrite),
        "insecure": bool(args.insecure),
        "verify_sha256": True,
        "verify_size": True,
        "headers": headers or {},
        "proxy": args.proxy,
    }
    return spec, None


def cmd_download(root, args):
    """创建下载任务并立刻交给 run 链路（权限/审批/状态/日志全部复用）。"""
    spec, err = _build_download_spec(args)
    if err:
        return print_json({"error": err}, EXIT_USAGE)

    task_id = args.id or ("dl-" + uuid.uuid4().hex[:6])
    tdir = task_dir(root, task_id)
    if (tdir / "task.json").exists():
        return print_json({"error": "task_exists", "task_id": task_id}, EXIT_CONFLICT)
    tdir.mkdir(parents=True, exist_ok=True)
    spec_path = tdir / "download.json"
    jwrite(spec_path, spec)

    level = str(args.level or DEFAULT_TASK_LEVEL).upper()
    command = "download %d item(s) -> %s" % (len(spec["items"]), spec["outputs"])
    now = now_ts()
    task = {
        "id": task_id,
        "kind": "download",
        "command": command,
        "cwd": None,
        "level": level,
        "timeout": int(args.timeout or 0),
        "env": {},
        "shell": False,
        "description": args.description,
        "tags": ["download"],
        "status": "pending",
        "created_at": now,
        "created_iso": iso(now),
        "updated_at": now,
        "started_at": None,
        "ended_at": None,
        "pid": None,
        "supervisor_pid": None,
        "exit_code": None,
        "approval_id": None,
        "finalized": False,
        "argv": [sys.executable,
                 str(Path(__file__).resolve().parent / "bts_download.py"),
                 "--spec", str(spec_path)],
        "download": {"spec": str(spec_path), "outputs": spec["outputs"],
                     "items": len(spec["items"]),
                     "concurrency": spec["concurrency"],
                     "rate_limit": spec["rate_limit"], "retry": spec["retry"]},
    }
    save_task(root, task)
    emit_event(root, {"type": "task_created", "task_id": task_id, "level": level,
                      "command": command, "kind": "download",
                      "items": len(spec["items"])})
    log_event(root, "task_created", task_id=task_id, level=level,
              command=command, kind="download", items=len(spec["items"]))

    payload, code = _start_task(root, task_id, args.approval, args.wait, False)
    payload["download"] = {"items": len(spec["items"]), "outputs": spec["outputs"],
                           "deduped": spec.get("deduped", 0),
                           "spec": str(spec_path),
                           "log": relative_task_path(task_id, "stdout.log")}
    return print_json(payload, code)


# ---------------------------------------------------------------- 种子下载

def _torrent_label(source, out_dir):
    if source["type"] == "magnet":
        found = re.search(r"xt=urn:btih:([a-zA-Z0-9]+)", source["value"])
        ident = found.group(1)[:16] if found else "magnet"
    else:
        ident = Path(source["path"]).name
    return "torrent %s -> %s" % (ident, out_dir)


def _resolve_aria2():
    """复用 worker 的解析逻辑（PATH / 环境变量 / 常见安装目录）。"""
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from bts_torrent import find_aria2c
    except ImportError:
        return None
    return find_aria2c()


def cmd_torrent(root, args):
    """创建种子下载任务并立刻交给 run 链路（权限/审批/状态/日志全部复用）。"""
    out_dir = str(Path(args.out or ".").expanduser().resolve())
    if args.magnet:
        magnet = args.magnet.strip()
        if not magnet.startswith("magnet:"):
            return print_json({"error": "magnet_invalid",
                               "hint": "magnet 链接必须以 magnet: 开头"}, EXIT_USAGE)
        source = {"type": "magnet", "value": magnet}
    else:
        torrent = Path(args.torrent).expanduser().resolve()
        if not torrent.is_file():
            return print_json({"error": "torrent_not_found", "path": str(torrent)}, EXIT_USAGE)
        source = {"type": "torrent", "path": str(torrent)}

    aria2 = _resolve_aria2()
    if not aria2:
        return print_json({"error": "aria2_not_found",
                           "hint": "winget install --id aria2.aria2 -e，"
                                   "或设置 BTS_ARIA2 指向 aria2c.exe"}, EXIT_USAGE)

    task_id = args.id or ("tor-" + uuid.uuid4().hex[:6])
    tdir = task_dir(root, task_id)
    if (tdir / "task.json").exists():
        return print_json({"error": "task_exists", "task_id": task_id}, EXIT_CONFLICT)
    tdir.mkdir(parents=True, exist_ok=True)

    seed_time = max(0.0, float(args.seed_time or 0))
    spec = {
        "source": source,
        "outputs": out_dir,
        "seed_time": seed_time,
        "max_download": max(0, int(args.max_download or 0)),
        "max_upload": max(0, int(args.max_upload or 0)),
        "log": str(tdir / "aria2.log"),
        "aria2": aria2,
    }
    spec_path = tdir / "torrent.json"
    jwrite(spec_path, spec)

    level = str(args.level or DEFAULT_TASK_LEVEL).upper()
    command = _torrent_label(source, out_dir)
    now = now_ts()
    task = {
        "id": task_id,
        "kind": "torrent",
        "command": command,
        "cwd": None,
        "level": level,
        "timeout": int(args.timeout or 0),
        "env": {},
        "shell": False,
        "description": args.description,
        "tags": ["torrent"],
        "status": "pending",
        "created_at": now,
        "created_iso": iso(now),
        "updated_at": now,
        "started_at": None,
        "ended_at": None,
        "pid": None,
        "supervisor_pid": None,
        "exit_code": None,
        "approval_id": None,
        "finalized": False,
        "argv": [sys.executable,
                 str(Path(__file__).resolve().parent / "bts_torrent.py"),
                 "--spec", str(spec_path)],
        "torrent": {"spec": str(spec_path), "source": source["type"],
                    "outputs": out_dir, "seed_time": seed_time,
                    "max_download": spec["max_download"],
                    "max_upload": spec["max_upload"], "aria2": aria2},
    }
    save_task(root, task)
    emit_event(root, {"type": "task_created", "task_id": task_id, "level": level,
                      "command": command, "kind": "torrent",
                      "source": source["type"]})
    log_event(root, "task_created", task_id=task_id, level=level,
              command=command, kind="torrent", source=source["type"])

    payload, code = _start_task(root, task_id, args.approval, args.wait, False)
    payload["torrent"] = {"source": source["type"], "outputs": out_dir,
                          "seed_time": seed_time, "spec": str(spec_path),
                          "aria2": aria2,
                          "log": relative_task_path(task_id, "stdout.log")}
    return print_json(payload, code)


def relative_task_path(task_id, name):
    return "tasks/%s/%s" % (task_id, name)


def cmd_check(root, args):
    changed = refresh_all(root, args.task_id)
    tasks = list_tasks(root)
    if args.task_id:
        tasks = [t for t in tasks if t.get("id") == args.task_id]
        if not tasks:
            print(json.dumps({"error": "not_found", "task_id": args.task_id}, ensure_ascii=False))
            return EXIT_CONFLICT
        t = tasks[0]
        return print_json({"task": t, "changed": bool(changed),
                           "next_poll": 5 if t.get("status") in ("starting", "running") else 0})
    return print_json({
        "changed": [t["id"] for t in changed],
        "running": [t["id"] for t in tasks if t.get("status") in ("starting", "running")],
        "total": len(tasks),
        "next_poll": 5 if any(t.get("status") in ("starting", "running") for t in tasks) else 0,
    })


def cmd_list(root, args):
    tasks = list_tasks(root)
    if args.status:
        statuses = {s.strip() for s in args.status.split(",") if s.strip()}
        tasks = [t for t in tasks if t.get("status") in statuses]
    if args.pack:
        tasks = [t for t in tasks if (t.get("pack") or {}).get("pack_id") == args.pack]
    if args.json:
        return print_json({"tasks": tasks, "count": len(tasks)})
    if not tasks:
        print("no tasks")
        return EXIT_OK
    print("%-10s %-10s %-6s %-6s %s" % ("ID", "STATUS", "LEVEL", "EXIT", "COMMAND"))
    for t in tasks:
        cmd = (t.get("command") or "").replace("\n", " ")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        print("%-10s %-10s %-6s %-6s %s" % (t.get("id"), t.get("status"), t.get("level"),
                                            "-" if t.get("exit_code") is None else t.get("exit_code"),
                                            cmd))
    return EXIT_OK


def _tail(path, lines):
    p = Path(path)
    if not p.exists():
        return ""
    data = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:]) if lines > 0 else "\n".join(data)


def cmd_show(root, args):
    task = load_task(root, args.task_id)
    if not task:
        print(json.dumps({"error": "not_found", "task_id": args.task_id}, ensure_ascii=False))
        return EXIT_CONFLICT
    refresh_task(root, task)
    tdir = task_dir(root, task["id"])
    payload = dict(task)
    payload["stdout_tail"] = _tail(tdir / "stdout.log", args.lines)
    payload["stderr_tail"] = _tail(tdir / "stderr.log", args.lines)
    payload["files"] = {
        "stdout": str(tdir / "stdout.log"),
        "stderr": str(tdir / "stderr.log"),
        "exit_code": str(tdir / "exit_code"),
    }
    return print_json(payload)


def cmd_status(root, args):
    task = load_task(root, args.task_id)
    if not task:
        print(json.dumps({"error": "not_found", "task_id": args.task_id}, ensure_ascii=False))
        return EXIT_CONFLICT
    refresh_task(root, task)
    info = {"task_id": task["id"], "status": task.get("status"),
            "level": task.get("level"), "exit_code": task.get("exit_code"),
            "pid": task.get("pid"), "started_at": task.get("started_at"),
            "ended_at": task.get("ended_at"), "approval_id": task.get("approval_id")}
    if args.json:
        return print_json(info)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_stop(root, args):
    task = load_task(root, args.task_id)
    if not task:
        print(json.dumps({"error": "not_found", "task_id": args.task_id}, ensure_ascii=False))
        return EXIT_CONFLICT
    refresh_task(root, task)
    if task.get("status") not in ("starting", "running"):
        print(json.dumps({"error": "not_running", "task_id": task["id"],
                          "status": task.get("status")}, ensure_ascii=False))
        return EXIT_CONFLICT
    label = "stop %s" % task["id"]
    gate = gate_operation(root, "L1", label, args.approval,
                          {"task_id": task["id"], "source": "stop",
                           "resume": "python %s stop %s --approval <approval_id>"
                                     % (Path(__file__).name, task["id"])})
    if not gate["allowed"]:
        return print_json(gate, EXIT_DENIED)

    kill_pid(task.get("pid"))
    kill_pid(task.get("supervisor_pid"))
    finalize_task(root, task, "killed", task.get("exit_code"), note="stopped_by_user")
    log_event(root, "task_stop", task_id=task["id"], gate=gate.get("reason"))
    return print_json({"task": task, "stopped": True})


def cmd_delete(root, args):
    task = load_task(root, args.task_id)
    if not task:
        print(json.dumps({"error": "not_found", "task_id": args.task_id}, ensure_ascii=False))
        return EXIT_CONFLICT
    refresh_task(root, task)
    if task.get("status") in ("starting", "running"):
        print(json.dumps({"error": "state_conflict", "task_id": task["id"],
                          "status": task.get("status"),
                          "hint": "stop the task before deleting"}, ensure_ascii=False))
        return EXIT_CONFLICT
    label = "delete %s" % task["id"]
    gate = gate_operation(root, "L2", label, args.approval,
                          {"task_id": task["id"], "source": "delete",
                           "resume": "python %s delete %s --approval <approval_id>"
                                     % (Path(__file__).name, task["id"])})
    if not gate["allowed"]:
        return print_json(gate, EXIT_DENIED)
    tdir = task_dir(root, task["id"])
    for child in tdir.glob("*"):
        try:
            child.unlink()
        except OSError:
            pass
    try:
        tdir.rmdir()
    except OSError:
        pass
    log_event(root, "task_deleted", task_id=task["id"], gate=gate.get("reason"))
    return print_json({"task_id": task["id"], "deleted": True})


def cmd_approve(root, args):
    pol = policy(root)
    actor = str(pol.get("actor_level") or DEFAULT_ACTOR_LEVEL).upper()
    targets = []

    if args.all:
        base = approvals_dir(root)
        if not base.exists():
            print(json.dumps({"error": "no_approvals"}, ensure_ascii=False))
            return EXIT_CONFLICT
        for p in sorted(base.glob("*.json")):
            rec = jread(p, {})
            if rec.get("status") != "pending":
                continue
            if mark_approval_expired(root, rec):
                continue
            targets.append(rec["id"])
        if not targets:
            return print_json({"approvals": [], "count": 0, "events": [],
                               "note": "no pending approvals"})
    else:
        if not args.approval_id:
            print(json.dumps({"error": "missing_approval_id"}, ensure_ascii=False))
            return EXIT_USAGE
        targets = list(args.approval_id)

    results = []
    events = []
    for approval_id in targets:
        record = load_approval(root, approval_id)
        if not record:
            results.append({"approval_id": approval_id, "ok": False, "error": "missing"})
            continue
        if record.get("status") != "pending":
            results.append({"approval_id": approval_id, "ok": False,
                            "error": "already_%s" % record.get("status"),
                            "status": record.get("status")})
            continue
        if approval_expired(record):
            record["status"] = "expired"
            record["resolved_at"] = now_ts()
            record["resolved_iso"] = iso()
            record["resolved_by"] = "system"
            save_approval(root, record)
            log_event(root, "approval_expired", approval_id=approval_id,
                      required_level=record.get("required_level"),
                      schedule_id=record.get("schedule_id"), task_id=record.get("task_id"))
            results.append({"approval_id": approval_id, "ok": False, "error": "expired",
                            "expires_at": record.get("expires_at")})
            continue
        if level_rank(actor) < level_rank(record.get("required_level")):
            results.append({"approval_id": approval_id, "ok": False, "error": "insufficient_level",
                            "required_level": record.get("required_level"),
                            "actor_level": actor})
            continue

        status = "rejected" if args.reject else "approved"
        record["status"] = status
        record["resolved_at"] = now_ts()
        record["resolved_iso"] = iso()
        record["resolved_by"] = "actor:%s" % actor
        record["note"] = args.note
        save_approval(root, record)

        event = {
            "ts": now_ts(),
            "iso": iso(),
            "type": "approval_resolved",
            "approval_id": approval_id,
            "status": status,
            "required_level": record.get("required_level"),
            "source": record.get("source"),
            "task_id": record.get("task_id"),
            "schedule_id": record.get("schedule_id"),
            "plan_id": record.get("plan_id"),
            "stage_id": record.get("stage_id"),
            "step_id": record.get("step_id"),
            "resolved_by": record["resolved_by"],
            "note": record.get("note"),
            "resume": record.get("resume"),
        }
        sid = record.get("schedule_id")
        target = schedule_event_stream(root, sid) if sid else event_stream(root)
        append_event_line(target, event)
        events.append(event)
        log_event(root, "approval_resolved", approval_id=approval_id, status=status,
                  required_level=record.get("required_level"),
                  schedule_id=record.get("schedule_id"), task_id=record.get("task_id"),
                  resolved_by=record["resolved_by"])
        results.append({"approval_id": approval_id, "ok": True, "status": status,
                        "approval": record, "event": event})

    payload = {"approvals": results, "count": len(results),
               "events": events, "actor_level": actor}
    code = EXIT_OK
    if len(results) == 1:
        payload.update(results[0])
        payload["approvals"] = results
        if not results[0].get("ok") and not args.all:
            # 单条审批失败要给出可判别的退出码；--all 是批量语义，保持 0
            code = EXIT_DENIED if results[0].get("error") == "insufficient_level" \
                else EXIT_CONFLICT
    return print_json(payload, code)


def cmd_approval_prune(root, args):
    base = approvals_dir(root)
    payload = {"dry_run": bool(args.dry_run), "older_than": args.older_than,
               "removed": [], "would_remove": [], "kept_pending": 0, "kept_other": 0}
    if not base.exists():
        payload["note"] = "no approvals"
        return print_json(payload)
    cutoff = now_ts() - int(args.older_than) if args.older_than else None
    for p in sorted(base.glob("*.json")):
        rec = jread(p, {})
        status = rec.get("status")
        if status == "pending":
            payload["kept_pending"] += 1        # 待决审批永不清理
            continue
        if status not in ("approved", "rejected", "expired"):
            payload["kept_other"] += 1          # 未知状态不猜，保留
            continue
        if cutoff is not None:
            ts = int(rec.get("resolved_at") or rec.get("created_at") or 0)
            if ts and ts > cutoff:
                payload["kept_other"] += 1
                continue
        if args.dry_run:
            payload["would_remove"].append(str(p))
            continue
        try:
            p.unlink()
            payload["removed"].append(str(p))
        except OSError as exc:
            payload.setdefault("errors", []).append({"path": str(p), "error": str(exc)})
    if args.dry_run:
        payload["count"] = len(payload["would_remove"])
    else:
        payload["count"] = len(payload["removed"])
        if payload["removed"]:
            log_event(root, "approvals_pruned", removed=len(payload["removed"]),
                      older_than=args.older_than)
    return print_json(payload)


def cmd_events(root, args):
    path = event_stream(root)
    if not path.exists():
        return print_json({"events": [], "count": 0, "cursor": 0})
    lines = read_jsonl(path)
    state_path = Path(root) / "events_state.json"
    state = jread(state_path, {}) or {}
    cursor = int(state.get("cursor") or 0)
    if args.lines:
        chunk = lines[-args.lines:] if args.lines > 0 else lines
        return print_json({"events": chunk, "count": len(chunk), "cursor": len(lines),
                           "mode": "tail"})
    chunk = lines[cursor:]
    jwrite(state_path, {"cursor": len(lines), "updated_at": now_ts(), "iso": iso()})
    return print_json({"events": chunk, "count": len(chunk), "cursor": len(lines)})


def cmd_logs(root, args):
    path = Path(root) / "scheduler.log"
    if not path.exists():
        return print_json({"lines": [], "count": 0})
    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = raw[-args.lines:] if args.lines > 0 else raw
    if args.plain:
        for line in chunk:
            print(line)
        return EXIT_OK
    parsed = []
    for line in chunk:
        try:
            parsed.append(json.loads(line))
        except Exception:
            parsed.append({"raw": line})
    if args.json:
        return print_json({"lines": parsed, "count": len(parsed),
                           "rotated": (path.with_name(path.name + ".1")).exists()})
    for rec in parsed:
        if "raw" in rec:
            print(rec["raw"])
            continue
        extras = " ".join("%s=%s" % (k, v) for k, v in rec.items()
                          if k not in ("ts", "iso", "level", "msg") and v is not None)
        print("%s %-5s %-18s %s" % (rec.get("iso", ""), rec.get("level", ""),
                                    rec.get("msg", ""), extras))
    return EXIT_OK


def cmd_policy(root, args):
    pol = policy(root)
    if args.json:
        return print_json(pol)
    print(json.dumps(pol, ensure_ascii=False, indent=2))
    return EXIT_OK


# ---------------------------------------------------------------- 输出与 CLI

def print_json(data, code=EXIT_OK):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return code


def build_parser():
    ap = argparse.ArgumentParser(
        prog="bts.py",
        description="background-task-scheduler — Task 层执行器 (v2.7)",
    )
    ap.add_argument("--root", default=None, help="BTS_ROOT，默认 <cwd>/.bts")
    sub = ap.add_subparsers(dest="command_name")

    p = sub.add_parser("create", help="创建任务")
    p.add_argument("id", nargs="?", default=None)
    p.add_argument("--command", default=None)
    p.add_argument("--cwd", default=None)
    p.add_argument("--level", default=None, choices=list(LEVELS))
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--no-shell", action="store_true")
    p.add_argument("--spec", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("pack", help="加载指令包（多条命令一次性登记，不执行）")
    p.add_argument("commands", nargs="?", default=None,
                   help="英文逗号分隔的命令；命令内部的逗号写 \\,")
    p.add_argument("--pack-id", default=None, help="指令包标识，默认自动生成")
    p.add_argument("--cwd", default=None)
    p.add_argument("--level", default=None, choices=list(LEVELS))
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--description", default=None)

    p = sub.add_parser("run", help="启动任务")
    p.add_argument("task_id")
    p.add_argument("--approval", default=None)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("download", help="创建并启动下载任务")
    p.add_argument("--url", action="append", default=[], help="下载地址，可重复")
    p.add_argument("--file", default=None, help="清单文件（.json / .txt / .csv）")
    p.add_argument("--out", default=None, help="输出目录，默认当前目录")
    p.add_argument("--name", default=None, help="文件名（仅单 URL 可用）")
    p.add_argument("--sha256", default=None, help="期望的 SHA-256（仅单 URL 可用）")
    p.add_argument("--size", type=int, default=None, help="期望字节数（仅单 URL 可用）")
    p.add_argument("--header", action="append", default=[], help="请求头 'Key: Value'")
    p.add_argument("--proxy", default=None)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--rate", type=int, default=0, help="全局限速（字节/秒，0 不限）")
    p.add_argument("--retry", type=int, default=2, help="单个条目失败后的重试次数")
    p.add_argument("--backoff", type=int, default=3, help="重试退避基数（秒）")
    p.add_argument("--io-timeout", type=int, default=30, help="单次网络读超时（秒）")
    p.add_argument("--level", default=None, choices=list(LEVELS))
    p.add_argument("--timeout", type=int, default=0, help="整个下载任务的上限秒数，0 不限")
    p.add_argument("--id", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--approval", default=None)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--no-resume", action="store_true", help="禁用断点续传（不写 .part）")
    p.add_argument("--overwrite", action="store_true", help="目标已存在也重新下载")
    p.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验")

    p = sub.add_parser("torrent", help="创建并启动种子下载任务（aria2c）")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--magnet", default=None, help="magnet 链接")
    src.add_argument("--torrent", default=None, help=".torrent 文件路径")
    p.add_argument("--out", default=None, help="输出目录，默认当前目录")
    p.add_argument("--seed-time", type=float, default=0,
                   help="做种时长（分钟，0 表示下完即停）")
    p.add_argument("--max-download", type=int, default=0, help="全局限速（字节/秒，0 不限）")
    p.add_argument("--max-upload", type=int, default=0, help="全局上传限速（字节/秒，0 不限）")
    p.add_argument("--level", default=None, choices=list(LEVELS))
    p.add_argument("--timeout", type=int, default=0, help="整个任务的上限秒数，0 不限")
    p.add_argument("--id", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--approval", default=None)
    p.add_argument("--wait", action="store_true")

    p = sub.add_parser("supervise", help="内部：监督并等待子进程")
    p.add_argument("task_id")
    p.add_argument("--log", default=None, help="内部：WMI 启动时 supervisor 自身的日志文件")

    p = sub.add_parser("check", help="同步任务状态")
    p.add_argument("task_id", nargs="?", default=None)

    p = sub.add_parser("list", help="列出任务")
    p.add_argument("--status", default=None)
    p.add_argument("--pack", default=None, help="只列出指定指令包的任务")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="查看任务详情")
    p.add_argument("task_id")
    p.add_argument("--lines", type=int, default=20)

    p = sub.add_parser("status", help="查看任务状态")
    p.add_argument("task_id")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("stop", help="停止任务")
    p.add_argument("task_id")
    p.add_argument("--approval", default=None)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("delete", help="删除任务")
    p.add_argument("task_id")
    p.add_argument("--approval", default=None)

    p = sub.add_parser("approve", help="批准/拒绝审批（可批量）")
    p.add_argument("approval_id", nargs="*", default=[])
    p.add_argument("--all", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--note", default=None)

    p = sub.add_parser("approval", help="审批记录维护（prune 清理已决议记录）")
    p.add_argument("action", choices=["prune"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--older-than", type=int, default=0,
                   help="只清理早于 N 秒的已决议记录（按 resolved_at/created_at），0 不限")

    p = sub.add_parser("events", help="消费事件流")
    p.add_argument("--lines", type=int, default=0)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("logs", help="查看调度器日志")
    p.add_argument("--lines", type=int, default=20)
    p.add_argument("--plain", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("policy", help="查看有效策略")
    p.add_argument("--json", action="store_true")

    return ap


HANDLERS = {
    "create": cmd_create,
    "pack": cmd_pack,
    "run": cmd_run,
    "download": cmd_download,
    "torrent": cmd_torrent,
    "supervise": cmd_supervise,
    "check": cmd_check,
    "list": cmd_list,
    "show": cmd_show,
    "status": cmd_status,
    "stop": cmd_stop,
    "delete": cmd_delete,
    "approve": cmd_approve,
    "approval": cmd_approval_prune,
    "events": cmd_events,
    "logs": cmd_logs,
    "policy": cmd_policy,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command_name:
        parser.print_help()
        return EXIT_USAGE
    if args.command_name == "approve" and not args.all and not args.approval_id:
        print(json.dumps({"error": "missing_approval_id",
                          "hint": "使用 approve <id>... 或 --all"}, ensure_ascii=False))
        return EXIT_USAGE
    root = bts_root(args.root)
    handler = HANDLERS[args.command_name]
    return handler(root, args) or EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
