#!/usr/bin/env python3
"""ats_ui.py — agent-task-scheduler 人类用本地 Web 面板 (v2.9.1)

只读监控 + 运行控制 + 审批 + 创建编排。纯标准库，无第三方依赖。

用法:
  python scripts/ats_ui.py [--root DIR] [--host 127.0.0.1] [--port 8765] [--open]
  python scripts/ats_ui.py [--root DIR] --ensure --open   # 未运行则后台自启动，再打开浏览器
  python scripts/ats_ui.py [--root DIR] --status | --stop
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "2.9.1"
SERVER_START = int(time.time())

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_FILE = Path(__file__).resolve()
ATS = SCRIPT_DIR / "ats.py"
PLAN = SCRIPT_DIR / "ats_plan.py"

ROOT = Path.cwd() / ".ats"
PORT = 8765
sys.path.insert(0, str(SCRIPT_DIR))
try:                                    # MCP HTTP 端点；工具表复用同目录 ats_mcp.py
    import ats_mcp_http as mcp_http
except Exception:
    mcp_http = None
MCP_ENDPOINT = None                     # 由 serve() 创建，默认关闭，只在控制台进程内启停
JOBS = []
JOBS_LOCK = threading.Lock()
POLICY_CACHE = {"ts": 0.0, "data": None}


def default_root():
    try:
        base = Path.cwd()
    except OSError:  # 工作目录已被删除
        base = SCRIPT_DIR.parent
    return base / ".ats"


def resolve_root(value):
    if value:
        return Path(value).expanduser().resolve()
    env = os.environ.get("ATS_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return default_root().resolve()


def to_ts(value):
    """把 epoch 秒/毫秒、ISO 字符串、数字字符串统一成 int 秒，无法识别返回 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        v = int(value)
        return v // 1000 if v > 10 ** 12 else v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        try:
            return int(float(s))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return 0


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def tail_text(path, lines):
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-int(lines):]) if lines and lines > 0 else "\n".join(data)


def run_cli(script, argv, timeout=90):
    cmd = [sys.executable, str(script)] + [str(x) for x in argv]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"code": -1, "data": {"error": "cli_timeout", "timeout": timeout},
                "stdout": "", "stderr": ""}
    except OSError as exc:
        return {"code": -1, "data": {"error": "cli_spawn_failed", "message": str(exc)},
                "stdout": "", "stderr": ""}
    data = None
    text = (proc.stdout or "").strip()
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return {"code": proc.returncode, "data": data,
            "stdout": proc.stdout or "", "stderr": proc.stderr or ""}


def normalize_task(task, fallback_id):
    """兼容旧版 task.json（task_id/required_level、无 id）。"""
    if not task.get("id"):
        task["id"] = task.get("task_id") or fallback_id
    if not task.get("level") and task.get("required_level"):
        task["level"] = task["required_level"]
    return task


def task_rows(root):
    rows = []
    base = Path(root) / "tasks"
    if base.exists():
        for path in base.glob("*/task.json"):
            task = read_json(path)
            if isinstance(task, dict):
                rows.append(normalize_task(task, path.parent.name))
    rows.sort(key=lambda t: (to_ts(t.get("created_at")), str(t.get("id") or "")), reverse=True)
    return rows[:300]


def schedule_rows(root, errors=None):
    result = run_cli(PLAN, ["--root", str(root), "list", "--json"], timeout=30)
    data = result.get("data")
    if isinstance(data, dict) and "schedules" in data:
        return data["schedules"]
    if errors is not None:
        msg = (result.get("stderr") or "").strip().splitlines()
        detail = msg[-1] if msg else ("exit %s" % result.get("code"))
        errors.append({"scope": "计划列表", "message": detail[:300]})
    return []


def approval_rows(root):
    rows = []
    base = Path(root) / "approvals"
    if base.exists():
        now = int(time.time())
        for path in base.glob("*.json"):
            rec = read_json(path)
            if not isinstance(rec, dict):
                continue
            expires = rec.get("expires_at")
            rec["_expired"] = bool(expires and now >= int(expires))
            rows.append(rec)
    rows.sort(key=lambda r: (to_ts(r.get("created_at"))), reverse=True)
    return rows[:200]


def event_rows(root, limit=60):
    path = Path(root) / "events.jsonl"
    rows = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    rows.reverse()
    return rows


def policy_info(root, errors=None):
    now = time.time()
    if POLICY_CACHE["data"] is not None and now - POLICY_CACHE["ts"] < 15:
        return POLICY_CACHE["data"]
    result = run_cli(ATS, ["--root", str(root), "policy", "--json"], timeout=20)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if not data and errors is not None:
        detail = (result.get("stderr") or "").strip().splitlines()
        errors.append({"scope": "策略", "message": (detail[-1] if detail else "exit %s" % result.get("code"))[:300]})
    POLICY_CACHE.update(ts=now, data=data)
    return data


def invalidate_policy_cache():
    """policy.set 成功后立即失效 15s 缓存，避免 UI 读到旧开关。"""
    POLICY_CACHE.update(ts=0.0, data=None)


def mcp_state():
    """MCP 端点状态。来自进程内存里的真实监听对象，不做持久化猜测。"""
    if MCP_ENDPOINT is None:
        return {"available": False, "status": "stopped", "host": "127.0.0.1",
                "port": None, "url": None, "sessions": 0, "started_at": None,
                "error": "MCP 模块未加载（缺少 ats_mcp_http.py 或 ats_mcp.py）",
                "transport": "streamable-http", "protocol_version": None,
                "config": None}
    return MCP_ENDPOINT.state()


def require_mcp():
    if MCP_ENDPOINT is None:
        raise ValueError("MCP 模块未加载（缺少 ats_mcp_http.py 或 ats_mcp.py）")
    return MCP_ENDPOINT


def build_state(root):
    errors = []

    def safe(label, fn, fallback):
        try:
            return fn()
        except Exception as exc:  # 单个子项异常不拖垮整个 /api/state
            errors.append({"scope": label,
                           "message": ("%s: %s" % (type(exc).__name__, exc))[:300]})
            return fallback

    tasks = safe("任务", lambda: task_rows(root), [])
    schedules = safe("计划", lambda: schedule_rows(root, errors), [])
    approvals = safe("审批", lambda: approval_rows(root), [])
    events = safe("事件", lambda: event_rows(root), [])
    jobs = safe("后台操作", lambda: jobs_snapshot(), [])
    policy = safe("策略", lambda: policy_info(root, errors), {})

    running = [t for t in tasks if t.get("status") in ("starting", "running")]
    failed = [t for t in tasks if t.get("status") == "failed"]
    pending = [a for a in approvals if a.get("status") == "pending" and not a.get("_expired")]
    return {
        "server": {"ok": True, "version": VERSION, "pid": os.getpid(),
                   "started_at": SERVER_START, "port": PORT, "script": str(SCRIPT_FILE)},
        "root": str(root),
        "now": int(time.time()),
        "tasks": tasks,
        "schedules": schedules,
        "approvals": approvals,
        "events": events,
        "jobs": jobs,
        "policy": policy,
        "errors": errors,
        "counts": {
            "tasks": len(tasks),
            "running": len(running),
            "failed": len(failed),
            "pending_approvals": len(pending),
            "schedules": len(schedules),
            "plans_running": sum(1 for s in schedules if s.get("plan_status") in ("running", "starting")),
        },
        "mcp": safe("MCP", mcp_state,
                    {"available": False, "status": "stopped",
                     "error": "MCP 状态读取失败"}),
    }


def jobs_snapshot():
    with JOBS_LOCK:
        return list(JOBS[:12])


def job_update(job, **fields):
    with JOBS_LOCK:
        job.update(fields)


def spawn_async(root, action, script, argv, label):
    job = {"id": "job-" + uuid.uuid4().hex[:6], "action": action, "label": label,
           "status": "running", "started_at": int(time.time()), "ended_at": None,
           "code": None, "note": None}
    with JOBS_LOCK:
        JOBS.insert(0, job)
        del JOBS[30:]

    def worker():
        result = run_cli(script, argv, timeout=None)
        data = result.get("data")
        note = None
        if isinstance(data, dict):
            note = data.get("status") or data.get("error")
            if data.get("error") and data.get("hint"):
                note = "%s：%s" % (data["error"], data["hint"])
        job_update(job, status="ok" if result["code"] == 0 else "failed",
                   code=result["code"], ended_at=int(time.time()), note=note)

    threading.Thread(target=worker, daemon=True, name="ats-ui-job").start()
    return job


def task_detail(root, task_id):
    tdir = Path(root) / "tasks" / task_id
    task = read_json(tdir / "task.json")
    if not isinstance(task, dict):
        return None
    normalize_task(task, task_id)
    files = {}
    for name in ("stdout.log", "stderr.log", "hook-enc.log", "supervisor.log"):
        p = tdir / name
        files[name] = {"path": str(p), "size": p.stat().st_size if p.exists() else 0}
    return {"task": task, "files": files}


def plan_detail(root, ident):
    result = run_cli(PLAN, ["--root", str(root), "show", ident, "--json"], timeout=30)
    if isinstance(result.get("data"), dict) and result.get("code") == 0:
        return result["data"]
    return {"error": result.get("data") or {"error": "plan_show_failed", "exit_code": result.get("code")}}


def action_result(result, action, is_async=False, job=None):
    data = result.get("data")
    payload = {
        "ok": result.get("code") == 0,
        "exit_code": result.get("code"),
        "action": action,
        "async": is_async,
        "result": data,
        "stdout": (result.get("stdout") or "")[-3000:],
        "stderr": (result.get("stderr") or "")[-3000:],
    }
    if not isinstance(data, dict):
        payload["result"] = {"raw": (result.get("stdout") or result.get("stderr") or "").strip()[:3000]}
    if job:
        payload["job"] = job
    return payload


