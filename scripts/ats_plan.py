#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-task-scheduler — Plan / Schedule 层执行器 (v3.3.0-R0)

命令：
  submit_plan --spec plan.json [--id ID]
  submit_schedule --spec schedule.json [--id ID]
  run_plan <plan_id> [--approval AP] [--fail-fast]
  resume <schedule_id|plan_id> [--approval AP] [--fail-fast]
  check_schedule <schedule_id>
  pause <schedule_id> [--approval AP]
  cancel <schedule_id> [--approval AP]
  resume_schedule <schedule_id> [--approval AP]
  rerun_task <schedule_id> <stage_id> <step_id> [--approval AP]
  rerun_plan <plan_id> [--approval AP]
  list [--json]
  show <schedule_id|plan_id> [--json]
  delete <schedule_id|plan_id> [--approval AP]
  history <schedule_id> [--plan PLAN_ID] [--logs] [--lines N]
  prune [--keep N] [--dry-run]

Plan spec：
  {
    "id": "p1",
    "title": "...",
    "on_stage_failure": "continue" | "stop",
    "fail_on_critical": "continue" | "stop",
    "stages": [
      {"id": "prep", "mode": "serial" | "parallel",
       "steps": [
         {"id": "one", "command": "...", "cwd": null, "level": "L1",
          "timeout": 600, "depends_on": "zero",
          "retry": 1, "retry_backoff": 5, "idempotent": false, "critical": false}
       ]}
    ]
  }

Schedule spec：
  {"id": "p1", "every": 3600, "plan": "plan.json",
   "start_at": null, "max_runs": null, "on_stage_failure": "continue"}

退出码：0 正常 / 2 参数或 spec 非法 / 3 不存在或状态冲突 / 4 权限不足
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ats  # noqa: E402

try:                                    # 预演模块缺失时退化为纯权限门行为
    import ats_preflight  # noqa: E402
except Exception:                       # noqa: BLE001
    ats_preflight = None

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_DENIED = 4

TERMINAL_STEP = ("succeeded", "failed", "timeout", "skipped")
FAILED_STEP = ("failed", "timeout")
POLL_STALLED = 30
MAX_WAVES = 1000


# ---------------------------------------------------------------- 通用

def now_ts() -> int:
    return ats.now_ts()


def iso(ts=None) -> str:
    return ats.iso(ts)


def iso_run(ts):
    """next_run 的展示时间。

    0 与 None 都表示「已到期 / 可立即触发」，此时返回 None，
    避免把 epoch 0 格式化成 1970-01-01 这种看起来像「有个待跑任务」的假日期。
    """
    if ts is None:
        return None
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    return iso(value)


def schedules_dir(root) -> Path:
    return Path(root) / "schedules"


def schedule_dir(root, sid) -> Path:
    return schedules_dir(root) / str(sid)


def plan_dir(sdir, plan_id) -> Path:
    return Path(sdir) / "plans" / str(plan_id)


def stage_dir(pdir, stage_id) -> Path:
    return Path(pdir) / "stages" / str(stage_id)


def step_dir(pdir, stage_id, step_id) -> Path:
    return stage_dir(pdir, stage_id) / "steps" / str(step_id)


def all_plan_locations(root):
    """产出 (sdir, pdir)。"""
    base = schedules_dir(root)
    if not base.exists():
        return
    for sdir in sorted(base.iterdir()):
        pdirs = sdir / "plans"
        if not pdirs.exists():
            continue
        for pdir in sorted(pdirs.iterdir()):
            if (pdir / "plan.json").exists():
                yield sdir, pdir


def resolve_plan(root, ident):
    direct_sd = schedule_dir(root, ident)
    if (direct_sd / "schedule.json").exists():
        schedule = ats.jread(direct_sd / "schedule.json", {}) or {}
        pid = schedule.get("current_plan")
        if pid:
            pdir = plan_dir(direct_sd, pid)
            if (pdir / "plan.json").exists():
                return direct_sd, pdir
    for sdir, pdir in all_plan_locations(root):
        if pdir.name == ident:
            return sdir, pdir
    return None, None


def resolve_schedule(root, ident):
    sdir = schedule_dir(root, ident)
    if (sdir / "schedule.json").exists():
        return sdir
    return None


def emit(sdir, record):
    payload = {"ts": now_ts(), "iso": iso()}
    payload.update(record)
    ats.append_event_line(Path(sdir) / "events.jsonl", payload)
    return payload


def plan_log(root, plan, msg, **fields):
    kwargs = {"schedule_id": plan.get("schedule_id"),
              "plan_id": plan.get("id"),
              "stage_id": fields.pop("stage_id", None),
              "step_id": fields.pop("step_id", None)}
    kwargs.update(fields)
    return ats.log_event(root, msg, **kwargs)


# ---------------------------------------------------------------- 依赖与拓扑

def step_deps(step) -> list:
    dep = step.get("depends_on")
    if dep is None:
        return []
    if isinstance(dep, str):
        dep = [dep]
    if not isinstance(dep, list):
        return []
    return [str(x) for x in dep if x]


def find_step(stage, step_id):
    for step in stage.get("steps") or []:
        if step.get("id") == step_id:
            return step
    return None


def order_steps(steps) -> list:
    """Kahn 拓扑排序；返回长度小于有效 step 数说明存在环。无依赖时保持声明顺序。"""
    valid = [s for s in steps if isinstance(s, dict) and s.get("id")]
    ids = [s.get("id") for s in valid]
    by_id = {s.get("id"): s for s in valid}
    deps = {sid: [d for d in step_deps(by_id[sid]) if d in by_id] for sid in ids}
    indeg = {sid: len(deps[sid]) for sid in ids}
    queue = [sid for sid in ids if indeg[sid] == 0]
    out = []
    while queue:
        sid = queue.pop(0)
        out.append(sid)
        for other in ids:
            if sid in deps[other]:
                indeg[other] -= 1
                if indeg[other] == 0:
                    queue.append(other)
    return [by_id[sid] for sid in out]


def validate_spec(spec):
    errors = []
    if not isinstance(spec, dict):
        return ["spec_not_object"]
    stages = spec.get("stages")
    if not isinstance(stages, list) or not stages:
        return ["stages_required"]
    seen_stages = set()
    for si, stage in enumerate(stages):
        if not isinstance(stage, dict):
            errors.append("stage[%d].not_object" % si)
            continue
        stage_id = stage.get("id")
        if not stage_id:
            errors.append("stage[%d].id_required" % si)
            continue
        if stage_id in seen_stages:
            errors.append("stage[%d].id_duplicate:%s" % (si, stage_id))
        seen_stages.add(stage_id)
        mode = stage.get("mode", "serial")
        if mode not in ("serial", "parallel"):
            errors.append("stage[%s].mode_invalid" % stage_id)
        steps = stage.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("stage[%s].steps_required" % stage_id)
            continue
        ids = set()
        for sj, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append("stage[%s].step[%d].not_object" % (stage_id, sj))
                continue
            step_id = step.get("id")
            if not step_id:
                errors.append("stage[%s].step[%d].id_required" % (stage_id, sj))
                continue
            if step_id in ids:
                errors.append("stage[%s].step[%s].id_duplicate" % (stage_id, step_id))
            ids.add(step_id)
            if not step.get("command"):
                errors.append("stage[%s].step[%s].command_required" % (stage_id, step_id))
            elif ats.HOOK_PATTERN.search(str(step.get("command"))):
                # Step 走 shell=True 直接执行，没有 Task 层那套 @enc 拆分与钩子后置。
                # 静默把 @enc 当普通参数传给命令，会让人以为转换发生了、其实没有。
                errors.append("stage[%s].step[%s].hook_not_supported"
                              % (stage_id, step_id))
            level = str(step.get("level") or ats.DEFAULT_TASK_LEVEL).upper()
            if level not in ats.LEVEL_RANK:
                errors.append("stage[%s].step[%s].level_invalid" % (stage_id, step_id))
            retry = step.get("retry", 0)
            if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0:
                errors.append("stage[%s].step[%s].retry_invalid" % (stage_id, step_id))
            backoff = step.get("retry_backoff", 0)
            if not isinstance(backoff, int) or isinstance(backoff, bool) or backoff < 0:
                errors.append("stage[%s].step[%s].retry_backoff_invalid" % (stage_id, step_id))
            timeout = step.get("timeout", 0)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0:
                errors.append("stage[%s].step[%s].timeout_invalid" % (stage_id, step_id))
        stage_ids = {s.get("id") for s in steps if isinstance(s, dict) and s.get("id")}
        for step in steps:
            if not isinstance(step, dict):
                continue
            dep = step.get("depends_on")
            if dep is None:
                continue
            if isinstance(dep, str):
                deps = [dep]
            elif isinstance(dep, list) and all(isinstance(x, str) for x in dep):
                deps = dep
            else:
                errors.append("stage[%s].step[%s].depends_on_invalid"
                              % (stage_id, step.get("id")))
                continue
            for dep_id in deps:
                if dep_id == step.get("id"):
                    errors.append("stage[%s].step[%s].depends_on_self"
                                  % (stage_id, step.get("id")))
                elif dep_id not in stage_ids:
                    errors.append("stage[%s].step[%s].depends_on_unknown:%s"
                                  % (stage_id, step.get("id"), dep_id))
        if len(order_steps(steps)) < len([s for s in steps if isinstance(s, dict) and s.get("id")]):
            errors.append("stage[%s].depends_on_cycle" % stage_id)
    return errors


def plan_fingerprint(plan):
    """DAG 指纹：Plan Visa 绑定的对象。

    只投影「跑起来会变行为」的字段（stage 顺序与模式、step 的 id/命令/等级/依赖/
    重试/超时/cwd/env），刻意不纳入 plan.json 里的 attempt、status、时间戳等
    运行期元数据 —— 否则计划重跑一次 attempt+1 就会让签证失效，而 DAG 根本没变。

    归一化只做两件不改变语义的事：depends_on 统一成字符串列表并排序、env 键排序。
    其余字段逐字进入哈希，所以改命令、改等级、改依赖都会让指纹变，签证自动失效。
    """
    stages = []
    for stage in (plan.get("stages") or []):
        if not isinstance(stage, dict):
            continue
        steps = []
        for step in (stage.get("steps") or []):
            if not isinstance(step, dict):
                continue
            env = step.get("env") or {}
            steps.append({
                "id": step.get("id"),
                "command": step.get("command"),
                "level": str(step.get("level") or ats.DEFAULT_TASK_LEVEL).upper(),
                "depends_on": sorted(step_deps(step)),
                "retry": step.get("retry", 0),
                "retry_backoff": step.get("retry_backoff", 0),
                "timeout": step.get("timeout", 0),
                "cwd": step.get("cwd") or None,
                "env": {str(k): str(v) for k, v in sorted(env.items())}
                       if isinstance(env, dict) else {},
            })
        stages.append({"id": stage.get("id"),
                       "mode": stage.get("mode", "serial"),
                       "steps": steps})
    canonical = json.dumps(stages, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 状态读写

def load_plan(pdir):
    return ats.jread(Path(pdir) / "plan.json")


def save_plan(pdir, plan):
    plan["updated_at"] = now_ts()
    return ats.jwrite(Path(pdir) / "plan.json", plan)


def load_stage(pdir, stage_id):
    return ats.jread(stage_dir(pdir, stage_id) / "stage.json")


def save_stage(pdir, stage_id, state):
    state["updated_at"] = now_ts()
    return ats.jwrite(stage_dir(pdir, stage_id) / "stage.json", state)


def load_step(pdir, stage_id, step_id):
    data = ats.jread(step_dir(pdir, stage_id, step_id) / "step.json")
    if data is None:
        return {"id": step_id, "status": "pending", "attempts": 0,
                "stall_delivered": False, "approval_id": None}
    return data


def save_step(pdir, stage_id, step_id, state):
    state["updated_at"] = now_ts()
    return ats.jwrite(step_dir(pdir, stage_id, step_id) / "step.json", state)


def default_step_state(step):
    return {
        "id": step.get("id"),
        "status": "pending",
        "depends_on": step_deps(step),
        "retry": int(step.get("retry") or 0),
        "retry_backoff": int(step.get("retry_backoff") or 0),
        "attempts": 0,
        "stall_delivered": False,
        "started_at": None,
        "ended_at": None,
        "pid": None,
        "exit_code": None,
        "approval_id": None,
        "approval_reissued_at": None,
        "skipped_by": None,
        "blocked_by": None,
        "dependency_status": None,
    }


def materialize_plan(root, sdir, spec, plan_id=None, attempt=1):
    pid = plan_id or spec.get("id") or ("p-" + uuid.uuid4().hex[:6])
    pdir = plan_dir(sdir, pid)
    pdir.mkdir(parents=True, exist_ok=True)
    existing = ats.jread(pdir / "plan.json")
    if existing:
        stages = spec.get("stages") or existing.get("stages") or []
        plan = existing
        plan["stages"] = stages
    else:
        plan = {
            "id": pid,
            "schedule_id": Path(sdir).name,
            "title": spec.get("title"),
            "attempt": int(attempt),
            "status": "pending",
            "on_stage_failure": spec.get("on_stage_failure", "continue"),
            "fail_on_critical": spec.get("fail_on_critical", "continue"),
            "stages": spec.get("stages") or [],
            "created_at": now_ts(),
            "created_iso": iso(),
            "started_at": None,
            "ended_at": None,
            "plan_stall_delivered": False,
        }
        save_plan(pdir, plan)
        emit(sdir, {"type": "plan_materialized", "plan_id": pid,
                    "attempt": int(attempt), "stages": [s.get("id") for s in plan["stages"]]})
        ats.log_event(root, "plan_materialized", schedule_id=Path(sdir).name,
                      plan_id=pid, attempt=int(attempt))

    for stage in plan.get("stages") or []:
        sid = stage.get("id")
        st_dir = stage_dir(pdir, sid)
        st_dir.mkdir(parents=True, exist_ok=True)
        if not (st_dir / "stage.json").exists():
            ats.jwrite(st_dir / "stage.json", {
                "id": sid,
                "mode": stage.get("mode", "serial"),
                "status": "pending",
                "started_at": None,
                "ended_at": None,
                "waves": 0,
            })
        for step in stage.get("steps") or []:
            sd = step_dir(pdir, sid, step.get("id"))
            sd.mkdir(parents=True, exist_ok=True)
            if not (sd / "step.json").exists():
                ats.jwrite(sd / "step.json", default_step_state(step))
    return pdir, plan


# ---------------------------------------------------------------- 依赖执行

def stage_todo(pdir, stage):
    todo = []
    for step in stage.get("steps") or []:
        state = load_step(pdir, stage.get("id"), step.get("id"))
        if state.get("status") in TERMINAL_STEP:
            continue
        if state.get("status") == "running":
            continue
        todo.append((step, state))
    return todo


def deps_ready(pdir, stage, step):
    for dep_id in step_deps(step):
        dep = find_step(stage, dep_id)
        if not dep:
            continue
        state = load_step(pdir, stage.get("id"), dep_id)
        if state.get("status") != "succeeded":
            return False
    return True


def skip_dependents(root, pdir, sdir, plan, stage, todo):
    """依赖失败/超时/跳过时，把下游 step 标记 skipped(dependency)。"""
    changed = False
    for step, state in todo:
        deps = step_deps(step)
        if not deps:
            continue
        dep_id, dep_status = None, None
        for candidate in deps:
            dep = find_step(stage, candidate)
            if not dep:
                continue
            status = load_step(pdir, stage.get("id"), candidate).get("status")
            if status in FAILED_STEP + ("skipped",):
                dep_id, dep_status = candidate, status
                break
        if not dep_id:
            continue
        state["status"] = "skipped"
        state["skipped_by"] = "dependency"
        state["blocked_by"] = dep_id
        state["dependency_status"] = dep_status
        state["ended_at"] = now_ts()
        save_step(pdir, stage.get("id"), step.get("id"), state)
        emit(sdir, {"type": "task_skipped", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": stage.get("id"),
                    "step_id": step.get("id"), "skipped_by": "dependency",
                    "blocked_by": dep_id, "dependency_status": dep_status})
        plan_log(root, plan, "task_skipped", stage_id=stage.get("id"),
                 step_id=step.get("id"), skipped_by="dependency",
                 blocked_by=dep_id, dependency_status=dep_status)
        changed = True
    return changed


# ---------------------------------------------------------------- 步骤执行

def start_step(root, sdir, plan, stage, step):
    sid = stage.get("id")
    step_id = step.get("id")
    tdir = step_dir(plan_dir(sdir, plan["id"]), sid, step_id)
    tdir.mkdir(parents=True, exist_ok=True)
    state = load_step(plan_dir(sdir, plan["id"]), sid, step_id)
    state["status"] = "running"
    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["started_at"] = now_ts()
    state["ended_at"] = None
    state["exit_code"] = None
    state["stall_delivered"] = False
    save_step(plan_dir(sdir, plan["id"]), sid, step_id, state)

    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in (step.get("env") or {}).items()})
    cwd = step.get("cwd") or plan.get("cwd") or None
    if cwd:
        cwd = str(Path(cwd).expanduser())
    out = (tdir / "stdout.log").open("a", encoding="utf-8", errors="replace")
    err = (tdir / "stderr.log").open("a", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(str(step.get("command")), cwd=cwd, env=env, shell=True,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    state["pid"] = proc.pid
    save_step(plan_dir(sdir, plan["id"]), sid, step_id, state)
    emit(sdir, {"type": "task_start", "schedule_id": Path(sdir).name,
                "plan_id": plan.get("id"), "stage_id": sid, "step_id": step_id,
                "attempt": state["attempts"], "pid": proc.pid,
                "command": step.get("command")})
    plan_log(root, plan, "task_start", stage_id=sid, step_id=step_id,
             attempt=state["attempts"], pid=proc.pid)
    return {"proc": proc, "stage": stage, "step": step, "state": state,
            "tdir": tdir, "handles": (out, err)}


def finish_step(root, sdir, plan, handle):
    stage = handle["stage"]
    step = handle["step"]
    sid = stage.get("id")
    step_id = step.get("id")
    pdir = plan_dir(sdir, plan["id"])
    proc = handle["proc"]
    timeout = int(step.get("timeout") or 0)
    timed_out = False
    code = None
    try:
        code = proc.wait(timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired:
        timed_out = True
        ats.kill_pid(proc.pid)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = -1
    finally:
        for fh in handle.get("handles") or ():
            try:
                fh.close()
            except Exception:
                pass

    state = load_step(pdir, sid, step_id)
    state["status"] = "timeout" if timed_out else ("succeeded" if code == 0 else "failed")
    state["exit_code"] = code
    state["ended_at"] = now_ts()
    state["pid"] = None
    save_step(pdir, sid, step_id, state)
    return {"status": state["status"], "exit_code": code, "attempt": state["attempts"],
            "timed_out": timed_out, "step_id": step_id, "stage_id": sid}


def finish_with_retry(root, sdir, plan, stage, step, handle, abort=None):
    pdir = plan_dir(sdir, plan["id"])
    max_attempts = 1 + int(step.get("retry") or 0)
    backoff = int(step.get("retry_backoff") or 0)
    result = finish_step(root, sdir, plan, handle)
    if result["status"] == "succeeded":
        emit(sdir, {"type": "task_done", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": result["stage_id"],
                    "step_id": result["step_id"], "attempt": result["attempt"]})
        plan_log(root, plan, "task_done", stage_id=result["stage_id"],
                 step_id=result["step_id"], attempt=result["attempt"])
        return result

    attempt = int(result["attempt"])
    while result["status"] in FAILED_STEP and attempt < max_attempts:
        if abort is not None and abort.is_set():
            break
        emit(sdir, {"type": "task_retry", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": result["stage_id"],
                    "step_id": result["step_id"], "attempt": attempt,
                    "max_attempts": max_attempts, "backoff_seconds": backoff,
                    "prev_error_type": result["status"],
                    "exit_code": result["exit_code"]})
        plan_log(root, plan, "task_retry", stage_id=result["stage_id"],
                 step_id=result["step_id"], attempt=attempt,
                 max_attempts=max_attempts, backoff_seconds=backoff,
                 prev_error_type=result["status"])
        if backoff > 0:
            time.sleep(backoff)
        handle = start_step(root, sdir, plan, stage, step)
        result = finish_step(root, sdir, plan, handle)
        attempt = int(result["attempt"])

    if result["status"] == "succeeded":
        emit(sdir, {"type": "task_done", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": result["stage_id"],
                    "step_id": result["step_id"], "attempt": attempt})
        plan_log(root, plan, "task_done", stage_id=result["stage_id"],
                 step_id=result["step_id"], attempt=attempt)
        return result

    emit(sdir, {"type": "task_failed", "schedule_id": Path(sdir).name,
                "plan_id": plan.get("id"), "stage_id": result["stage_id"],
                "step_id": result["step_id"], "attempt": attempt,
                "max_attempts": max_attempts, "status": result["status"],
                "exit_code": result["exit_code"], "timed_out": result["timed_out"]})
    plan_log(root, plan, "task_failed", stage_id=result["stage_id"],
             step_id=result["step_id"], attempt=attempt,
             status=result["status"], exit_code=result["exit_code"],
             timed_out=result["timed_out"])
    return result


def _step_is_critical(step, fail_fast_all):
    return bool(fail_fast_all or step.get("critical"))


def finish_batch_fail_fast(root, sdir, plan, stage, batch, handles, fail_fast_all):
    """并行批次：关键步骤最终失败时立刻终止同批其余进程树。

    返回 (results, aborted)。results 按完成顺序排列；aborted=True 表示触发整 Plan 停止。
    """
    pdir = plan_dir(sdir, plan["id"])
    sid = stage.get("id")
    abort = threading.Event()
    results = []

    def _annotate(step_id, pid):
        state = load_step(pdir, sid, step_id)
        state["aborted_by"] = "fail_fast"
        state["aborted_at"] = now_ts()
        save_step(pdir, sid, step_id, state)
        emit(sdir, {"type": "task_aborted", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": sid, "step_id": step_id,
                    "reason": "critical_failed", "pid": pid})
        plan_log(root, plan, "task_aborted", stage_id=sid, step_id=step_id,
                 reason="critical_failed", pid=pid)

    with ThreadPoolExecutor(max_workers=max(1, len(batch))) as ex:
        futures = {}
        for (step, _state), handle in zip(batch, handles):
            futures[ex.submit(finish_with_retry, root, sdir, plan, stage,
                              step, handle, abort)] = step

        for fut in as_completed(futures):
            step = futures[fut]
            result = fut.result()
            results.append(result)
            if abort.is_set() or result["status"] not in FAILED_STEP:
                continue
            if not _step_is_critical(step, fail_fast_all):
                continue
            abort.set()
            for other in futures:
                if other is fut or other.done():
                    continue
                other_step = futures[other]
                state = load_step(pdir, sid, other_step.get("id"))
                pid = state.get("pid")
                if not pid:
                    continue
                ats.kill_pid(pid)
                _annotate(other_step.get("id"), pid)

    return results, abort.is_set()


# ---------------------------------------------------------------- 权限预检

def ensure_step_permission(root, plan, sdir, stage, step, approval):
    """返回 (action, payload)；action ∈ ready / failed / paused / denied。"""
    pdir = plan_dir(sdir, plan["id"])
    state = load_step(pdir, stage.get("id"), step.get("id"))
    command = step.get("command")
    context = {
        "required_level": str(step.get("level") or ats.DEFAULT_TASK_LEVEL).upper(),
        "source": "plan",
        "schedule_id": Path(sdir).name,
        "plan_id": plan.get("id"),
        "stage_id": stage.get("id"),
        "step_id": step.get("id"),
        "visa_id": ats.GUARD_VISA_ID,
    }
    approval_id = state.get("approval_id") or approval
    gate = ats.permission_gate(root, command, approval_id, context)

    approved_rec = gate.get("approval") or {}
    human_override = gate["reason"] == "approval" and bool(approved_rec.get("override"))
    preflight = None
    if ats_preflight is not None and not human_override and (
            gate["allowed"] or gate["reason"] == "approval_required"):
        preflight = ats_preflight.review(
            root, command, context.get("required_level"),
            context=dict(context, cwd=step.get("cwd") or plan.get("cwd")),
            pol=ats.policy(root), shell=True)

    if preflight and preflight.get("verdict") == "deny_injection":
        allow_override = bool(ats.policy(root).get("denied_injection_allow_override"))
        record = ats.create_approval(
            root, command, gate["required_level"], context,
            resume=("python ats_plan.py resume %s --approval <approval_id>"
                    % Path(sdir).name),
            preflight=preflight,
            status="pending" if allow_override else "denied_injection")
        state["status"] = "failed"
        state["error_type"] = "denied_injection"
        state["ended_at"] = now_ts()
        state["approval_id"] = record["id"]
        save_step(pdir, stage.get("id"), step.get("id"), state)
        emit(sdir, {"type": "task_failed", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": stage.get("id"),
                    "step_id": step.get("id"), "status": "failed",
                    "error_type": "denied_injection",
                    "approval_id": record["id"],
                    "preflight": preflight})
        plan_log(root, plan, "task_failed", stage_id=stage.get("id"),
                 step_id=step.get("id"), status="failed", error_type="denied_injection")
        return "denied", {"error": "denied_injection", "preflight": preflight,
                          "approval_id": record["id"], "gate": gate,
                          "override_allowed": allow_override}

    if gate["allowed"]:
        if preflight:
            ats.log_event(root, "preflight_completed", step_id=step.get("id"),
                          verdict=preflight.get("verdict"),
                          status=preflight.get("status"),
                          tags=preflight.get("tags"),
                          confidence=preflight.get("confidence"),
                          duration_ms=preflight.get("duration_ms"))
        if approval and not state.get("approval_id"):
            state["approval_id"] = approval
            save_step(pdir, stage.get("id"), step.get("id"), state)
        return "ready", gate

    if gate["reason"] in ("denied_policy", "denied_mode"):
        err = "policy_denied" if gate["reason"] == "denied_policy" else "mode_denied"
        state["status"] = "failed"
        state["error_type"] = err
        state["ended_at"] = now_ts()
        save_step(pdir, stage.get("id"), step.get("id"), state)
        emit(sdir, {"type": "task_failed", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": stage.get("id"),
                    "step_id": step.get("id"), "status": "failed",
                    "error_type": err,
                    "required_level": gate["required_level"],
                    "policy_max": gate["policy_max"],
                    "approval_mode": gate.get("approval_mode")})
        plan_log(root, plan, "task_failed", stage_id=stage.get("id"),
                 step_id=step.get("id"), status="failed", error_type=err)
        return "denied", gate

    if gate["reason"] == "visa_mismatch":
        # 签证作用域对不上 = 跨任务使用。设计上明令「直接拒绝、不扣配额、不回落审批」，
        # 所以这里既不建审批单也不暂停重试，直接判本步失败，把原因摆到事件流里。
        state["status"] = "failed"
        state["error_type"] = "visa_mismatch"
        state["ended_at"] = now_ts()
        save_step(pdir, stage.get("id"), step.get("id"), state)
        emit(sdir, {"type": "task_failed", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": stage.get("id"),
                    "step_id": step.get("id"), "status": "failed",
                    "error_type": "visa_mismatch",
                    "visa_id": gate.get("visa_id"),
                    "visa_scope_type": gate.get("visa_scope_type"),
                    "visa_reason": (gate.get("visa") or {}).get("reason")})
        plan_log(root, plan, "task_failed", stage_id=stage.get("id"),
                 step_id=step.get("id"), status="failed",
                 error_type="visa_mismatch", visa_id=gate.get("visa_id"),
                 visa_reason=(gate.get("visa") or {}).get("reason"))
        return "denied", gate

    # approval_required：缺失/过期时创建审批；pending 不重发（避免刷屏）
    check = ats.approval_check(root, state.get("approval_id"), command, context)
    if check["error"] in ("missing", "expired"):
        old = check.get("record")
        replaced = bool(old)
        record = ats.create_approval(
            root, command, gate["required_level"], context,
            resume=("python ats_plan.py resume %s --approval <approval_id>"
                    % Path(sdir).name),
            preflight=preflight,
        )
        state["approval_id"] = record["id"]
        state["approval_reissued_at"] = now_ts()
        save_step(pdir, stage.get("id"), step.get("id"), state)
        # 事件直接写入本 schedule 的事件流（与 announce_approval 的投递目标相同），
        # 因此不再调用 announce_approval，避免同一条审批产生两条 approval_created。
        emit(sdir, {"type": "approval_created", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": stage.get("id"),
                    "step_id": step.get("id"), "approval_id": record["id"],
                    "required_level": gate["required_level"],
                    "source": context.get("source"),
                    "expires_at": record.get("expires_at"),
                    "approval_error": check["error"],
                    "reissued": replaced,
                    "preflight": preflight,
                    "resume": record.get("resume")})
        plan_log(root, plan, "approval_reissued" if replaced else "approval_requested",
                 stage_id=stage.get("id"), step_id=step.get("id"),
                 approval_id=record["id"], required_level=gate["required_level"],
                 approval_error=check["error"], reissued=replaced)
        return "paused", {"approval": record, "gate": gate,
                          "reissued": replaced, "created": True}

    return "paused", {"approval": check.get("record"), "gate": gate,
                      "reissued": False, "approval_error": check.get("error")}


# ---------------------------------------------------------------- 阶段执行

def pause_plan(root, sdir, plan, stage, step, payload, waves=0):
    plan["status"] = "paused"
    plan["paused_at"] = now_ts()
    plan["paused_stage"] = stage.get("id")
    plan["paused_step"] = step.get("id")
    save_plan(plan_dir(sdir, plan["id"]), plan)
    stage_state = load_stage(plan_dir(sdir, plan["id"]), stage.get("id"))
    stage_state["status"] = "paused"
    # 暂停时先把已消耗的波次数落盘：resume 会重新进入 run_stage，
    # 若不持久化，恢复后计数会从 0 重新开始（stage.json 的 waves 失真）
    stage_state["waves"] = int(waves)
    save_stage(plan_dir(sdir, plan["id"]), stage.get("id"), stage_state)
    emit(sdir, {"type": "plan_paused", "schedule_id": Path(sdir).name,
                "plan_id": plan.get("id"), "stage_id": stage.get("id"),
                "step_id": step.get("id"),
                "approval_id": (payload.get("approval") or {}).get("id")
                if isinstance(payload.get("approval"), dict) else None,
                "required_level": (payload.get("gate") or {}).get("required_level"),
                "approval_error": payload.get("approval_error"),
                "reissued": bool(payload.get("reissued"))})
    plan_log(root, plan, "plan_paused", stage_id=stage.get("id"),
             step_id=step.get("id"),
             approval_id=(payload.get("approval") or {}).get("id")
             if isinstance(payload.get("approval"), dict) else None,
             required_level=(payload.get("gate") or {}).get("required_level"))
    return {"status": "paused", "plan_id": plan.get("id"),
            "stage_id": stage.get("id"), "step_id": step.get("id"),
            "approval": payload.get("approval"), "gate": payload.get("gate"),
            "approval_error": payload.get("approval_error"),
            "reissued": bool(payload.get("reissued"))}


def mark_remaining_skipped(root, pdir, sdir, plan, stage, reason):
    for step in stage.get("steps") or []:
        state = load_step(pdir, stage.get("id"), step.get("id"))
        if state.get("status") in TERMINAL_STEP:
            continue
        state["status"] = "skipped"
        state["skipped_by"] = reason
        state["ended_at"] = now_ts()
        save_step(pdir, stage.get("id"), step.get("id"), state)
        emit(sdir, {"type": "task_skipped", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "stage_id": stage.get("id"),
                    "step_id": step.get("id"), "skipped_by": reason})
        plan_log(root, plan, "task_skipped", stage_id=stage.get("id"),
                 step_id=step.get("id"), skipped_by=reason)


def run_stage(root, sdir, plan, stage, approval, fail_fast=False):
    pdir = plan_dir(sdir, plan["id"])
    stage_id = stage.get("id")
    mode = stage.get("mode", "serial")
    on_failure = plan.get("on_stage_failure", "continue")
    fail_on_critical = bool(fail_fast or plan.get("fail_on_critical") == "stop")
    stage_state = load_stage(pdir, stage_id) or {"id": stage_id, "mode": mode}
    if stage_state.get("status") in ("done", "failed", "skipped"):
        return "skipped", {}
    stage_state["status"] = "running"
    stage_state["started_at"] = stage_state.get("started_at") or now_ts()
    save_stage(pdir, stage_id, stage_state)

    # 波次数跨 pause/resume 累加：暂停前的轮次已落盘在 stage.json
    waves = int(stage_state.get("waves") or 0)
    while waves < MAX_WAVES:
        todo = stage_todo(pdir, stage)
        if not todo:
            break
        waves += 1
        # 依赖传播后不在此处 continue：否则下一轮权限检查会被整轮跳过
        skip_dependents(root, pdir, sdir, plan, stage, todo)
        todo = stage_todo(pdir, stage)
        if not todo:
            break

        for step, _state in todo:
            action, payload = ensure_step_permission(root, plan, sdir, stage, step, approval)
            if action == "paused":
                return "paused", pause_plan(root, sdir, plan, stage, step, payload, waves)
            if action == "denied":
                if on_failure == "stop":
                    mark_remaining_skipped(root, pdir, sdir, plan, stage, "stage_failed")
                    stage_state["status"] = "failed"
                    stage_state["ended_at"] = now_ts()
                    stage_state["waves"] = waves
                    save_stage(pdir, stage_id, stage_state)
                    return "failed", {}
                continue

        todo = stage_todo(pdir, stage)
        ready = [(step, state) for step, state in todo if deps_ready(pdir, stage, step)]
        if not ready:
            break
        batch = ready[:1] if mode == "serial" else ready

        handles = [start_step(root, sdir, plan, stage, step) for step, _ in batch]
        critical_failed = False
        if fail_on_critical and len(batch) > 1:
            results, critical_failed = finish_batch_fail_fast(
                root, sdir, plan, stage, batch, handles, fail_fast)
        else:
            results = [finish_with_retry(root, sdir, plan, stage, step, handle)
                       for (step, _), handle in zip(batch, handles)]
            if fail_on_critical:
                critical_failed = any(
                    r["status"] in FAILED_STEP and _step_is_critical(step, fail_fast)
                    for (step, _), r in zip(batch, results))

        if critical_failed:
            mark_remaining_skipped(root, pdir, sdir, plan, stage, "critical_failed")
            stage_state["status"] = "failed"
            stage_state["ended_at"] = now_ts()
            stage_state["waves"] = waves
            save_stage(pdir, stage_id, stage_state)
            return "failed", {"critical_failure": True}

        if on_failure == "stop" and any(r["status"] in FAILED_STEP for r in results):
            mark_remaining_skipped(root, pdir, sdir, plan, stage, "stage_failed")
            stage_state["status"] = "failed"
            stage_state["ended_at"] = now_ts()
            stage_state["waves"] = waves
            save_stage(pdir, stage_id, stage_state)
            return "failed", {}

    todo = stage_todo(pdir, stage)
    if todo:
        # 仍有未终结步骤但没有可执行波次 → 视为死锁/依赖未满足
        stage_state["status"] = "failed"
        stage_state["ended_at"] = now_ts()
        stage_state["waves"] = waves
        stage_state["error_type"] = "no_ready_steps"
        save_stage(pdir, stage_id, stage_state)
        plan_log(root, plan, "stage_failed", stage_id=stage_id,
                 error_type="no_ready_steps")
        return "failed", {"error_type": "no_ready_steps"}

    failed = any(
        load_step(pdir, stage_id, step.get("id")).get("status") in FAILED_STEP
        for step in stage.get("steps") or []
    )
    stage_state["status"] = "failed" if failed else "done"
    stage_state["ended_at"] = now_ts()
    stage_state["waves"] = waves
    save_stage(pdir, stage_id, stage_state)
    emit(sdir, {"type": "stage_failed" if failed else "stage_done",
                "schedule_id": Path(sdir).name, "plan_id": plan.get("id"),
                "stage_id": stage_id, "waves": waves})
    plan_log(root, plan, "stage_failed" if failed else "stage_done",
             stage_id=stage_id, waves=waves)
    return ("failed" if failed else "ok"), {"waves": waves}


# ---------------------------------------------------------------- 快照与保留

def summarize_plan(pdir, plan):
    counts = {}
    steps = []
    for stage in plan.get("stages") or []:
        for step in stage.get("steps") or []:
            state = load_step(pdir, stage.get("id"), step.get("id"))
            status = state.get("status")
            counts[status] = counts.get(status, 0) + 1
            steps.append({
                "stage_id": stage.get("id"),
                "step_id": step.get("id"),
                "status": status,
                "attempts": state.get("attempts") or 0,
                "exit_code": state.get("exit_code"),
                "started_at": state.get("started_at"),
                "ended_at": state.get("ended_at"),
                "skipped_by": state.get("skipped_by"),
                "blocked_by": state.get("blocked_by"),
            })
    return {
        "plan_id": plan.get("id"),
        "schedule_id": plan.get("schedule_id"),
        "attempt": plan.get("attempt"),
        "status": plan.get("status"),
        "on_stage_failure": plan.get("on_stage_failure"),
        "started_at": plan.get("started_at"),
        "ended_at": plan.get("ended_at"),
        "counts": counts,
        "steps": steps,
    }


def write_run_snapshot(root, sdir, plan):
    pdir = plan_dir(sdir, plan["id"])
    attempt = plan.get("attempt") or 1
    # 快照目录名：<plan_id>-a<attempt>；plan_id 已含该后缀时不重复追加
    name = plan["id"]
    suffix = "-a%s" % attempt
    if not name.endswith(suffix):
        name = name + suffix
    run_dir = Path(sdir) / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_plan(pdir, plan)
    ats.jwrite(run_dir / "summary.json", summary)
    ats.jwrite(run_dir / "plan.json", plan)
    schedule = ats.jread(Path(sdir) / "schedule.json", {}) or {}
    ats.jwrite(run_dir / "schedule.json", schedule)
    step_lines = []
    for record in summary["steps"]:
        step_lines.append(json.dumps(record, ensure_ascii=False))
    (run_dir / "steps.jsonl").write_text("\n".join(step_lines) + ("\n" if step_lines else ""),
                                        encoding="utf-8")
    emit(sdir, {"type": "run_snapshot", "schedule_id": Path(sdir).name,
                "plan_id": plan["id"], "attempt": attempt,
                "path": str(run_dir), "status": plan.get("status"),
                "counts": summary["counts"]})
    plan_log(root, plan, "run_snapshot", attempt=attempt, path=str(run_dir),
             status=plan.get("status"))
    keep = int(ats.policy(root).get("keep_runs_per_plan") or 0)
    pruned = prune_runs(root, sdir, keep)
    return run_dir, summary, pruned


def group_runs(sdir):
    runs_dir = Path(sdir) / "runs"
    if not runs_dir.exists():
        return {}
    groups = {}
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        summary = ats.jread(run_dir / "summary.json", {}) or {}
        pid = summary.get("plan_id") or run_dir.name.rsplit("-a", 1)[0]
        groups.setdefault(pid, []).append((run_dir, summary))
    return groups


def preview_prune(root, sdir, keep):
    """返回将删除的快照目录（按 plan 保留最近 keep 份）。"""
    out = []
    if int(keep) <= 0:
        return out
    for pid, entries in group_runs(sdir).items():
        if len(entries) <= int(keep):
            continue
        ordered = sorted(entries, key=lambda item: (
            int(item[1].get("ended_at") or item[1].get("started_at") or 0),
            item[0].stat().st_mtime))
        for run_dir, _summary in ordered[: len(ordered) - int(keep)]:
            out.append(run_dir)
    return out


def prune_runs(root, sdir, keep):
    targets = preview_prune(root, sdir, keep)
    removed = []
    for run_dir in targets:
        try:
            for child in Path(run_dir).glob("*"):
                if child.is_file():
                    child.unlink()
            Path(run_dir).rmdir()
            removed.append(str(run_dir))
        except OSError:
            continue
    if removed:
        emit(sdir, {"type": "prune_runs", "schedule_id": Path(sdir).name,
                    "keep": int(keep), "removed": removed})
        ats.log_event(root, "prune_runs", schedule_id=Path(sdir).name,
                      keep=int(keep), removed=len(removed))
    return removed


# ---------------------------------------------------------------- 停滞检测

def log_quiet_seconds(root, plan, sdir):
    """返回该 plan 距最近一条调度器日志的秒数。"""
    path = Path(root) / "scheduler.log"
    last = int(plan.get("started_at") or plan.get("created_at") or now_ts())
    if path.exists():
        for rec in ats.read_jsonl(path):
            if rec.get("plan_id") != plan.get("id"):
                continue
            if rec.get("schedule_id") != Path(sdir).name:
                continue
            try:
                last = max(last, int(rec.get("ts") or 0))
            except (TypeError, ValueError):
                continue
    return max(0, now_ts() - last)


def detect_plan_stalls(root, sdir, plan):
    """检测 Plan 层停滞：step 级看已运行时长，plan 级看最近日志静默时长。

    两类都只发一次（step 用 stall_delivered，plan 用 plan_stall_delivered），
    避免轮询时反复投递同一事件。
    """
    threshold = int(ats.policy(root).get("stall_seconds") or 0)
    if threshold <= 0:
        return []
    pdir = plan_dir(sdir, plan["id"])
    if plan.get("status") != "running":
        return []
    events = []

    for stage in plan.get("stages") or []:
        for step in stage.get("steps") or []:
            state = load_step(pdir, stage.get("id"), step.get("id"))
            if state.get("status") != "running" or state.get("stall_delivered"):
                continue
            started = int(state.get("started_at") or 0)
            quiet = now_ts() - started
            if quiet < threshold:
                continue
            payload = {
                "type": "plan_stalled", "scope": "step",
                "schedule_id": Path(sdir).name, "plan_id": plan.get("id"),
                "stage_id": stage.get("id"), "step_id": step.get("id"),
                "quiet_seconds": quiet, "threshold": threshold,
                "attempt": state.get("attempts"),
            }
            emit(sdir, payload)
            plan_log(root, plan, "plan_stalled", scope="step",
                     stage_id=stage.get("id"), step_id=step.get("id"),
                     quiet_seconds=quiet, threshold=threshold,
                     attempt=state.get("attempts"))
            events.append(payload)
            state["stall_delivered"] = True
            save_step(pdir, stage.get("id"), step.get("id"), state)

    if not plan.get("plan_stall_delivered"):
        quiet = log_quiet_seconds(root, plan, sdir)
        if quiet >= threshold:
            payload = {
                "type": "plan_stalled", "scope": "plan",
                "schedule_id": Path(sdir).name, "plan_id": plan.get("id"),
                "quiet_seconds": quiet, "threshold": threshold,
                "status": plan.get("status"),
            }
            emit(sdir, payload)
            plan_log(root, plan, "plan_stalled", scope="plan",
                     quiet_seconds=quiet, threshold=threshold,
                     status=plan.get("status"))
            events.append(payload)
            plan["plan_stall_delivered"] = True
            save_plan(pdir, plan)
    return events


# ---------------------------------------------------------------- 执行入口

def recover_interrupted(root, sdir, pdir, plan):
    """把上次异常中断残留的 running step 复位为 pending，避免阶段永久卡住。"""
    recovered = []
    for stage in plan.get("stages") or []:
        for step in stage.get("steps") or []:
            state = load_step(pdir, stage.get("id"), step.get("id"))
            if state.get("status") != "running":
                continue
            state["status"] = "pending"
            state["started_at"] = None
            state["pid"] = None
            state["stall_delivered"] = False
            save_step(pdir, stage.get("id"), step.get("id"), state)
            recovered.append("%s/%s" % (stage.get("id"), step.get("id")))
    if recovered:
        emit(sdir, {"type": "steps_recovered", "schedule_id": Path(sdir).name,
                    "plan_id": plan.get("id"), "steps": recovered})
        plan_log(root, plan, "steps_recovered", steps=recovered)
    return recovered


def acquire_plan_lock(pdir):
    """Plan 层原子互斥锁（O_CREAT|O_EXCL）。返回 (lock_path, None) 或 (None, detail)。"""
    lock_path = Path(pdir) / "run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder, age = {}, -1
            try:
                age = now_ts() - int(lock_path.stat().st_mtime)
            except OSError:
                pass
            try:
                holder = json.loads(lock_path.read_text(encoding="utf-8"))
            except Exception:
                holder = {}
            holder_pid = holder.get("pid")
            # 内容尚未写完（无 pid）按存活处理；只看持有者是否已死，锁龄不授权抢锁，
            # 否则长任务（>5min）会被第二个驱动者误判为崩溃残留
            if not holder_pid or ats.pid_alive(holder_pid) or attempt == 2:
                return None, {"reason": "run_lock_busy", "holder": holder, "age": age}
            try:
                lock_path.unlink()          # 持有者已死：清理残留后重试一次
            except OSError:
                return None, {"reason": "run_lock_busy", "holder": holder, "age": age}
            continue
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "ts": now_ts()}).encode("utf-8"))
        finally:
            os.close(fd)
        return str(lock_path), None
    return None, {"reason": "run_lock_busy", "holder": {}, "age": -1}