def require(payload, key):
    value = payload.get(key)
    if value in (None, "", []):
        raise ValueError("缺少参数 %s" % key)
    return value


def dispatch(root, payload):
    action = payload.get("action") or ""

    # MCP 开关：开启等于把「执行命令」暴露给本机任意进程，必须显式确认。
    if action == "mcp.start":
        if payload.get("confirm") is not True:
            raise ValueError("开启 MCP 端点需要确认：这相当于把「执行命令」的能力"
                             "暴露给本机任意进程")
        endpoint = require_mcp()
        ok, error = endpoint.start()
        state = endpoint.state()
        return {"ok": ok, "action": action, "async": False, "result": state,
                "error": error, "mcp": state}

    if action == "mcp.stop":
        endpoint = require_mcp()
        ok, error = endpoint.stop()
        state = endpoint.state()
        return {"ok": ok, "action": action, "async": False, "result": state,
                "error": error, "mcp": state}

    if action == "task.run":
        argv = ["--root", str(root), "run", str(require(payload, "task_id"))]
        if payload.get("force"):
            argv.append("--force")
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(ATS, argv, 30), action)

    if action == "task.retry":
        argv = ["--root", str(root), "run", str(require(payload, "task_id")), "--force"]
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(ATS, argv, 30), action)

    if action == "task.stop":
        argv = ["--root", str(root), "stop", str(require(payload, "task_id"))]
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(ATS, argv, 30), action)

    if action == "task.delete":
        argv = ["--root", str(root), "delete", str(require(payload, "task_id"))]
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(ATS, argv, 30), action)

    if action == "task.create":
        argv = ["--root", str(root), "create"]
        if payload.get("id"):
            argv.append(str(payload["id"]).strip())
        argv += ["--command", str(require(payload, "command"))]
        if payload.get("cwd"):
            argv += ["--cwd", str(payload["cwd"])]
        if payload.get("level"):
            argv += ["--level", str(payload["level"]).upper()]
        if payload.get("timeout") not in (None, ""):
            argv += ["--timeout", str(int(payload["timeout"]))]
        if payload.get("description"):
            argv += ["--description", str(payload["description"])]
        if payload.get("force"):
            argv.append("--force")
        for env_line in (payload.get("env") or "").splitlines():
            line = env_line.strip()
            if line:
                argv += ["--env", line]
        return action_result(run_cli(ATS, argv, 30), action)

    if action == "pack.create":
        commands = payload.get("commands") or []
        if isinstance(commands, str):
            commands = [commands]
        commands = [str(c).strip() for c in commands if str(c).strip()]
        if not commands:
            raise ValueError("指令包至少需要一条命令")
        joined = ",".join(commands)
        argv = ["--root", str(root), "pack", joined]
        if payload.get("pack_id"):
            argv += ["--pack-id", str(payload["pack_id"])]
        if payload.get("cwd"):
            argv += ["--cwd", str(payload["cwd"])]
        if payload.get("level"):
            argv += ["--level", str(payload["level"]).upper()]
        if payload.get("timeout") not in (None, ""):
            argv += ["--timeout", str(int(payload["timeout"]))]
        if payload.get("description"):
            argv += ["--description", str(payload["description"])]
        return action_result(run_cli(ATS, argv, 40), action)

    if action == "approval.resolve":
        ids = payload.get("ids") or []
        argv = ["--root", str(root), "approve"]
        if payload.get("all"):
            argv.append("--all")
        else:
            if not ids:
                raise ValueError("缺少审批 ID")
            argv += [str(x) for x in ids]
        if payload.get("reject"):
            argv.append("--reject")
        if payload.get("note"):
            argv += ["--note", str(payload["note"])]
        if payload.get("override"):
            argv.append("--override")
        return action_result(run_cli(ATS, argv, 30), action)

    if action == "policy.set":
        patch = payload.get("patch") or {}
        if not isinstance(patch, dict):
            raise ValueError("patch 必须是对象")
        argv = ["--root", str(root), "policy"]
        for key, value in patch.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            elif isinstance(value, bool):
                value = "true" if value else "false"
            argv += ["--set", str(key), str(value)]
        result = action_result(run_cli(ATS, argv, 30), action)
        invalidate_policy_cache()
        return result

    if action == "plan.submit":
        spec_text = str(require(payload, "spec_text"))
        try:
            spec_obj = json.loads(spec_text)
        except ValueError as exc:
            raise ValueError("Plan spec 不是合法 JSON：%s" % exc)
        if not isinstance(spec_obj, dict):
            raise ValueError("Plan spec 必须是 JSON 对象")
        ui_tmp = Path(root) / "ui_tmp"
        ui_tmp.mkdir(parents=True, exist_ok=True)
        spec_path = ui_tmp / ("spec-%d-%s.json" % (int(time.time()), uuid.uuid4().hex[:6]))
        spec_path.write_text(json.dumps(spec_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        argv = ["--root", str(root), "submit_plan", "--spec", str(spec_path)]
        if payload.get("id"):
            argv += ["--id", str(payload["id"])]
        try:
            return action_result(run_cli(PLAN, argv, 60), action)
        finally:
            try:
                spec_path.unlink()
            except OSError:
                pass

    if action == "plan.run":
        plan_id = str(require(payload, "plan_id"))
        argv = ["--root", str(root), "run_plan", plan_id]
        if payload.get("fail_fast"):
            argv.append("--fail-fast")
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        job = spawn_async(root, action, PLAN, argv, "执行 Plan %s" % plan_id)
        return {"ok": True, "async": True, "action": action, "job": job,
                "message": "Plan 已在后台执行"}

    if action == "plan.resume":
        ident = str(require(payload, "ident"))
        argv = ["--root", str(root), "resume", ident]
        if payload.get("fail_fast"):
            argv.append("--fail-fast")
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        job = spawn_async(root, action, PLAN, argv, "继续 Plan %s" % ident)
        return {"ok": True, "async": True, "action": action, "job": job,
                "message": "继续命令已在后台执行"}

    if action == "plan.pause":
        argv = ["--root", str(root), "pause", str(require(payload, "schedule_id"))]
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(PLAN, argv, 30), action)

    if action == "plan.cancel":
        argv = ["--root", str(root), "cancel", str(require(payload, "ident"))]
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(PLAN, argv, 30), action)

    if action == "plan.unpause":
        argv = ["--root", str(root), "resume_schedule", str(require(payload, "schedule_id"))]
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(PLAN, argv, 30), action)

    if action == "plan.rerun":
        argv = ["--root", str(root), "rerun_plan", str(require(payload, "plan_id"))]
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(PLAN, argv, 30), action)

    if action == "plan.delete":
        argv = ["--root", str(root), "delete", str(require(payload, "ident"))]
        if payload.get("approval"):
            argv += ["--approval", str(payload["approval"])]
        return action_result(run_cli(PLAN, argv, 30), action)

    if action == "schedule.tick":
        schedule_id = str(require(payload, "schedule_id"))
        argv = ["--root", str(root), "check_schedule", schedule_id]
        job = spawn_async(root, action, PLAN, argv, "驱动周期 %s" % schedule_id)
        return {"ok": True, "async": True, "action": action, "job": job,
                "message": "周期检查已在后台执行"}

    raise ValueError("未知操作：%s" % action)


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ATS 控制台</title>
<style>
:root{
  --bg:#0c0f14; --panel:#141922; --panel2:#1a2029; --line:#242d3a;
  --text:#dbe3ee; --muted:#7f8b9e; --accent:#3ddbc0; --blue:#6aa8ff;
  --ok:#3ecf8e; --warn:#f0b429; --bad:#ff6b6b; --purple:#b18aff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
button{font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);
  border-radius:6px;padding:6px 12px;cursor:pointer}
button:hover{border-color:#3a4758;background:#202936}
button.primary{background:rgba(61,219,192,.14);border-color:rgba(61,219,192,.45);color:var(--accent)}
button.danger{background:rgba(255,107,107,.12);border-color:rgba(255,107,107,.4);color:var(--bad)}
button.ghost{background:transparent}
button:disabled{opacity:.45;cursor:not-allowed}
input,select,textarea{background:#0f141b;border:1px solid var(--line);color:var(--text);
  border-radius:6px;padding:7px 10px;font:inherit;width:100%}
textarea{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;resize:vertical}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
header{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;
  gap:16px;padding:12px 22px;background:rgba(12,15,20,.92);border-bottom:1px solid var(--line);
  backdrop-filter:blur(8px)}
.brand{display:flex;align-items:center;gap:12px}
.logo{display:grid;place-items:center;width:38px;height:38px;border-radius:9px;
  background:linear-gradient(135deg,rgba(61,219,192,.22),rgba(106,168,255,.18));
  border:1px solid rgba(61,219,192,.4);color:var(--accent);font-weight:800;letter-spacing:.5px}
h1{margin:0;font-size:17px;font-weight:650}
.sub{font-size:12px;color:var(--muted);font-family:ui-monospace,Consolas,monospace}
.head-right{display:flex;align-items:center;gap:10px}
.live{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px rgba(62,207,142,.15)}
.live.off{background:var(--bad);box-shadow:0 0 0 3px rgba(255,107,107,.15)}
.live.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(240,180,41,.18)}
.banner{display:none;margin:10px 22px 0;padding:10px 14px;border-radius:9px;font-size:13px;
  background:rgba(255,107,107,.1);border:1px solid rgba(255,107,107,.4);color:#ffd7d7}
.banner.warn{background:rgba(240,180,41,.1);border-color:rgba(240,180,41,.4);color:#ffe9b8}
.banner code{font-family:ui-monospace,Consolas,monospace;background:#0f141b;border:1px solid var(--line);
  border-radius:5px;padding:2px 6px;color:var(--text);word-break:break-all}
.banner .hint{color:var(--muted);margin-top:6px}
.banner button{float:right;margin-left:8px}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;padding:14px 22px 4px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.kpi .v{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.kpi .l{font-size:12px;color:var(--muted)}
.kpi.hot .v{color:var(--accent)} .kpi.warn .v{color:var(--warn)} .kpi.bad .v{color:var(--bad)}
nav.tabs{display:flex;gap:6px;padding:10px 22px 0;flex-wrap:wrap}
.tab{background:transparent;border:1px solid transparent;border-bottom:none;border-radius:8px 8px 0 0;
  color:var(--muted);padding:8px 16px}
.tab b{color:var(--text);font-weight:600;margin-left:6px;font-size:12px}
.tab.active{background:var(--panel);border-color:var(--line);color:var(--text)}
main{padding:14px 22px 60px;max-width:1500px;margin:0 auto}
.panel{display:none;animation:fade .18s ease}
.panel.active{display:block}
@keyframes fade{from{opacity:.35;transform:translateY(3px)}to{opacity:1;transform:none}}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.toolbar input.search{max-width:300px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--panel2);color:var(--muted);border-radius:20px;
  padding:4px 12px;font-size:12px;cursor:pointer}
.chip.on{color:var(--accent);border-color:rgba(61,219,192,.45);background:rgba(61,219,192,.1)}
.tablewrap{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:auto}
table{width:100%;border-collapse:collapse;min-width:760px}
th{position:sticky;top:0;background:#171d26;color:var(--muted);text-align:left;font-weight:600;
  font-size:12px;letter-spacing:.2px;padding:10px 12px;border-bottom:1px solid var(--line);z-index:2}
td{padding:9px 12px;border-bottom:1px solid rgba(36,45,58,.6);vertical-align:middle}
tbody tr{cursor:pointer}
tbody tr:hover{background:#1a2029}
tbody tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.muted{color:var(--muted)}
.pill{display:inline-block;border-radius:20px;padding:2px 9px;font-size:11.5px;font-weight:600;
  border:1px solid transparent;white-space:nowrap}
.s-pending{color:#9aa4b2;background:rgba(154,164,178,.12);border-color:rgba(154,164,178,.3)}
.s-starting,.s-running{color:var(--blue);background:rgba(106,168,255,.12);border-color:rgba(106,168,255,.35)}
.s-succeeded{color:var(--ok);background:rgba(62,207,142,.12);border-color:rgba(62,207,142,.35)}
.s-failed{color:var(--bad);background:rgba(255,107,107,.12);border-color:rgba(255,107,107,.35)}
.s-timeout{color:var(--warn);background:rgba(240,180,41,.12);border-color:rgba(240,180,41,.35)}
.s-killed{color:var(--purple);background:rgba(177,138,255,.12);border-color:rgba(177,138,255,.35)}
.s-paused{color:#ffb86b;background:rgba(255,184,107,.12);border-color:rgba(255,184,107,.35)}
.s-done{color:var(--ok);background:rgba(62,207,142,.12);border-color:rgba(62,207,142,.35)}
.s-skipped{color:#8f9bb0;background:rgba(143,155,176,.1);border-color:rgba(143,155,176,.25)}
.empty{padding:44px 20px;text-align:center;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h3{margin:0 0 10px;font-size:14px;font-weight:650}
.card .field{margin-bottom:10px}
.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.hint{font-size:11.5px;color:var(--muted);margin-top:5px;line-height:1.45}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.card .pf{margin-top:10px;padding-top:8px;border-top:1px dashed var(--line)}
.card .pf summary{cursor:pointer;font-size:12.5px;font-weight:600}
.pill.s-denied_injection{color:var(--bad);border-color:rgba(255,107,107,.45);background:rgba(255,107,107,.12)}
pre.log{background:#0b0f15;border:1px solid var(--line);border-radius:8px;padding:12px;
  font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:1.55;color:#c8d2e0;
  max-height:46vh;overflow:auto;white-space:pre-wrap;word-break:break-all;margin:0}
.appr{border-left:3px solid var(--warn)}
.appr .cmd{font-family:ui-monospace,Consolas,monospace;font-size:13px;background:#0f141b;
  border:1px solid var(--line);border-radius:6px;padding:9px 11px;margin:8px 0;word-break:break-all}
.meta{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.meta b{color:var(--text);font-weight:600}
.events{display:flex;flex-direction:column;gap:8px}
.ev{display:flex;gap:12px;align-items:flex-start;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:9px 12px}
.ev .t{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--muted);white-space:nowrap;padding-top:2px}
.ev .type{font-weight:650;color:var(--accent);white-space:nowrap}
.ev .body{color:var(--muted);font-size:12px;word-break:break-all}
.drawer{position:fixed;top:0;right:0;width:min(620px,94vw);height:100vh;background:#11161e;
  border-left:1px solid var(--line);z-index:40;transform:translateX(102%);transition:transform .22s ease;
  display:flex;flex-direction:column;box-shadow:-20px 0 60px rgba(0,0,0,.45)}
.drawer.open{transform:none}
.drawer-head{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:14px 18px;border-bottom:1px solid var(--line)}
.drawer-head h2{margin:0;font-size:15px}
.drawer-body{padding:16px 18px;overflow:auto;flex:1}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:35;display:none}
.overlay.on{display:block}
.tabsline{display:flex;gap:6px;margin:12px 0}
.tabsline button{padding:4px 11px;font-size:12px}
.tabsline button.on{color:var(--accent);border-color:rgba(61,219,192,.45);background:rgba(61,219,192,.1)}
.toasts{position:fixed;bottom:18px;right:18px;z-index:60;display:flex;flex-direction:column;gap:8px;max-width:420px}
.toast{background:#1b222c;border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:8px;padding:10px 14px;font-size:13px;box-shadow:0 8px 30px rgba(0,0,0,.4);animation:fade .2s}
.toast.bad{border-left-color:var(--bad)} .toast.warn{border-left-color:var(--warn)}
.section-title{font-size:12px;color:var(--muted);letter-spacing:.4px;margin:16px 0 8px}
.steps{display:flex;flex-direction:column;gap:6px}
.step{display:flex;align-items:center;gap:10px;background:var(--panel2);border:1px solid var(--line);
  border-radius:7px;padding:7px 10px;font-size:12.5px}
.step .sid{font-family:ui-monospace,Consolas,monospace;color:var(--muted);font-size:11.5px}
.mcpbar{margin:10px 22px 0;border:1px solid var(--line);border-left:3px solid var(--muted);
  border-radius:9px;padding:10px 14px;font-size:12.5px;background:var(--panel2)}
.mcpbar.running{border-left-color:var(--ok);background:rgba(62,207,142,.05)}
.mcpbar.failed{border-left-color:var(--bad);background:rgba(255,107,107,.06)}
.mcpbar .row{display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.mcpbar .spacer{flex:1}
.mcpbar .hint{margin-top:6px;color:var(--muted);word-break:break-all}
.mcpbar .hint.bad{color:var(--bad)}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}main{padding:12px 12px 40px}header{padding:10px 12px} .mcpbar{margin:10px 12px 0}}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="logo">ATS</div>
    <div><h1>调度器控制台</h1><div class="sub" id="rootPath">-</div></div>
  </div>
  <div class="head-right">
    <span class="live" id="liveDot"></span><span class="sub" id="liveText">连接中</span>
    <select id="interval" style="width:auto">
      <option value="2000">2 秒</option><option value="3000" selected>3 秒</option>
      <option value="5000">5 秒</option><option value="10000">10 秒</option>
    </select>
    <button id="btnRefresh">刷新</button>
  </div>
</header>

<div class="banner" id="banner"></div>

<section class="kpis" id="kpis"></section>
<section class="mcpbar" id="mcpBar">
  <div class="row">
    <span class="pill s-pending" id="mcpState">未启动</span>
    <span class="sub">端点 <b id="mcpAddr">-</b></span>
    <span class="sub">客户端 <b id="mcpClients">0</b></span>
    <span class="sub" id="mcpStarted"></span>
    <span class="spacer"></span>
    <button class="primary" id="btnMcpToggle">开启 MCP</button>
    <button id="btnMcpCopy">复制客户端配置</button>
  </div>
  <div class="hint" id="mcpHint">正在读取 MCP 状态…</div>
</section>
<nav class="tabs">
  <button class="tab active" data-tab="tasks">任务 <b id="cntTasks">0</b></button>
  <button class="tab" data-tab="plans">计划 <b id="cntPlans">0</b></button>
  <button class="tab" data-tab="approvals">审批 <b id="cntApprovals">0</b></button>
  <button class="tab" data-tab="events">事件</button>
  <button class="tab" data-tab="new">新建</button>
</nav>

<main>
  <section class="panel active" id="panel-tasks">
    <div class="toolbar">
      <input class="search" id="taskSearch" placeholder="搜索 ID / 命令 / 描述">
      <div class="chips" id="taskChips"></div>
      <span class="sub" id="taskCount"></span>
    </div>
    <div class="tablewrap"><table>
      <thead><tr><th>状态</th><th>ID</th><th>等级</th><th>命令</th><th>退出</th><th>用时</th><th>创建</th></tr></thead>
      <tbody id="taskBody"></tbody>
    </table></div>
  </section>

  <section class="panel" id="panel-plans">
    <div class="toolbar"><span class="sub">计划与周期（点击行查看明细）</span></div>
    <div class="tablewrap"><table>
      <thead><tr><th>ID</th><th>类型</th><th>状态</th><th>运行</th><th>下次</th><th>当前 Plan</th><th>步数</th></tr></thead>
      <tbody id="planBody"></tbody>
    </table></div>
  </section>

  <section class="panel" id="panel-approvals">
    <div class="toolbar">
      <label class="sub"><input type="checkbox" id="showAllApprovals" style="width:auto;vertical-align:middle"> 显示已决议</label>
      <span class="sub" id="approvalCount"></span>
    </div>
    <div class="card" style="margin-bottom:12px">
      <h3>审批策略</h3>
      <div class="grid2">
        <div class="field"><label>全局模式</label><select id="modeGlobal"></select></div>
        <div class="field"><label>L0</label><select class="modeLevel" id="modeL0"></select></div>
        <div class="field"><label>L1</label><select class="modeLevel" id="modeL1"></select></div>
        <div class="field"><label>L2</label><select class="modeLevel" id="modeL2"></select></div>
        <div class="field"><label>L3</label><select class="modeLevel" id="modeL3"></select></div>
      </div>
      <div class="actions">
        <label class="sub"><input type="checkbox" id="polOverride"> 允许 denied_injection 强制放行</label>
        <label class="sub"><input type="checkbox" id="polDry"> 启用诱饵试跑</label>
        <label class="sub"><input type="checkbox" id="polIso"> 启用 Win32 隔离（Job Object kill-on-close；非强安全边界）</label>
      </div>
      <div class="actions"><button class="primary" id="btnSavePolicy">保存审批策略</button>
        <span class="hint" id="policyHint"></span></div>
      <div class="hint">生效方式：下一次 run 立即生效（policy.json 热读）。</div>
    </div>
    <div class="cards" id="approvalBody"></div>
  </section>

  <section class="panel" id="panel-events">
    <div class="section-title">后台操作（本界面发起）</div>
    <div class="events" id="jobBody"></div>
    <div class="section-title">事件流</div>
    <div class="events" id="eventBody"></div>
  </section>

  <section class="panel" id="panel-new">
    <div class="cards">
      <div class="card">
        <h3>新建任务</h3>
        <div class="field"><label>命令（末尾可带 @enc 钩子）</label>
          <textarea id="tCommand" rows="3" placeholder="make build @enc --from gbk --to utf-8 build\logs"></textarea></div>
        <div class="grid2">
          <div class="field"><label>任务 ID（可空）</label><input id="tId"></div>
          <div class="field"><label>等级</label><select id="tLevel"><option>L1</option><option>L0</option><option>L2</option><option>L3</option></select></div>
        </div>
        <div class="grid2">
          <div class="field"><label>工作目录（可空）</label><input id="tCwd"></div>
          <div class="field"><label>超时（秒，0 不限）</label><input id="tTimeout" type="number" value="0"></div>
        </div>
        <div class="field"><label>描述（可空）</label><input id="tDesc"></div>
        <div class="field"><label>环境变量（每行 K=V，可空）</label><textarea id="tEnv" rows="2"></textarea></div>
        <div class="actions"><button class="primary" id="btnCreateTask">创建任务</button></div>
        <div class="hint">创建后需在任务列表中运行；@enc 只在主命令退出码为 0 时执行。</div>
      </div>

      <div class="card">
        <h3>加载指令包</h3>
        <div class="field"><label>命令（每行一条，仅登记不执行）</label>
          <textarea id="pCommands" rows="4" placeholder="cmd one&#10;cmd two"></textarea></div>
        <div class="grid2">
          <div class="field"><label>Pack ID（可空）</label><input id="pId"></div>
          <div class="field"><label>等级</label><select id="pLevel"><option>L1</option><option>L0</option><option>L2</option><option>L3</option></select></div>
        </div>
        <div class="grid2">
          <div class="field"><label>工作目录（可空）</label><input id="pCwd"></div>
          <div class="field"><label>超时（秒）</label><input id="pTimeout" type="number" value="0"></div>
        </div>
        <div class="field"><label>描述（可空）</label><input id="pDesc"></div>
        <div class="actions"><button class="primary" id="btnCreatePack">加载指令包</button></div>
        <div class="hint">每行会按英文逗号拼成 CLI 的 commands 参数；条目中含英文逗号时请改用 CLI。</div>
      </div>

      <div class="card">
        <h3>提交 Plan</h3>
        <div class="field"><label>Plan spec（JSON）</label>
          <textarea id="planSpec" rows="8" placeholder='{"stages":[{"id":"s1","steps":[{"id":"a","command":"echo hi"}]}]}'></textarea></div>
        <div class="grid2">
          <div class="field"><label>Plan ID（可空）</label><input id="planId"></div>
          <div class="field"><label>提交后</label><input value="在“计划”页执行" disabled></div>
        </div>
        <div class="actions"><button class="primary" id="btnSubmitPlan">提交 Plan</button></div>
        <div class="hint">一次性计划提交后用 run_plan 执行；周期计划提交后用“驱动周期”触发。</div>
      </div>
    </div>
  </section>
</main>

<div class="overlay" id="overlay"></div>
<aside class="drawer" id="drawer">
  <div class="drawer-head">
    <h2 id="drawerTitle">详情</h2>
    <div><button class="ghost" id="btnCloseDrawer">关闭</button></div>
  </div>
  <div class="drawer-body" id="drawerBody"></div>
</aside>
<div class="toasts" id="toasts"></div>

<script>
const SRV = __SRV__;
const $ = s => document.querySelector(s);
let DATA = null, TAB = 'tasks', SEL_TASK = null, SEL_PLAN = null, LOGFILE = 'stdout';
let FILTER_STATUS = 'all', TIMER = null, TICK = 0;

function esc(v){ return String(v==null?'':v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toSec(v){
  if(v==null || v==='') return 0;
  if(typeof v==='number') return v>1e12 ? Math.floor(v/1000) : Math.floor(v);
  const s=String(v).trim();
  if(!s) return 0;
  if(/^-?\d+(\.\d+)?$/.test(s)) return Math.floor(Number(s));
  const d=new Date(s);
  return isNaN(d.getTime()) ? 0 : Math.floor(d.getTime()/1000);
}
function ts(t){ const v=toSec(t); return v ? new Date(v*1000).toLocaleString('zh-CN',{hour12:false}) : '-'; }
function dur(a,b,c){
  const s=toSec(a); if(!s) return '-';
  const running = (c==='running' || c==='starting');
  const e=toSec(b);
  if(!e && !running) return '-';   // 已结束但没有结束时间（旧记录）
  const end=e || Math.floor(Date.now()/1000);
  const d=Math.max(0,end-s);
  if(d<60) return d+'s';
  if(d<3600) return Math.floor(d/60)+'m '+(d%60)+'s';
  return Math.floor(d/3600)+'h '+Math.floor((d%3600)/60)+'m';
}
function pill(s){ return '<span class="pill s-'+esc(s||'pending')+'">'+esc(s||'-')+'</span>'; }
function toast(msg, kind){ const el=document.createElement('div'); el.className='toast '+(kind||''); el.textContent=msg; $('#toasts').appendChild(el); setTimeout(()=>{el.style.opacity='0';el.style.transition='opacity .3s';setTimeout(()=>el.remove(),350)},4200); }
function errText(d){ const r=d.result||{}; let m=r.error||r.reason||d.message||('退出码 '+d.exit_code); if(r.hint) m+='（'+r.hint+'）'; if(r.approval_id) m+='，审批 '+r.approval_id; return m; }

async function api(path, opts, timeoutMs){
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs || 25000);
  try{
    const r = await fetch(path, Object.assign({cache:'no-store', signal:ctl.signal}, opts||{}));
    let data = null;
    try{ data = await r.json(); }catch(e){}
    if(!r.ok && !data) throw new Error('HTTP '+r.status);
    return data;
  }catch(e){
    if(e.name === 'AbortError') throw new Error('请求超时（'+Math.round((timeoutMs||25000)/1000)+' 秒）');
    throw e;
  }finally{ clearTimeout(timer); }
}

function setLive(kind, text){
  const dot = $('#liveDot');
  dot.classList.toggle('off', kind !== 'ok');
  dot.classList.toggle('warn', kind === 'warn');
  $('#liveText').textContent = text;
}

function showBanner(html, kind){
  const b = $('#banner');
  b.className = 'banner ' + (kind || 'bad');
  b.innerHTML = html;
  b.style.display = 'block';
}
function hideBanner(){ $('#banner').style.display = 'none'; }

function bootCommand(root){
  return 'python "' + (SRV.script || 'ats_ui.py') + '" --root "' + (root || SRV.root || '') + '" --ensure --open';
}

function bannerWarn(msg){
  showBanner('<b>数据异常</b>：' + esc(msg)
    + '<div class="hint">后端仍可达；可点“刷新”重试，或查看 <code>' + esc(SRV.root + '\\ui_tmp\\ui.log') + '</code></div>', 'warn');
}

async function pingOnce(){
  try{ const r = await fetch('/api/ping', {cache:'no-store'}); return r.ok ? await r.json() : null; }
  catch(e){ return null; }
}

function renderSafe(){
  try{ render(); }
  catch(e){
    setLive('warn', '渲染异常');
    bannerWarn('界面渲染异常：' + e.message + '（后端仍可达，数据可能不完整）');
    console.error(e);
  }
}

async function refresh(){
  let d;
  try{
    d = await api('/api/state', {}, 25000);
  }catch(e){
    const p = await pingOnce();
    if(p){
      setLive('warn', '状态读取失败');
      bannerWarn('后端已连接，但读取状态失败：' + e.message);
    }else{
      setLive('bad', '后端不可达');
      const root = (DATA && DATA.root) || SRV.root || '';
      $('#banner').className = 'banner bad';
      $('#banner').innerHTML = '<b>后端不可达</b>：控制台服务未运行、已退出，或页面地址与端口不一致。'
        + '<div class="hint">一键启动（未运行时会自动拉起并打开浏览器）：<code>' + esc(bootCommand(root)) + '</code>'
        + ' <button class="ghost" id="btnCopyBoot">复制命令</button></div>'
        + '<div class="hint">启动日志：<code>' + esc(root + '\\ui_tmp\\ui.log') + '</code></div>';
      $('#banner').style.display = 'block';
      const cb = $('#btnCopyBoot');
      if(cb) cb.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(bootCommand(root)); toast('启动命令已复制','ok'); };
    }
    return;
  }
  if(!d || d.error){
    setLive('warn', '状态异常');
    bannerWarn('状态接口返回异常：' + ((d && (d.message || d.error)) || '空响应'));
    return;
  }
  DATA = d; TICK++;
  setLive('ok', '实时 · ' + new Date().toLocaleTimeString('zh-CN',{hour12:false}));
  if((d.errors || []).length){
    bannerWarn('部分数据读取异常：' + d.errors.map(x => x.scope + '：' + x.message).join('；'));
  }else{
    hideBanner();
  }
  renderSafe();
}
function restartTimer(){ if(TIMER) clearInterval(TIMER); const ms=+$('#interval').value; TIMER=setInterval(refresh, ms); }

function render(){
  if(!DATA) return;
  const c = DATA.counts || {};
  $('#rootPath').textContent = DATA.root;
  $('#kpis').innerHTML = [
    ['任务', c.tasks, ''], ['运行中', c.running, 'hot'], ['失败', c.failed, c.failed?'bad':''],
    ['待审批', c.pending_approvals, c.pending_approvals?'warn':''], ['计划/周期', c.schedules, '']
  ].map(([l,v,cls]) => '<div class="kpi '+cls+'"><div class="v">'+esc(v)+'</div><div class="l">'+l+'</div></div>').join('');
  $('#cntTasks').textContent = c.tasks || 0;
  $('#cntPlans').textContent = c.schedules || 0;
  $('#cntApprovals').textContent = c.pending_approvals || 0;
  renderTasks(); renderPlans(); renderApprovals(); renderEvents(); renderMcp(); renderPolicyControls();
  if(SEL_TASK && TICK % 3 === 0) loadTask(true);
  if(SEL_PLAN && TICK % 3 === 0) loadPlan(true);
}

function taskMatches(t){
  const q = $('#taskSearch').value.trim().toLowerCase();
  const s = FILTER_STATUS;
  if(s !== 'all' && t.status !== s) return false;
  if(!q) return true;
  return [t.id,t.command,t.description].some(v => String(v||'').toLowerCase().includes(q));
}
function renderMcp(){
  const M = DATA.mcp || {status:'stopped'};
  const st = M.status || 'stopped';
  const bar = $('#mcpBar');
  bar.className = 'mcpbar ' + (st==='running' ? 'running' : (st==='failed' ? 'failed' : ''));
  $('#mcpState').className = 'pill ' + (st==='running' ? 's-running' : (st==='failed' ? 's-failed' : 's-pending'));
  $('#mcpState').textContent = st==='running' ? '运行中' : (st==='failed' ? '启动失败' : '未启动');
  $('#mcpAddr').textContent = M.url || '-';
  $('#mcpClients').textContent = String(M.sessions==null ? 0 : M.sessions);
  $('#mcpStarted').textContent = (st==='running' && M.started_at) ? '启动于 '+ts(M.started_at) : '';
  $('#btnMcpToggle').textContent = st==='running' ? '关闭 MCP' : (st==='failed' ? '重试开启' : '开启 MCP');
  $('#btnMcpToggle').className = st==='running' ? 'danger' : 'primary';
  $('#btnMcpCopy').disabled = !M.config;
  const hint = $('#mcpHint');
  hint.className = 'hint' + (M.error ? ' bad' : '');
  if(st==='failed'){
    hint.textContent = '启动失败：' + (M.error || '原因未知');
  }else if(st==='running'){
    hint.textContent = '只监听 127.0.0.1，可随时关闭并释放端口。开启期间，本机任意进程都能通过它执行命令。';
  }else if(!M.available){
    hint.textContent = M.error || 'MCP 模块未加载';
  }else{
    hint.textContent = '默认关闭。开启后本机 MCP 客户端可连接 ' + (M.url||'-')
      + '；这等于把「执行命令」能力暴露给本机任意进程。';
  }
}

function mcpConfigText(){
  const M = DATA.mcp || {};
  return M.config ? JSON.stringify(M.config, null, 2) : '';
}

function fallbackCopy(text, done){
  try{
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done();
  }catch(e){ toast('复制失败：'+e.message, 'bad'); }
}

function mcpCopy(){
  const text = mcpConfigText();
  if(!text) return toast('MCP 端点信息未就绪，暂无可复制的配置', 'bad');
  const done = () => toast('客户端配置已复制：'+((DATA.mcp||{}).url||''), 'ok');
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  }else{
    fallbackCopy(text, done);
  }
}

async function mcpToggle(){
  const st = (DATA && DATA.mcp && DATA.mcp.status) || 'stopped';
  if(st === 'running'){
    if(!confirm('关闭 MCP 端点？已连接的客户端会断开，端口随即释放。')) return;
    try{
      await api('/api/action', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'mcp.stop'})}, 60000);
    }catch(e){ toast('请求失败：'+e.message, 'bad'); }
    refresh();
    return;
  }
  const url = ((DATA.mcp||{}).url) || '该地址';
  const ok = confirm('开启 MCP HTTP 端点？\n\n这等同于把「执行命令」的能力暴露给本机任意进程：'
    + '本机任何程序都能通过 ' + url + ' 创建并运行任务。\n\n只监听 127.0.0.1，关闭时立即释放端口。');
  if(!ok) return;
  try{
    const d = await api('/api/action', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'mcp.start', confirm:true})}, 60000);
    if(d.ok) toast('MCP 端点已开启：'+((d.mcp||{}).url||''), 'ok');
    else toast('MCP 启动失败：'+((d.mcp||{}).error || d.message || '原因未知'), 'bad');
  }catch(e){ toast('请求失败：'+e.message, 'bad'); }
  refresh();
}