def run_plan(root, plan_id, approval=None, fail_fast=False):
    sdir, pdir = resolve_plan(root, plan_id)
    if not sdir:
        return {"error": "not_found", "plan_id": plan_id}, EXIT_CONFLICT
    lock_path, lock_err = acquire_plan_lock(pdir)
    if lock_err:
        return {"error": "run_lock_busy", "plan_id": pdir.name, "lock": lock_err,
                "hint": "同一 Plan 已有驱动者在运行；确认其进程已死后重试"}, EXIT_CONFLICT
    try:
        return _run_plan_locked(root, sdir, pdir, approval, fail_fast)
    finally:
        ats.release_run_lock(lock_path)


def _run_plan_locked(root, sdir, pdir, approval=None, fail_fast=False):
    plan = ats.jread(pdir / "plan.json")
    prev = plan.get("status")
    if prev in ("done", "failed", "cancelled"):
        # 终态 Plan 不再执行：否则所有 Stage 返回 skipped，入口处的 running 会被收尾改写成 done
        return {"error": "state_conflict", "plan_id": plan.get("id"), "status": prev,
                "hint": "用 rerun_task 重置失败步骤后再 run_plan"}, EXIT_CONFLICT
    first = prev in ("pending", None)
    resumed = prev in ("paused", "running")

    plan["status"] = "running"
    plan["started_at"] = plan.get("started_at") or now_ts()
    plan["started_iso"] = plan.get("started_iso") or iso()
    plan["ended_at"] = None
    save_plan(pdir, plan)
    recovered = recover_interrupted(root, sdir, pdir, plan)
    if first:
        emit(sdir, {"type": "plan_start", "schedule_id": Path(sdir).name,
                    "plan_id": plan["id"], "attempt": plan.get("attempt")})
        plan_log(root, plan, "plan_start", attempt=plan.get("attempt"))
    elif resumed or approval:
        emit(sdir, {"type": "plan_resumed", "schedule_id": Path(sdir).name,
                    "plan_id": plan["id"], "approval": approval,
                    "recovered": recovered})
        plan_log(root, plan, "plan_resumed", approval=approval,
                 recovered=len(recovered))

    result = {"plan_id": plan["id"], "schedule_id": Path(sdir).name,
              "stages": [], "status": "running"}
    for stage in plan.get("stages") or []:
        outcome, extra = run_stage(root, sdir, plan, stage, approval, fail_fast)
        result["stages"].append({"stage_id": stage.get("id"), "result": outcome,
                                 "detail": {k: v for k, v in (extra or {}).items()
                                            if k in ("waves", "error_type", "critical_failure")}})
        if outcome == "paused":
            result["status"] = "paused"
            result["approval"] = extra.get("approval") if isinstance(extra, dict) else None
            plan = ats.jread(pdir / "plan.json") or plan
            return result, EXIT_OK
        if outcome == "failed":
            stop_all = (plan.get("on_stage_failure", "continue") == "stop"
                        or bool((extra or {}).get("critical_failure")))
            if stop_all:
                skip_reason = ("critical_failed" if bool((extra or {}).get("critical_failure"))
                               else "on_stage_failure=stop")
                for rest in plan.get("stages") or []:
                    state = load_stage(pdir, rest.get("id")) or {}
                    if state.get("status") in ("done", "failed", "skipped"):
                        continue
                    state["status"] = "skipped"
                    state["ended_at"] = now_ts()
                    save_stage(pdir, rest.get("id"), state)
                    emit(sdir, {"type": "stage_skipped", "schedule_id": Path(sdir).name,
                                "plan_id": plan["id"], "stage_id": rest.get("id"),
                                "reason": skip_reason})
                result["status"] = "failed"
                break
            result["status"] = "partial"

    plan = ats.jread(pdir / "plan.json") or plan
    if result["status"] in ("partial", "failed"):
        plan["status"] = "failed"
    elif result["status"] == "running":
        plan["status"] = "done"
    plan["ended_at"] = now_ts()
    plan["ended_iso"] = iso()
    save_plan(pdir, plan)
    result["status"] = plan["status"]
    emit(sdir, {"type": "plan_end", "schedule_id": Path(sdir).name,
                "plan_id": plan["id"], "status": plan["status"],
                "attempt": plan.get("attempt")})
    plan_log(root, plan, "plan_end", status=plan["status"],
             attempt=plan.get("attempt"))
    run_dir, summary, pruned = write_run_snapshot(root, sdir, plan)
    result["run_dir"] = str(run_dir)
    result["counts"] = summary["counts"]
    result["pruned"] = pruned
    return result, EXIT_OK