function renderTasks(){
  const tasks = (DATA.tasks||[]);
  const statuses = ['all','running','succeeded','failed','done','pending','killed','timeout'];
  $('#taskChips').innerHTML = statuses.map(s =>
    '<button class="chip '+(FILTER_STATUS===s?'on':'')+'" data-status="'+s+'">'+(s==='all'?'全部':s)+'</button>').join('');
  $('#taskChips').querySelectorAll('.chip').forEach(b => b.onclick = () => { FILTER_STATUS = b.dataset.status; renderTasks(); });
  const rows = tasks.filter(taskMatches);
  $('#taskCount').textContent = rows.length + ' / ' + tasks.length;
  $('#taskBody').innerHTML = rows.length ? rows.map(t => {
    const hook = t.post_hook ? ' <span class="pill s-'+esc(t.post_hook.status||'pending')+'">@enc '+esc(t.post_hook.status||'待执行')+'</span>' : '';
    return '<tr data-id="'+esc(t.id)+'">'
      + '<td>'+pill(t.status)+'</td>'
      + '<td class="mono">'+esc(t.id)+'</td>'
      + '<td>'+esc(t.level||'-')+'</td>'
      + '<td class="mono">'+esc((t.command||'').slice(0,120))+(t.command&&t.command.length>120?'…':'')+hook+'</td>'
      + '<td>'+esc(t.exit_code==null?'-':t.exit_code)+'</td>'
      + '<td>'+esc(dur(t.started_at,t.ended_at,t.status))+'</td>'
      + '<td class="muted">'+esc(ts(t.created_at))+'</td></tr>';
  }).join('') : '<tr><td colspan="7"><div class="empty">没有匹配的任务</div></td></tr>';
  $('#taskBody').querySelectorAll('tr[data-id]').forEach(tr => tr.onclick = () => openTask(tr.dataset.id));
}