# ---------------------------------------------------------------- Schedule

def _load_spec(path):
    p = Path(path)
    if not p.exists():
        return None, "spec_not_found"
    data = ats.jread(p)
    if not isinstance(data, dict):
        return None, "spec_invalid"
    return data, None


def cmd_submit_plan(root, args):
    spec, err = _load_spec(args.spec)
    if err:
        return ats.print_json({"error": err, "spec": args.spec}, EXIT_USAGE)
    errors = validate_spec(spec)
    if errors:
        return ats.print_json({"error": "spec_invalid", "errors": errors}, EXIT_USAGE)

    sid = args.id or spec.get("id") or ("s-" + uuid.uuid4().hex[:6])
    sdir = schedule_dir(root, sid)
    if (sdir / "schedule.json").exists():
        return ats.print_json({"error": "schedule_exists", "schedule_id": sid}, EXIT_CONFLICT)
    sdir.mkdir(parents=True, exist_ok=True)
    schedule = {
        "id": sid,
        "type": "oneshot",
        "status": "active",
        "every": 0,
        "next_run": None,
        "runs": 0,
        "max_runs": 1,
        "current_plan": None,
        "base_plan_id": sid,
        "plan_spec": spec,
        "on_stage_failure": spec.get("on_stage_failure", "continue"),
        "created_at": now_ts(),
        "created_iso": iso(),
    }
    ats.jwrite(sdir / "schedule.json", schedule)
    emit(sdir, {"type": "schedule_created", "schedule_id": sid,
                "kind": "oneshot", "title": spec.get("title")})
    ats.log_event(root, "schedule_created", schedule_id=sid, kind="oneshot")

    pid = "%s-a1" % sid
    pdir, plan = materialize_plan(root, sdir, spec, plan_id=pid, attempt=1)
    schedule["current_plan"] = pid
    schedule["runs"] = 1
    ats.jwrite(sdir / "schedule.json", schedule)
    return ats.print_json({"schedule_id": sid, "plan_id": pid, "plan_dir": str(pdir),
                           "status": "pending",
                           "run_with": "python ats_plan.py run_plan %s" % pid})