function renderPlans(){
  const rows = DATA.schedules || [];
  $('#planBody').innerHTML = rows.length ? rows.map(s => {
    const counts = s.counts || {};
    const ctext = Object.keys(counts).map(k => esc(k)+':'+counts[k]).join(' ') || '-';
    return '<tr data-ident="'+esc(s.schedule_id)+'">'
      + '<td class="mono">'+esc(s.schedule_id)+'</td>'
      + '<td>'+esc(s.type||'-')+'</td>'
      + '<td>'+pill(s.status)+'</td>'
      + '<td>'+esc((s.runs==null?'-':s.runs)+(s.max_runs?' / '+s.max_runs:''))+'</td>'
      + '<td class="muted">'+esc(s.next_run?ts(s.next_run):'-')+'</td>'
      + '<td class="mono">'+pill(s.plan_status||'none')+' '+esc(s.current_plan||'')+'</td>'
      + '<td class="muted">'+ctext+'</td></tr>';
  }).join('') : '<tr><td colspan="7"><div class="empty">暂无计划或周期</div></td></tr>';
  $('#planBody').querySelectorAll('tr[data-ident]').forEach(tr => tr.onclick = () => openPlan(tr.dataset.ident));
}

function preflightHtml(a){
  const pf = a.preflight;
  if(!pf || typeof pf !== 'object') return '';
  const dr = pf.dry_run || null;
  const st = pf.static || {};
  const ev = (st.evidence || []).slice(0, 6);
  const rows = [
    ['判决', pf.verdict || '-'],
    ['置信度', pf.confidence || '-'],
    ['标签', (pf.tags||[]).join(', ') || '无'],
    ['原因', pf.reason || '-'],
    ['阶段', pf.status || '-'],
    ['耗时', pf.duration_ms==null ? '-' : pf.duration_ms+' ms']
  ];
  if(dr){
    rows.push(['试跑', dr.status || '-']);
    rows.push(['退出码', dr.exit_code==null ? '-' : String(dr.exit_code)]);
    rows.push(['超时', dr.timed_out ? '是' : '否']);
    rows.push(['哨兵被改', dr.sentinel_touched ? '是' : '否']);
    rows.push(['隔离', dr.job_mode || '-']);
    rows.push(['残留目录', dr.leftover ? '有' : '无']);
    if((dr.sentinels||[]).length){
      rows.push(['哨兵明细', dr.sentinels.map(x => x.path+':'+x.change).join(', ')]);
    }
  }
  return '<details class="pf"><summary>preflight 证据</summary>'
    + '<div class="meta" style="margin-top:8px;gap:10px">'
    + rows.map(([k,v]) => '<span>'+esc(k)+' <b>'+esc(v)+'</b></span>').join('')
    + '</div>'
    + (ev.length ? '<div class="hint mono" style="margin-top:8px">'
        + ev.map(e => esc((e.file||'')+(e.line?'#'+e.line:'')+' '+e.detail)).join('<br>') + '</div>' : '')
    + '</details>';
}

function renderApprovals(){
  const all = (DATA.approvals||[]);
  const showAll = $('#showAllApprovals').checked;
  const rows = all.filter(a => showAll || ((a.status==='pending' || a.status==='denied_injection') && !a._expired));
  $('#approvalCount').textContent = rows.length + ' 条';
  const allowOverride = !!(DATA.policy && DATA.policy.denied_injection_allow_override);
  $('#approvalBody').innerHTML = rows.length ? rows.map(a => {
    const inj = !!(a.preflight && a.preflight.verdict==='deny_injection');
    const open = (a.status==='pending' || a.status==='denied_injection') && !a._expired;
    const can = a.status==='pending' && !a._expired;
    const overrideBtn = open && inj ? '<button class="danger" data-override="'+esc(a.id)+'"'
      + (allowOverride?'':' disabled')+'>强制放行（需备注）</button>' : '';
    return '<div class="card appr"><div class="meta">'+pill(a.status)+(a._expired?' '+pill('expired'):'')
      + '<span>等级 <b>'+esc(a.required_level||'-')+'</b></span>'
      + '<span>来源 <b>'+esc(a.source||'-')+'</b></span>'
      + '<span>目标 <b>'+esc(a.task_id||a.plan_id||a.schedule_id||'-')+'</b></span></div>'
      + '<div class="cmd">'+esc(a.command||'-')+'</div>'
      + '<div class="meta"><span>创建 '+esc(ts(a.created_at))+'</span><span>过期 '+esc(a.expires_at?ts(a.expires_at):'不限')+'</span>'
      + '<span class="mono">'+esc(a.id)+'</span></div>'
      + preflightHtml(a)
      + (can?'<div class="actions"><button class="primary" data-approve="'+esc(a.id)+'">批准</button>'
           +'<button class="danger" data-reject="'+esc(a.id)+'">拒绝</button></div>':'')
      + ((!can && open && inj) ? '<div class="actions"><button class="danger" data-reject="'+esc(a.id)+'">拒绝</button></div>' : '')
      + overrideBtn
      + (inj && !allowOverride ? '<div class="hint bad">当前策略禁止人工放行 denied_injection。</div>' : '')
      + (inj && a.status==='denied_injection' ? '<div class="hint">该记录已在策略上被拒；放行需先开启审批策略开关。</div>' : '')
      + (a.resume?'<div class="hint">批准后恢复：<span class="mono">'+esc(a.resume)+'</span></div>':'')
      + '</div>';
  }).join('') : '<div class="empty">没有待处理审批</div>';
  $('#approvalBody').querySelectorAll('[data-approve]').forEach(b => b.onclick = () => act('approval.resolve',{ids:[b.dataset.approve]},'批准 '+b.dataset.approve+'？'));
  $('#approvalBody').querySelectorAll('[data-reject]').forEach(b => b.onclick = () => {
    const note = prompt('拒绝原因（可空）：','');
    if(note===null) return;
    act('approval.resolve',{ids:[b.dataset.reject],reject:true,note},'拒绝 '+b.dataset.reject+'？');
  });
  $('#approvalBody').querySelectorAll('[data-override]').forEach(b => b.onclick = () => {
    const note = prompt('强制放行备注（必填，将写入审批记录）：','');
    if(note===null) return;
    if(!note.trim()) return toast('强制放行必须填写备注','bad');
    act('approval.resolve',{ids:[b.dataset.override],override:true,note},'确认强制放行 '+b.dataset.override+'？此操作会忽略预演证据并执行命令。');
  });
}

const APPROVAL_MODES = [['auto','auto（按身份直通）'],['ask','ask（人工审批）'],['deny-only','deny-only（直接拒绝）']];
let POLICY_SIG = '';

function fillModeSelect(el, val){
  if(!el) return;
  if(!el.options.length){
    APPROVAL_MODES.forEach(([v,t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t; el.appendChild(o);
    });
  }
  el.value = val || 'ask';
}

function renderPolicyControls(){
  const p = DATA && DATA.policy;
  if(!p || !$('#modeGlobal')) return;
  const byLevel = p.approval_mode_by_level || {};
  const sig = JSON.stringify([p.approval_mode, byLevel, p.denied_injection_allow_override,
    p.preflight_dry_run, p.win32_isolation]);
  const editing = document.activeElement && document.activeElement.closest
    && document.activeElement.closest('#panel-approvals');
  if(sig === POLICY_SIG || editing) return;
  POLICY_SIG = sig;
  fillModeSelect($('#modeGlobal'), p.approval_mode);
  ['L0','L1','L2','L3'].forEach(l => fillModeSelect($('#mode'+l), byLevel[l] || p.approval_mode));
  $('#polOverride').checked = !!p.denied_injection_allow_override;
  $('#polDry').checked = !!p.preflight_dry_run;
  $('#polIso').checked = !!p.win32_isolation;
}

async function savePolicy(){
  const patch = {
    approval_mode: $('#modeGlobal').value,
    approval_mode_by_level: {L0:$('#modeL0').value,L1:$('#modeL1').value,
      L2:$('#modeL2').value,L3:$('#modeL3').value},
    denied_injection_allow_override: $('#polOverride').checked,
    preflight_dry_run: $('#polDry').checked,
    win32_isolation: $('#polIso').checked
  };
  $('#policyHint').textContent = '保存中…';
  try{
    const d = await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'policy.set',patch})}, 60000);
    if(d.ok){
      POLICY_SIG = '';
      $('#policyHint').textContent = '已保存 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
      toast('审批策略已保存','ok');
      refresh();
    }else{
      $('#policyHint').textContent = '保存失败';
      toast(errText(d),'bad');
    }
  }catch(e){
    $('#policyHint').textContent = '保存失败';
    toast('请求失败：'+e.message,'bad');
  }
}

function renderEvents(){
  const jobs = DATA.jobs||[];
  $('#jobBody').innerHTML = jobs.length ? jobs.map(j =>
    '<div class="ev"><div class="t">'+esc(ts(j.started_at))+'</div><div class="type">'+esc(j.action)+'</div>'
    + '<div class="body">'+esc(j.label)+(j.note?' · '+esc(j.note):'')+' · <b>'+esc(j.status)+'</b></div></div>').join('')
    : '<div class="empty">本界面尚未发起操作</div>';
  const evs = DATA.events||[];
  $('#eventBody').innerHTML = evs.length ? evs.map(e => {
    const rest = Object.assign({}, e); delete rest.ts; delete rest.iso; delete rest.type;
    const keys = Object.keys(rest).filter(k => rest[k]!=null && typeof rest[k]!=='object').slice(0,6);
    const body = keys.map(k => k+'='+rest[k]).join(' · ');
    return '<div class="ev"><div class="t">'+esc(e.iso||ts(e.ts))+'</div><div class="type">'+esc(e.type||'-')+'</div><div class="body">'+esc(body)+'</div></div>';
  }).join('') : '<div class="empty">暂无事件</div>';
}

async function act(action, payload, confirmMsg){
  if(confirmMsg && !confirm(confirmMsg)) return;
  try{
    const d = await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify(Object.assign({action}, payload))}, 120000);
    if(d.async){ toast(d.message||'已提交后台执行','ok'); }
    else if(d.ok){ toast('操作完成：'+action,'ok'); }
    else {
      toast(errText(d),'bad');
      const r = d.result||{};
      if(r.approval_id){ toast('已创建审批 '+r.approval_id+'，批准后再试','warn'); showTab('approvals'); }
    }
    refresh();
  }catch(e){ toast('请求失败：'+e.message,'bad'); }
}

function showTab(name){
  TAB = name;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab===name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id==='panel-'+name));
}