def cmd_submit_schedule(root, args):
    spec, err = _load_spec(args.spec)
    if err:
        return ats.print_json({"error": err, "spec": args.spec}, EXIT_USAGE)
    plan_ref = spec.get("plan")
    if not plan_ref:
        return ats.print_json({"error": "missing_plan_spec"}, EXIT_USAGE)
    plan_spec, perr = _load_spec(plan_ref)
    if perr:
        return ats.print_json({"error": "plan_%s" % perr, "plan": plan_ref}, EXIT_USAGE)
    errors = validate_spec(plan_spec)
    if errors:
        return ats.print_json({"error": "spec_invalid", "errors": errors}, EXIT_USAGE)
    every = spec.get("every")
    if not isinstance(every, int) or isinstance(every, bool) or every <= 0:
        return ats.print_json({"error": "every_invalid"}, EXIT_USAGE)
    max_runs = spec.get("max_runs")
    if max_runs is not None and (not isinstance(max_runs, int) or max_runs <= 0):
        return ats.print_json({"error": "max_runs_invalid"}, EXIT_USAGE)

    sid = args.id or spec.get("id") or ("s-" + uuid.uuid4().hex[:6])
    sdir = schedule_dir(root, sid)
    if (sdir / "schedule.json").exists():
        return ats.print_json({"error": "schedule_exists", "schedule_id": sid}, EXIT_CONFLICT)
    sdir.mkdir(parents=True, exist_ok=True)
    start_at = spec.get("start_at")
    try:
        next_run = int(start_at) if start_at is not None else now_ts() + every
    except (TypeError, ValueError):
        return ats.print_json({"error": "start_at_invalid"}, EXIT_USAGE)
    schedule = {
        "id": sid,
        "type": "recurring",
        "status": "active",
        "every": every,
        "next_run": next_run,
        "start_at": start_at,
        "runs": 0,
        "max_runs": max_runs,
        "current_plan": None,
        "base_plan_id": sid,
        "plan_spec": plan_spec,
        "on_stage_failure": spec.get("on_stage_failure",
                                     plan_spec.get("on_stage_failure", "continue")),
        "created_at": now_ts(),
        "created_iso": iso(),
        "next_run_iso": iso_run(next_run),
    }
    ats.jwrite(sdir / "schedule.json", schedule)
    emit(sdir, {"type": "schedule_created", "schedule_id": sid, "kind": "recurring",
                "every": every, "next_run": next_run})
    ats.log_event(root, "schedule_created", schedule_id=sid, kind="recurring",
                  every=every)
    return ats.print_json({"schedule_id": sid, "every": every,
                           "next_run": next_run, "next_run_iso": iso_run(next_run),
                           "drive_with": "python ats_plan.py check_schedule %s" % sid})


def _trigger_schedule(root, sdir, schedule):
    attempt = int(schedule.get("runs") or 0) + 1
    pid = "%s-a%d" % (schedule.get("base_plan_id") or schedule["id"], attempt)
    spec = dict(schedule.get("plan_spec") or {})
    spec["on_stage_failure"] = schedule.get("on_stage_failure", "continue")
    prev = json.loads(json.dumps(schedule))   # 深拷贝：plan_spec 是嵌套 dict，浅拷贝回滚不了它
    # 先持久化触发意图，最后才建 plan：materialize 失败时宁可少跑一轮，
    # 也不能留下「runs 未推进 + plan.json 已被建过」的脏状态供下一轮复用
    schedule["current_plan"] = pid
    schedule["runs"] = attempt
    schedule["next_run"] = now_ts() + int(schedule.get("every") or 0)
    schedule["last_run_at"] = now_ts()
    schedule["next_run_iso"] = iso_run(schedule["next_run"])
    if schedule.get("max_runs") and attempt >= int(schedule["max_runs"]):
        schedule["status"] = "done"
        schedule["next_run"] = None
        schedule["next_run_iso"] = None
    ats.jwrite(sdir / "schedule.json", schedule)
    try:
        pdir, plan = materialize_plan(root, sdir, spec, plan_id=pid, attempt=attempt)
    except Exception as exc:
        schedule.clear()
        schedule.update(prev)             # 回滚，避免 runs 比实际执行次数多 1
        ats.jwrite(sdir / "schedule.json", schedule)
        ats.log_event(root, "schedule_materialize_failed", schedule_id=schedule.get("id"),
                      attempt=attempt, error=str(exc))
        raise
    emit(sdir, {"type": "schedule_triggered", "schedule_id": schedule["id"],
                "plan_id": pid, "attempt": attempt,
                "next_run": schedule.get("next_run")})
    ats.log_event(root, "schedule_triggered", schedule_id=schedule["id"],
                  plan_id=pid, attempt=attempt)
    return pid


def cmd_run_plan(root, args):
    result, code = run_plan(root, args.plan_id, args.approval,
                            fail_fast=getattr(args, "fail_fast", False))
    return ats.print_json(result, code)


def cmd_resume(root, args):
    sdir, pdir = resolve_plan(root, args.ident)
    if not sdir:
        return ats.print_json({"error": "not_found", "ident": args.ident}, EXIT_CONFLICT)
    plan = ats.jread(pdir / "plan.json")
    if plan.get("status") == "done":
        return ats.print_json({"error": "plan_done", "plan_id": plan["id"]}, EXIT_CONFLICT)
    if plan.get("status") == "paused" and not args.approval:
        state = ats.jread(pdir / "stages" / plan.get("paused_stage") / "steps"
                          / plan.get("paused_step") / "step.json", {}) or {}
        return ats.print_json({
            "status": "paused",
            "plan_id": plan["id"],
            "stage_id": plan.get("paused_stage"),
            "step_id": plan.get("paused_step"),
            "approval_id": state.get("approval_id"),
            "hint": "批准后使用 resume <id> --approval <approval_id>",
        })
    result, code = run_plan(root, plan["id"], args.approval,
                            fail_fast=getattr(args, "fail_fast", False))
    return ats.print_json(result, code)


def cmd_check_schedule(root, args):
    sdir = resolve_schedule(root, args.schedule_id)
    if not sdir:
        return ats.print_json({"error": "not_found", "schedule_id": args.schedule_id},
                              EXIT_CONFLICT)
    schedule = ats.jread(sdir / "schedule.json", {}) or {}
    payload = {"schedule_id": schedule.get("id"), "status": schedule.get("status"),
               "runs": schedule.get("runs"), "next_run": schedule.get("next_run"),
               "next_run_iso": iso_run(schedule.get("next_run")),
               "triggered": [], "stalled": [], "next_poll": 0}

    if schedule.get("status") == "active" and schedule.get("next_run") is not None \
            and now_ts() >= int(schedule["next_run"]):
        pid = _trigger_schedule(root, sdir, schedule)
        schedule = ats.jread(sdir / "schedule.json", {}) or schedule
        payload["triggered"].append(pid)
        payload["runs"] = schedule.get("runs")
        payload["next_run"] = schedule.get("next_run")
        payload["next_run_iso"] = iso_run(schedule.get("next_run"))
        result, _code = run_plan(root, pid)
        payload["plan"] = result
        if result.get("status") == "paused":
            schedule["status"] = "paused"
            ats.jwrite(sdir / "schedule.json", schedule)
            payload["status"] = "paused"
        schedule = ats.jread(sdir / "schedule.json", {}) or schedule

    stalls = []
    for pdir in sorted((sdir / "plans").glob("*")) if (sdir / "plans").exists() else []:
        plan = ats.jread(pdir / "plan.json")
        if not plan:
            continue
        stalls.extend(detect_plan_stalls(root, sdir, plan))
    payload["stalled"] = stalls

    schedule = ats.jread(sdir / "schedule.json", {}) or schedule
    if schedule.get("status") == "active":
        nxt = schedule.get("next_run")
        if nxt is None:
            payload["status"] = "done"
            payload["next_poll"] = 0
        else:
            wait = max(0, int(nxt) - now_ts())
            payload["next_poll"] = min(POLL_STALLED, wait) if wait > 0 else 0
            payload["status"] = "active"
            payload["next_run"] = nxt
            payload["next_run_iso"] = iso_run(nxt)
    else:
        payload["status"] = schedule.get("status")
        payload["next_poll"] = 0
    if stalls:
        payload["next_poll"] = POLL_STALLED
    return ats.print_json(payload)


def cmd_pause(root, args):
    sdir = resolve_schedule(root, args.schedule_id)
    if not sdir:
        return ats.print_json({"error": "not_found", "schedule_id": args.schedule_id},
                              EXIT_CONFLICT)
    schedule = ats.jread(sdir / "schedule.json", {}) or {}
    if schedule.get("status") != "active":
        return ats.print_json({"error": "state_conflict", "status": schedule.get("status")},
                              EXIT_CONFLICT)
    gate = ats.gate_operation(root, "L1", "pause %s" % schedule["id"], args.approval,
                              {"schedule_id": schedule["id"], "source": "pause",
                               "resume": "python ats_plan.py pause %s --approval <approval_id>"
                                         % schedule["id"]})
    if not gate.get("allowed"):
        return ats.print_json(gate, EXIT_DENIED)
    schedule["status"] = "paused"
    schedule["paused_at"] = now_ts()
    ats.jwrite(sdir / "schedule.json", schedule)
    emit(sdir, {"type": "schedule_paused", "schedule_id": schedule["id"]})
    ats.log_event(root, "schedule_paused", schedule_id=schedule["id"])
    return ats.print_json({"schedule_id": schedule["id"], "status": "paused"})


def cmd_resume_schedule(root, args):
    sdir = resolve_schedule(root, args.schedule_id)
    if not sdir:
        return ats.print_json({"error": "not_found", "schedule_id": args.schedule_id},
                              EXIT_CONFLICT)
    schedule = ats.jread(sdir / "schedule.json", {}) or {}
    if schedule.get("status") != "paused":
        return ats.print_json({"error": "state_conflict", "schedule_id": schedule.get("id"),
                               "status": schedule.get("status"),
                               "hint": "只有 paused 的 Schedule 可以 resume_schedule"},
                              EXIT_CONFLICT)
    gate = ats.gate_operation(root, "L1", "resume_schedule %s" % schedule["id"], args.approval,
                              {"schedule_id": schedule["id"], "source": "resume_schedule",
                               "resume": "python ats_plan.py resume_schedule %s --approval <approval_id>"
                                         % schedule["id"]})
    if not gate.get("allowed"):
        return ats.print_json(gate, EXIT_DENIED)

    now = now_ts()
    schedule["status"] = "active"
    schedule["resumed_at"] = now
    if schedule.get("next_run") is None:
        # 暂停期间 next_run 可能已被清空；按周期补一个，避免恢复后永不触发
        schedule["next_run"] = now + int(schedule.get("every") or 0)
        schedule["next_run_iso"] = iso_run(schedule["next_run"])
    ats.jwrite(sdir / "schedule.json", schedule)

    current = {}
    if schedule.get("current_plan"):
        current = ats.jread(plan_dir(sdir, schedule["current_plan"]) / "plan.json", {}) or {}
    emit(sdir, {"type": "schedule_resumed", "schedule_id": schedule["id"],
                "next_run": schedule.get("next_run")})
    ats.log_event(root, "schedule_resumed", schedule_id=schedule["id"],
                  next_run=schedule.get("next_run"))
    payload = {"schedule_id": schedule["id"], "status": "active",
               "next_run": schedule.get("next_run"),
               "next_run_iso": schedule.get("next_run_iso"),
               "current_plan": schedule.get("current_plan"),
               "current_plan_status": current.get("status"),
               "gate": gate.get("reason")}
    if current.get("status") == "paused":
        payload["warning"] = "current_plan 仍 paused：需先 approve 并 resume 该 Plan，或 rerun_plan 重置"
    return ats.print_json(payload)