async function openTask(id){
  SEL_TASK = id; LOGFILE = 'stdout'; $('#drawer').classList.add('open'); $('#overlay').classList.add('on');
  $('#drawerTitle').textContent = '任务 ' + id;
  $('#drawerBody').innerHTML = '<div class="empty">加载中…</div>';
  await loadTask(false);
}
async function loadTask(quiet){
  if(!SEL_TASK) return;
  try{
    const d = await api('/api/task?id='+encodeURIComponent(SEL_TASK));
    if(!d.task){ if(!quiet) $('#drawerBody').innerHTML='<div class="empty">任务不存在</div>'; return; }
    const t = d.task, h = t.post_hook;
    const status = t.status;
    const canRun = ['pending','failed','timeout','succeeded','killed'].includes(status);
    const canStop = ['starting','running'].includes(status);
    $('#drawerBody').innerHTML =
      '<div class="meta" style="margin-bottom:10px">'+pill(status)
      + '<span>等级 <b>'+esc(t.level)+'</b></span><span>退出 <b>'+esc(t.exit_code==null?'-':t.exit_code)+'</b></span>'
      + '<span>用时 <b>'+esc(dur(t.started_at,t.ended_at,t.status))+'</b></span><span>PID <b>'+esc(t.pid||'-')+'</b></span></div>'
      + '<pre class="log" style="max-height:12vh;margin-bottom:12px">'+esc(t.command||'')+'</pre>'
      + (h?'<div class="card" style="padding:10px;margin-bottom:12px"><div class="meta">'+pill(h.status||'pending')
          + '<span>@enc 钩子</span><span>退出 <b>'+esc(h.exit_code==null?'-':h.exit_code)+'</b></span>'
          + (h.error_type?'<span>错误 <b>'+esc(h.error_type)+'</b></span>':'')+'</div>'
          + '<div class="mono muted" style="margin-top:6px">'+esc(h.args||'')+'</div></div>':'')
      + '<div class="actions" style="margin-bottom:12px">'
      + (canRun?'<button class="primary" id="dRun">运行</button>':'')
      + (canStop?'<button class="danger" id="dStop">停止</button>':'')
      + (status!=='running'?'<button id="dRetry">重试</button>':'')
      + (status!=='running'?'<button class="danger" id="dDelete">删除</button>':'')
      + '<button id="dCopy">复制命令</button></div>'
      + '<div class="tabsline">'+['stdout','stderr','hook','supervisor'].map(f =>
          '<button data-log="'+f+'" class="'+(LOGFILE===f?'on':'')+'">'+f+'</button>').join('')+'</div>'
      + '<pre class="log" id="logBox">加载中…</pre>';
    $('#dRun') && ($('#dRun').onclick = () => act('task.run',{task_id:t.id},'运行 '+t.id+'？'));
    $('#dStop') && ($('#dStop').onclick = () => act('task.stop',{task_id:t.id},'停止 '+t.id+'？'));
    $('#dRetry') && ($('#dRetry').onclick = () => act('task.retry',{task_id:t.id},'重试 '+t.id+'？'));
    $('#dDelete') && ($('#dDelete').onclick = () => act('task.delete',{task_id:t.id},'删除 '+t.id+'（不可恢复）？'));
    $('#dCopy') && ($('#dCopy').onclick = () => { navigator.clipboard && navigator.clipboard.writeText(t.command||''); toast('命令已复制','ok'); });
    $('#drawerBody').querySelectorAll('[data-log]').forEach(b => b.onclick = () => {
      LOGFILE = b.dataset.log;
      $('#drawerBody').querySelectorAll('[data-log]').forEach(x => x.classList.toggle('on', x.dataset.log===LOGFILE));
      loadLog();
    });
    loadLog();
  }catch(e){ if(!quiet) $('#drawerBody').innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>'; }
}
async function loadLog(){
  if(!SEL_TASK) return;
  try{
    const d = await api('/api/log?task='+encodeURIComponent(SEL_TASK)+'&file='+LOGFILE+'&lines=400');
    const box = $('#logBox'); if(box) box.textContent = d.text || '（空）';
  }catch(e){ const box=$('#logBox'); if(box) box.textContent='日志读取失败：'+e.message; }
}

async function openPlan(ident){
  SEL_PLAN = ident; $('#drawer').classList.add('open'); $('#overlay').classList.add('on');
  $('#drawerTitle').textContent = '计划 ' + ident;
  $('#drawerBody').innerHTML = '<div class="empty">加载中…</div>';
  await loadPlan(false);
}
async function loadPlan(quiet){
  if(!SEL_PLAN) return;
  try{
    const d = await api('/api/plan?ident='+encodeURIComponent(SEL_PLAN));
    if(d.error){ if(!quiet) $('#drawerBody').innerHTML='<div class="empty">'+esc(d.error.error||'加载失败')+'</div>'; return; }
    const sch = d.schedule||{}, sum = d.summary||{}, plan = d.plan||{};
    const counts = sum.counts||{};
    const ctext = Object.keys(counts).map(k => pill(k)+' '+counts[k]).join(' ');
    const steps = sum.steps||[];
    $('#drawerBody').innerHTML =
      '<div class="meta" style="margin-bottom:10px">'+pill(sch.status||'-')
      + '<span>类型 <b>'+esc(sch.type||'-')+'</b></span><span>运行 <b>'+esc(sch.runs==null?'-':sch.runs)+'</b></span>'
      + '<span>下次 <b>'+esc(sch.next_run?ts(sch.next_run):'-')+'</b></span></div>'
      + '<div class="meta" style="margin-bottom:10px"><span>当前 Plan <b class="mono">'+esc(sch.current_plan||plan.id||'-')+'</b></span>'
      + '<span>Plan 状态 <b>'+esc(plan.status||sum.status||'-')+'</b></span></div>'
      + '<div class="actions" style="margin-bottom:12px">'
      + '<label class="sub"><input type="checkbox" id="pFailFast" style="width:auto;vertical-align:middle"> fail-fast</label>'
      + '<button class="primary" id="pRun">执行</button>'
      + '<button id="pResume">继续</button>'
      + '<button id="pTick">驱动周期</button>'
      + '<button id="pPause">暂停周期</button>'
      + '<button id="pUnpause">恢复周期</button>'
      + '<button id="pCancel">取消</button>'
      + '<button id="pRerun">重置重跑</button>'
      + '<button class="danger" id="pDelete">删除</button></div>'
      + '<div class="section-title">步骤（'+steps.length+'）</div>'
      + '<div class="steps">'+(steps.length? steps.map(s =>
          '<div class="step">'+pill(s.status)+'<span class="sid">'+esc(s.stage_id)+' / '+esc(s.step_id)+'</span>'
          + '<span class="muted">尝试 '+esc(s.attempts)+' · 退出 '+esc(s.exit_code==null?'-':s.exit_code)+'</span></div>').join('')
          : '<div class="empty">当前 Plan 无步骤</div>')+'</div>'
      + '<div class="section-title">步骤状态统计</div><div class="meta">'+(ctext||'<span class="muted">无</span>')+'</div>';
    const opt = () => ({
      ident: SEL_PLAN,
      plan_id: sch.current_plan || plan.id || '',
      schedule_id: sch.id || SEL_PLAN,
      fail_fast: $('#pFailFast') && $('#pFailFast').checked
    });
    $('#pRun') && ($('#pRun').onclick = () => { const o=opt(); if(!o.plan_id) return toast('没有可执行的 Plan','bad'); act('plan.run',{plan_id:o.plan_id,fail_fast:o.fail_fast},'执行 '+o.plan_id+'？'); });
    $('#pResume') && ($('#pResume').onclick = () => act('plan.resume',{ident:SEL_PLAN,fail_fast:opt().fail_fast},'继续 '+SEL_PLAN+'？'));
    $('#pTick') && ($('#pTick').onclick = () => act('schedule.tick',{schedule_id:opt().schedule_id},'驱动周期 '+opt().schedule_id+'？'));
    $('#pPause') && ($('#pPause').onclick = () => act('plan.pause',{schedule_id:opt().schedule_id},'暂停周期？'));
    $('#pUnpause') && ($('#pUnpause').onclick = () => act('plan.unpause',{schedule_id:opt().schedule_id},'恢复周期？'));
    $('#pCancel') && ($('#pCancel').onclick = () => act('plan.cancel',{ident:SEL_PLAN},'取消 '+SEL_PLAN+'？'));
    $('#pRerun') && ($('#pRerun').onclick = () => { const o=opt(); if(!o.plan_id) return toast('没有可重置的 Plan','bad'); act('plan.rerun',{plan_id:o.plan_id},'重置 '+o.plan_id+' 为 pending？'); });
    $('#pDelete') && ($('#pDelete').onclick = () => act('plan.delete',{ident:SEL_PLAN},'删除 '+SEL_PLAN+'（不可恢复）？'));
  }catch(e){ if(!quiet) $('#drawerBody').innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>'; }
}

function closeDrawer(){ SEL_TASK=null; SEL_PLAN=null; $('#drawer').classList.remove('open'); $('#overlay').classList.remove('on'); }
$('#overlay').onclick = closeDrawer;
$('#btnCloseDrawer').onclick = closeDrawer;
document.addEventListener('keydown', e => { if(e.key==='Escape') closeDrawer(); });

$('#btnRefresh').onclick = refresh;
$('#btnMcpToggle').onclick = mcpToggle;
$('#btnMcpCopy').onclick = mcpCopy;
$('#interval').onchange = restartTimer;
$('#taskSearch').oninput = renderTasks;
$('#showAllApprovals').onchange = renderApprovals;
$('#btnSavePolicy').onclick = savePolicy;
document.querySelectorAll('.tab').forEach(b => b.onclick = () => showTab(b.dataset.tab));

$('#btnCreateTask').onclick = () => {
  const command = $('#tCommand').value.trim();
  if(!command) return toast('命令不能为空','bad');
  act('task.create',{id:$('#tId').value.trim(),command,cwd:$('#tCwd').value.trim(),
    level:$('#tLevel').value,timeout:$('#tTimeout').value,description:$('#tDesc').value.trim(),
    env:$('#tEnv').value}, null);
};
$('#btnCreatePack').onclick = () => {
  const commands = $('#pCommands').value.split('\n').map(x=>x.trim()).filter(Boolean);
  if(!commands.length) return toast('至少一条命令','bad');
  act('pack.create',{commands,pack_id:$('#pId').value.trim(),cwd:$('#pCwd').value.trim(),
    level:$('#pLevel').value,timeout:$('#pTimeout').value,description:$('#pDesc').value.trim()}, null);
};
$('#btnSubmitPlan').onclick = () => {
  const spec = $('#planSpec').value.trim();
  if(!spec) return toast('spec 不能为空','bad');
  act('plan.submit',{spec_text:spec,id:$('#planId').value.trim()}, null);
};

restartTimer(); refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AtsUI/" + VERSION

    def log_message(self, fmt, *args):
        return

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _send(self, body, content_type="application/json; charset=utf-8", code=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            srv = json.dumps({"root": str(ROOT), "script": str(SCRIPT_FILE),
                              "port": PORT, "version": VERSION}, ensure_ascii=False)
            return self._send(HTML.replace("__SRV__", srv), "text/html; charset=utf-8")
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/ping":
                return self._send({"ok": True, "server": "ats-ui", "version": VERSION,
                                   "pid": os.getpid(), "root": str(ROOT), "port": PORT,
                                   "script": str(SCRIPT_FILE), "started_at": SERVER_START,
                                   "now": int(time.time())})
            if parsed.path == "/api/state":
                return self._send(build_state(ROOT))
            if parsed.path == "/api/task":
                task_id = (query.get("id") or [""])[0]
                data = task_detail(ROOT, task_id)
                if not data:
                    return self._send({"error": "not_found", "task_id": task_id}, code=404)
                return self._send(data)
            if parsed.path == "/api/plan":
                ident = (query.get("ident") or [""])[0]
                if not ident:
                    return self._send({"error": "missing_ident"}, code=400)
                return self._send(plan_detail(ROOT, ident))
            if parsed.path == "/api/log":
                task_id = (query.get("task") or [""])[0]
                name = (query.get("file") or ["stdout"])[0]
                lines = int((query.get("lines") or ["300"])[0])
                mapping = {"stdout": "stdout.log", "stderr": "stderr.log",
                           "hook": "hook-enc.log", "supervisor": "supervisor.log"}
                if name not in mapping:
                    return self._send({"error": "bad_file"}, code=400)
                path = Path(ROOT) / "tasks" / task_id / mapping[name]
                return self._send({"task": task_id, "file": name,
                                   "text": tail_text(path, max(1, min(lines, 1000)))})
            if parsed.path == "/api/events":
                return self._send({"events": event_rows(ROOT, 80)})
            if parsed.path == "/api/policy":
                return self._send(policy_info(ROOT))
        except ValueError as exc:
            return self._send({"error": "bad_request", "message": str(exc)}, code=400)
        except Exception as exc:  # 兜底：任何读取异常都返回 JSON，避免连接被直接掐断
            return self._send({"error": "server_error", "scope": parsed.path,
                               "message": "%s: %s" % (type(exc).__name__, exc)}, code=500)
        return self._send({"error": "not_found"}, code=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            return self._send({"error": "not_found"}, code=404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._send({"ok": False, "error": "bad_body", "message": str(exc)}, code=400)
        try:
            return self._send(dispatch(ROOT, payload))
        except ValueError as exc:
            return self._send({"ok": False, "error": "bad_action", "message": str(exc)}, code=400)
        except Exception as exc:  # 面板自身异常不让进程崩掉
            return self._send({"ok": False, "error": "ui_internal", "message": str(exc)}, code=500)


def pid_file(root):
    return Path(root) / "ui_tmp" / "server.json"


def probe_ping(host, port, timeout=2.0):
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = "http://%s:%d/api/ping" % (probe_host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def spawn_wmi(args, cwd):
    """Windows：经 WMI 创建进程，脱离调用方作业对象（普通 Popen 子进程会被宿主一并回收）。"""
    if os.name != "nt":
        return None
    ps = ("$ErrorActionPreference='Stop'; "
          "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          "-Arguments @{CommandLine=%s; CurrentDirectory=%s}; "
          "if ($r.ReturnValue -ne 0) { exit 1 }; $r.ProcessId"
          % (_ps_quote(subprocess.list2cmdline(args)), _ps_quote(cwd)))
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=25,
                             creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [x.strip() for x in (out.stdout or "").splitlines() if x.strip()]
    try:
        return int(lines[-1])
    except (IndexError, ValueError):
        return None


def launch_detached(root, host, port):
    tmp = Path(root) / "ui_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    log = tmp / "ui.log"
    args = [sys.executable, str(SCRIPT_FILE), "--root", str(root), "--host", host,
            "--port", str(port), "--log", str(log), "--serve"]
    pid = spawn_wmi(args, str(SCRIPT_DIR))
    if pid is None:
        with open(log, "ab") as fh:
            flags = (0x00000008 | 0x00000200 | 0x01000000) if os.name == "nt" else 0
            try:
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=fh,
                                        stderr=subprocess.STDOUT, creationflags=flags,
                                        close_fds=True, cwd=str(SCRIPT_DIR))
            except OSError:
                flags = 0x00000008 if os.name == "nt" else 0
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=fh,
                                        stderr=subprocess.STDOUT, creationflags=flags,
                                        close_fds=True, cwd=str(SCRIPT_DIR))
        pid = proc.pid
    info = {"pid": pid, "host": host, "port": port, "root": str(root),
            "url": "http://%s:%d/" % (host, port), "log": str(log),
            "started_at": int(time.time())}
    pid_file(root).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return pid, log


def ensure_running(root, host, port, open_browser):
    url = "http://%s:%d/" % (host, port)
    info = probe_ping(host, port)
    if info:
        print("控制台已在运行: %s  (PID %s, root %s)" % (url, info.get("pid"), info.get("root")))
        if open_browser:
            webbrowser.open(url)
        return 0
    pid, log = launch_detached(root, host, port)
    for _ in range(40):
        time.sleep(0.25)
        info = probe_ping(host, port, timeout=1.0)
        if info:
            print("控制台已自动启动: %s  (PID %s)" % (url, info.get("pid")))
            print("ATS_ROOT: %s" % root)
            if open_browser:
                webbrowser.open(url)
            return 0
    print("自动启动失败：10 秒内 %s 无响应。" % url)
    try:
        tail = Path(log).read_text(encoding="utf-8", errors="replace").strip().splitlines()[-15:]
    except OSError:
        tail = []
    if tail:
        print("启动日志 %s 末尾：" % log)
        print("\n".join(tail))
    print("若端口被占用，请换端口：python \"%s\" --root \"%s\" --port %d --open"
          % (SCRIPT_FILE, root, port + 1))
    return 2


def stop_server(root):
    path = pid_file(root)
    info = read_json(path)
    if not isinstance(info, dict) or not info.get("pid"):
        print("未找到运行记录：%s（可能不是用 --ensure 启动的）" % path)
        return 1
    pid = int(info["pid"])
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    try:
        path.unlink()
    except OSError:
        pass
    print("已停止控制台 PID %s" % pid)
    return 0


def serve(root, host, port, open_browser, log_path=None, mcp_port=None):
    global ROOT, PORT, MCP_ENDPOINT
    ROOT = root
    PORT = port
    MCP_ENDPOINT = None
    if mcp_http is not None:                     # 默认关闭，仅在内存里准备端点对象
        MCP_ENDPOINT = mcp_http.Endpoint(root, mcp_port or (port + 1))
    if log_path:
        try:
            fh = open(log_path, "a", buffering=1, encoding="utf-8")
            sys.stdout = fh
            sys.stderr = fh
        except OSError:
            pass
    if not (Path(root) / "tasks").exists() and not (Path(root) / "schedules").exists():
        print("提示：%s 下还没有 tasks/ 或 schedules/，界面会监听后续变化" % root)
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print("无法监听 %s:%d —— %s" % (host, port, exc))
        print("若已有实例在运行，请用 --ensure 复用；否则换 --port。")
        return 2
    httpd.daemon_threads = True
    url = "http://%s:%d/" % (host, port)
    print("ATS 控制台 v%s: %s" % (VERSION, url))
    print("ATS_ROOT: %s" % root)
    print("按 Ctrl+C 退出")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        if MCP_ENDPOINT is not None:
            MCP_ENDPOINT.stop()                  # 控制台退出时一并释放 MCP 端口
        httpd.server_close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="agent-task-scheduler 本地 Web 面板")
    parser.add_argument("--root", default=None, help="ATS_ROOT，默认 <cwd>/.ats")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mcp-port", type=int, default=None,
                        help="MCP HTTP 端点端口，默认 控制台端口+1；只绑 127.0.0.1，默认关闭")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--ensure", action="store_true", help="未运行则在后台自启动，然后（可配合 --open）打开浏览器")
    parser.add_argument("--status", action="store_true", help="查看控制台是否在运行")
    parser.add_argument("--stop", action="store_true", help="停止由 --ensure 启动的控制台")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--log", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    root = resolve_root(args.root)
    if args.status:
        info = probe_ping(args.host, args.port)
        if info:
            print("运行中: http://%s:%d/  PID %s  root %s  v%s"
                  % (args.host, args.port, info.get("pid"), info.get("root"), info.get("version")))
            return 0
        print("未运行：http://%s:%d/ 无响应" % (args.host, args.port))
        return 1
    if args.stop:
        return stop_server(root)
    if args.ensure:
        return ensure_running(root, args.host, args.port, args.open)
    return serve(root, args.host, args.port, args.open, args.log, args.mcp_port)


if __name__ == "__main__":
    sys.exit(main())