def cmd_cancel(root, args):
    sdir = resolve_schedule(root, args.schedule_id)
    if not sdir:
        return ats.print_json({"error": "not_found", "schedule_id": args.schedule_id},
                              EXIT_CONFLICT)
    schedule = ats.jread(sdir / "schedule.json", {}) or {}
    if schedule.get("status") == "cancelled":
        return ats.print_json({"error": "already_cancelled"}, EXIT_CONFLICT)
    gate = ats.gate_operation(root, "L2", "cancel %s" % schedule["id"], args.approval,
                              {"schedule_id": schedule["id"], "source": "cancel",
                               "resume": "python ats_plan.py cancel %s --approval <approval_id>"
                                         % schedule["id"]})
    if not gate.get("allowed"):
        return ats.print_json(gate, EXIT_DENIED)

    cancelled_steps = []
    for pdir in sorted((sdir / "plans").glob("*")) if (sdir / "plans").exists() else []:
        plan = ats.jread(pdir / "plan.json")
        if not plan or plan.get("status") in ("done", "failed"):
            continue
        for stage in plan.get("stages") or []:
            for step in stage.get("steps") or []:
                state = load_step(pdir, stage.get("id"), step.get("id"))
                if state.get("status") in TERMINAL_STEP:
                    continue
                state["status"] = "skipped"
                state["skipped_by"] = "user"
                state["ended_at"] = now_ts()
                save_step(pdir, stage.get("id"), step.get("id"), state)
                cancelled_steps.append("%s/%s" % (stage.get("id"), step.get("id")))
        plan["status"] = "cancelled"
        plan["ended_at"] = now_ts()
        save_plan(pdir, plan)

    schedule["status"] = "cancelled"
    schedule["cancelled_at"] = now_ts()
    schedule["next_run"] = None
    ats.jwrite(sdir / "schedule.json", schedule)
    emit(sdir, {"type": "schedule_cancelled", "schedule_id": schedule["id"],
                "skipped_steps": cancelled_steps})
    ats.log_event(root, "schedule_cancelled", schedule_id=schedule["id"],
                  skipped_steps=len(cancelled_steps))
    return ats.print_json({"schedule_id": schedule["id"], "status": "cancelled",
                           "skipped_steps": cancelled_steps, "gate": gate.get("reason")})


def cmd_rerun_task(root, args):
    sdir = resolve_schedule(root, args.schedule_id)
    if not sdir:
        return ats.print_json({"error": "not_found", "schedule_id": args.schedule_id},
                              EXIT_CONFLICT)
    schedule = ats.jread(sdir / "schedule.json", {}) or {}
    plan = ats.jread(plan_dir(sdir, schedule.get("current_plan")) / "plan.json")
    if not plan:
        return ats.print_json({"error": "no_current_plan"}, EXIT_CONFLICT)
    stage = next((s for s in plan.get("stages") or [] if s.get("id") == args.stage_id), None)
    if not stage:
        return ats.print_json({"error": "stage_not_found", "stage_id": args.stage_id},
                              EXIT_CONFLICT)
    step = find_step(stage, args.step_id)
    if not step:
        return ats.print_json({"error": "step_not_found", "step_id": args.step_id},
                              EXIT_CONFLICT)

    gate = ats.gate_operation(root, "L1",
                              "rerun %s/%s/%s" % (schedule["id"], args.stage_id,
                                                  args.step_id),
                              args.approval,
                              {"schedule_id": schedule["id"], "plan_id": plan["id"],
                               "stage_id": args.stage_id, "step_id": args.step_id,
                               "source": "rerun",
                               "resume": "python ats_plan.py rerun_task %s %s %s --approval <approval_id>"
                                         % (schedule["id"], args.stage_id, args.step_id)})
    if not gate.get("allowed"):
        return ats.print_json(gate, EXIT_DENIED)

    pdir = plan_dir(sdir, plan["id"])
    state = load_step(pdir, args.stage_id, args.step_id)
    state.update({"status": "pending", "attempts": 0, "exit_code": None,
                  "started_at": None, "ended_at": None, "stall_delivered": False,
                  "skipped_by": None, "blocked_by": None, "dependency_status": None})
    save_step(pdir, args.stage_id, args.step_id, state)

    rearmed = []
    for downstream in stage.get("steps") or []:
        deps = step_deps(downstream)
        if args.step_id not in deps:
            continue
        dstate = load_step(pdir, args.stage_id, downstream.get("id"))
        if dstate.get("status") == "skipped" and dstate.get("skipped_by") == "dependency":
            dstate.update({"status": "pending", "skipped_by": None, "blocked_by": None,
                           "dependency_status": None, "attempts": 0,
                           "exit_code": None, "started_at": None, "ended_at": None})
            save_step(pdir, args.stage_id, downstream.get("id"), dstate)
            rearmed.append(downstream.get("id"))
    stage_state = load_stage(pdir, args.stage_id) or {"id": args.stage_id}
    stage_state["status"] = "pending"
    stage_state["ended_at"] = None
    stage_state["error_type"] = None
    save_stage(pdir, args.stage_id, stage_state)
    plan["status"] = "pending"
    plan["ended_at"] = None
    plan["plan_stall_delivered"] = False
    save_plan(pdir, plan)
    emit(sdir, {"type": "task_rerun", "schedule_id": schedule["id"],
                "plan_id": plan["id"], "stage_id": args.stage_id,
                "step_id": args.step_id, "rearmed_steps": rearmed})
    ats.log_event(root, "task_rerun", schedule_id=schedule["id"], plan_id=plan["id"],
                  stage_id=args.stage_id, step_id=args.step_id)
    return ats.print_json({"schedule_id": schedule["id"], "plan_id": plan["id"],
                           "stage_id": args.stage_id, "step_id": args.step_id,
                           "status": "pending", "rearmed_steps": rearmed})


def cmd_rerun_plan(root, args):
    sdir, pdir = resolve_plan(root, args.plan_id)
    if not sdir:
        return ats.print_json({"error": "not_found", "plan_id": args.plan_id},
                              EXIT_CONFLICT)
    plan = ats.jread(pdir / "plan.json")
    if not plan:
        return ats.print_json({"error": "not_found", "plan_id": args.plan_id},
                              EXIT_CONFLICT)
    lock_path, lock_err = acquire_plan_lock(pdir)
    if lock_err:
        return ats.print_json({"error": "run_lock_busy", "plan_id": plan.get("id"),
                               "lock": lock_err,
                               "hint": "该 Plan 正在执行，等待结束后再重置"}, EXIT_CONFLICT)
    try:
        gate = ats.gate_operation(root, "L1", "rerun_plan %s" % plan["id"], args.approval,
                                  {"schedule_id": Path(sdir).name, "plan_id": plan["id"],
                                   "source": "rerun_plan",
                                   "resume": "python ats_plan.py rerun_plan %s --approval <approval_id>"
                                             % plan["id"]})
        if not gate.get("allowed"):
            return ats.print_json(gate, EXIT_DENIED)

        plan["status"] = "pending"
        plan["started_at"] = None
        plan["started_iso"] = None
        plan["ended_at"] = None
        plan["ended_iso"] = None
        for key in ("paused_at", "paused_stage", "paused_step"):
            plan.pop(key, None)
        plan["plan_stall_delivered"] = False
        save_plan(pdir, plan)

        rearmed = 0
        for stage in plan.get("stages") or []:
            stage_state = load_stage(pdir, stage.get("id")) or {"id": stage.get("id")}
            stage_state.update({"status": "pending", "started_at": None, "ended_at": None,
                                "waves": 0, "error_type": None})
            save_stage(pdir, stage.get("id"), stage_state)
            for step in stage.get("steps") or []:
                state = load_step(pdir, stage.get("id"), step.get("id")) \
                    or default_step_state(step)
                state.update({"status": "pending", "attempts": 0, "exit_code": None,
                              "started_at": None, "ended_at": None, "pid": None,
                              "approval_id": None, "approval_reissued_at": None,
                              "skipped_by": None, "blocked_by": None,
                              "dependency_status": None, "stall_delivered": False})
                save_step(pdir, stage.get("id"), step.get("id"), state)
                rearmed += 1

        schedule = ats.jread(Path(sdir) / "schedule.json", {}) or {}
        emit(sdir, {"type": "plan_rerun", "schedule_id": Path(sdir).name,
                    "plan_id": plan["id"], "rearmed_steps": rearmed})
        ats.log_event(root, "plan_rerun", schedule_id=Path(sdir).name,
                      plan_id=plan["id"], rearmed_steps=rearmed)
        payload = {"schedule_id": Path(sdir).name, "plan_id": plan["id"],
                   "status": "pending", "rearmed_steps": rearmed,
                   "schedule_status": schedule.get("status"),
                   "gate": gate.get("reason")}
        if schedule.get("status") not in (None, "active"):
            payload["warning"] = "schedule 状态为 %s；周期触发需先 resume_schedule %s" \
                                 % (schedule.get("status"), Path(sdir).name)
        return ats.print_json(payload)
    finally:
        ats.release_run_lock(lock_path)


# ---------------------------------------------------------------- 查看与维护

def cmd_list(root, args):
    rows = []
    for sdir in sorted(schedules_dir(root).glob("*")) if schedules_dir(root).exists() else []:
        schedule = ats.jread(sdir / "schedule.json")
        if not schedule:
            continue
        plan = ats.jread(plan_dir(sdir, schedule.get("current_plan")) / "plan.json", {}) or {}
        counts = {}
        if plan:
            pdir = plan_dir(sdir, plan.get("id"))
            for stage in plan.get("stages") or []:
                for step in stage.get("steps") or []:
                    state = load_step(pdir, stage.get("id"), step.get("id"))
                    counts[state.get("status")] = counts.get(state.get("status"), 0) + 1
        rows.append({
            "schedule_id": schedule.get("id"),
            "type": schedule.get("type"),
            "status": schedule.get("status"),
            "runs": schedule.get("runs"),
            "max_runs": schedule.get("max_runs"),
            "every": schedule.get("every"),
            "next_run": schedule.get("next_run"),
            "current_plan": schedule.get("current_plan"),
            "plan_status": plan.get("status"),
            "counts": counts,
        })
    if args.json:
        return ats.print_json({"schedules": rows, "count": len(rows)})
    if not rows:
        print("no schedules")
        return EXIT_OK
    print("%-10s %-9s %-10s %-5s %-6s %-9s %s"
          % ("SCHEDULE", "TYPE", "STATUS", "RUNS", "EVERY", "PLAN", "COUNTS"))
    for row in rows:
        print("%-10s %-9s %-10s %-5s %-6s %-9s %s"
              % (row["schedule_id"], row["type"], row["status"], row["runs"],
                 row["every"] or "-", row["plan_status"] or "-",
                 json.dumps(row["counts"], ensure_ascii=False)))
    return EXIT_OK


def cmd_show(root, args):
    sdir, pdir = resolve_plan(root, args.ident)
    if not sdir:
        sdir = resolve_schedule(root, args.ident)
        if not sdir:
            return ats.print_json({"error": "not_found", "ident": args.ident}, EXIT_CONFLICT)
    schedule = ats.jread(sdir / "schedule.json", {}) or {}
    if not pdir or args.ident == schedule.get("id"):
        pid = schedule.get("current_plan")
        pdir = plan_dir(sdir, pid) if pid else None
    payload = {"schedule": schedule}
    if pdir and (pdir / "plan.json").exists():
        plan = ats.jread(pdir / "plan.json")
        payload["plan"] = plan
        payload["summary"] = summarize_plan(pdir, plan)
    if args.json:
        return ats.print_json(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_delete(root, args):
    sdir, pdir = resolve_plan(root, args.ident)
    target_kind = "plan"
    if not sdir:
        sdir = resolve_schedule(root, args.ident)
        target_kind = "schedule"
    if not sdir:
        return ats.print_json({"error": "not_found", "ident": args.ident}, EXIT_CONFLICT)

    schedule = ats.jread(sdir / "schedule.json", {}) or {}
    gate = ats.gate_operation(root, "L2", "delete %s" % args.ident, args.approval,
                              {"schedule_id": schedule.get("id"), "source": "delete",
                               "resume": "python ats_plan.py delete %s --approval <approval_id>"
                                         % args.ident})
    if not gate.get("allowed"):
        return ats.print_json(gate, EXIT_DENIED)

    if target_kind == "plan" and pdir:
        plan = ats.jread(pdir / "plan.json", {}) or {}
        if plan.get("status") == "running":
            return ats.print_json({"error": "state_conflict", "status": "running"},
                                  EXIT_CONFLICT)
        for child in sorted(Path(pdir).rglob("*"), reverse=True):
            try:
                child.rmdir() if child.is_dir() else child.unlink()
            except OSError:
                continue
        try:
            Path(pdir).rmdir()
        except OSError:
            pass
        ats.log_event(root, "plan_deleted", schedule_id=schedule.get("id"),
                      plan_id=args.ident)
        return ats.print_json({"deleted": True, "kind": "plan", "plan_id": args.ident})

    if schedule.get("status") in ("active", "paused"):
        schedule["status"] = "cancelled"
        schedule["next_run"] = None
        ats.jwrite(sdir / "schedule.json", schedule)
    for child in sorted(Path(sdir).rglob("*"), reverse=True):
        try:
            child.rmdir() if child.is_dir() else child.unlink()
        except OSError:
            continue
    try:
        Path(sdir).rmdir()
    except OSError:
        pass
    ats.log_event(root, "schedule_deleted", schedule_id=args.ident)
    return ats.print_json({"deleted": True, "kind": "schedule",
                           "schedule_id": args.ident})


def cmd_history(root, args):
    sdir = resolve_schedule(root, args.schedule_id)
    if not sdir:
        return ats.print_json({"error": "not_found", "schedule_id": args.schedule_id},
                              EXIT_CONFLICT)
    keep = int(ats.policy(root).get("keep_runs_per_plan") or 0)
    runs = []
    for pid, entries in group_runs(sdir).items():
        if args.plan and pid != args.plan:
            continue
        for run_dir, summary in sorted(entries, key=lambda it: it[0].name):
            runs.append({
                "run": run_dir.name,
                "plan_id": pid,
                "attempt": summary.get("attempt"),
                "status": summary.get("status"),
                "counts": summary.get("counts"),
                "started_at": summary.get("started_at"),
                "ended_at": summary.get("ended_at"),
                "path": str(run_dir),
            })
    payload = {"schedule_id": args.schedule_id, "runs": runs, "count": len(runs),
               "keep_runs_per_plan": keep}
    if args.logs:
        path = Path(root) / "scheduler.log"
        records = [r for r in ats.read_jsonl(path)
                   if r.get("schedule_id") == args.schedule_id]
        payload["logs"] = records[-args.lines:] if args.lines > 0 else records
    return ats.print_json(payload)


def cmd_prune(root, args):
    keep = args.keep if args.keep is not None \
        else int(ats.policy(root).get("keep_runs_per_plan") or 0)
    targets = []
    for sdir in sorted(schedules_dir(root).glob("*")) if schedules_dir(root).exists() else []:
        if not (sdir / "schedule.json").exists():
            continue
        pending = preview_prune(root, sdir, keep)
        targets.append({"schedule_id": sdir.name, "keep": keep,
                        "would_remove": [str(p) for p in pending]})
    if args.dry_run:
        total = sum(len(t["would_remove"]) for t in targets)
        return ats.print_json({"dry_run": True, "keep": keep,
                               "schedules": targets, "count": total})
    removed = []
    for sdir in sorted(schedules_dir(root).glob("*")) if schedules_dir(root).exists() else []:
        if not (sdir / "schedule.json").exists():
            continue
        removed.extend(prune_runs(root, sdir, keep))
    return ats.print_json({"dry_run": False, "keep": keep, "removed": removed,
                           "count": len(removed)})


# ---------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(
        prog="ats_plan.py",
        description="agent-task-scheduler — Plan / Schedule 层执行器 (v3.3.0-R0)",
    )
    ap.add_argument("--root", default=None, help="ATS_ROOT，默认 <ats 本体目录>/.ats")
    sub = ap.add_subparsers(dest="command_name")

    p = sub.add_parser("submit_plan", help="提交一次性计划")
    p.add_argument("--spec", required=True)
    p.add_argument("--id", default=None)

    p = sub.add_parser("submit_schedule", help="提交周期计划")
    p.add_argument("--spec", required=True)
    p.add_argument("--id", default=None)

    p = sub.add_parser("run_plan", help="执行计划")
    p.add_argument("plan_id")
    p.add_argument("--approval", default=None)
    p.add_argument("--fail-fast", action="store_true",
                   help="任一步骤失败即终止整个 Plan（等同把所有步骤视为 critical）")

    p = sub.add_parser("resume", help="从暂停处继续")
    p.add_argument("ident")
    p.add_argument("--approval", default=None)
    p.add_argument("--fail-fast", action="store_true",
                   help="任一步骤失败即终止整个 Plan（等同把所有步骤视为 critical）")

    p = sub.add_parser("check_schedule", help="驱动周期计划")
    p.add_argument("schedule_id")

    p = sub.add_parser("pause", help="暂停周期计划")
    p.add_argument("schedule_id")
    p.add_argument("--approval", default=None)

    p = sub.add_parser("cancel", help="取消计划/周期")
    p.add_argument("schedule_id")
    p.add_argument("--approval", default=None)

    p = sub.add_parser("resume_schedule", help="恢复已暂停的周期计划")
    p.add_argument("schedule_id")
    p.add_argument("--approval", default=None)

    p = sub.add_parser("rerun_task", help="重跑某一步并重新武装下游")
    p.add_argument("schedule_id")
    p.add_argument("stage_id")
    p.add_argument("step_id")
    p.add_argument("--approval", default=None)

    p = sub.add_parser("rerun_plan", help="把整个 Plan 重置为 pending")
    p.add_argument("plan_id")
    p.add_argument("--approval", default=None)

    p = sub.add_parser("list", help="列出计划")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="查看详情")
    p.add_argument("ident")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("delete", help="删除计划/周期")
    p.add_argument("ident")
    p.add_argument("--approval", default=None)

    p = sub.add_parser("history", help="查看运行快照")
    p.add_argument("schedule_id")
    p.add_argument("--plan", default=None)
    p.add_argument("--logs", action="store_true")
    p.add_argument("--lines", type=int, default=20)

    p = sub.add_parser("prune", help="清理旧快照")
    p.add_argument("--keep", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")

    return ap


HANDLERS = {
    "submit_plan": cmd_submit_plan,
    "submit_schedule": cmd_submit_schedule,
    "run_plan": cmd_run_plan,
    "resume": cmd_resume,
    "check_schedule": cmd_check_schedule,
    "pause": cmd_pause,
    "cancel": cmd_cancel,
    "resume_schedule": cmd_resume_schedule,
    "rerun_task": cmd_rerun_task,
    "rerun_plan": cmd_rerun_plan,
    "list": cmd_list,
    "show": cmd_show,
    "delete": cmd_delete,
    "history": cmd_history,
    "prune": cmd_prune,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 与 ats.py 同一套启动准入：ats_plan 也会真正执行命令，不能绕过看门狗。
    skip_guard, guard_approval, visa_id, passphrase, argv = ats._extract_guard_flags(argv)
    ats.GUARD_PASSPHRASE = passphrase or None
    ats.GUARD_VISA_ID = visa_id or None
    root = ats.ats_root(ats._pre_scan_root(argv))
    denied = ats.guard_handshake(root, skip_guard=skip_guard, guard_approval=guard_approval,
                                 visa_id=visa_id)
    if denied is not None:
        ats.print_json(denied["payload"], denied["code"])
        return denied["code"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command_name:
        parser.print_help()
        return EXIT_USAGE
    root = ats.ats_root(args.root)
    handler = HANDLERS[args.command_name]
    return handler(root, args) or EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
