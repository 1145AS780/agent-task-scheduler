#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-task-scheduler — MCP 适配器（stdio / JSON-RPC 2.0，纯标准库）

协议流：stdin/stdout 每行一个 JSON-RPC 2.0 消息，UTF-8 且不带 BOM。
stdout 只允许出现协议消息；所有日志、traceback、调试输出一律走 stderr。

设计约束：
  * 不 import ats.py / ats_plan.py，避免它们模块级或调用时的 print 污染协议流；
    每次工具调用都以子进程方式走现有 CLI，状态读写只有一份真相。
  * 不修改任何 CLI 行为与文件格式；MCP 只是入口。
  * 启动只检查两个 CLI 文件是否存在，不扫描任务目录。

用法：
  python scripts/ats_mcp.py [--root DIR]

  --root 透传给 ats.py / ats_plan.py；缺省则由 ATS_ROOT 或 <ats 本体目录>/.ats 决定。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

SERVER_NAME = "agent-task-scheduler-mcp"
SERVER_VERSION = "0.1.0"

# 服务端支持的协议版本，按新到旧排列；客户端请求命中就回声，否则回最新的
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

SCRIPT_DIR = Path(__file__).resolve().parent
ATS = SCRIPT_DIR / "ats.py"
ATS_PLAN = SCRIPT_DIR / "ats_plan.py"

ROOT = None


# ---------------------------------------------------------------- 协议输出

def _send(message):
    """写一行 UTF-8 JSON 到 stdout；这是唯一允许碰 stdout 的地方。"""
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _log(text):
    sys.stderr.write("[ats-mcp] %s\n" % text)
    sys.stderr.flush()


def _error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


# ---------------------------------------------------------------- CLI 调用

class BadArgs(Exception):
    """工具参数校验失败；转成 isError 的 tool result，不让进程崩。"""


def _child_env():
    env = dict(os.environ)
    # 子进程统一 UTF-8 输出，避免中文 Windows 的 GBK/OEM 编码问题
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _decode(raw):
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _run_cli(script, argv, timeout=120):
    """跑现有 CLI，返回 (rc, stdout, stderr)。stdout 只是被捕获，不进协议流。"""
    cmd = [sys.executable, str(script)]
    if ROOT:
        cmd += ["--root", ROOT]
    cmd += [str(a) for a in argv]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, env=_child_env())
        return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return 124, _decode(exc.stdout), "CLI 超时（%ss）" % timeout
    except OSError as exc:
        return 125, "", "无法启动 CLI: %s" % exc


def _call_json(script, argv, timeout=120):
    """调用 CLI 并解析 JSON；返回 (payload, is_error)。"""
    rc, out, err = _run_cli(script, argv, timeout=timeout)
    out = out.strip()
    if not out:
        return {"error": "empty_output", "exit_code": rc, "stderr": err[-2000:]}, True
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"error": "invalid_json", "exit_code": rc,
                "stdout_tail": out[-2000:], "stderr_tail": err[-2000:]}, True
    if not isinstance(data, dict):
        return {"error": "invalid_json_shape", "exit_code": rc, "value": data}, True
    return data, rc != 0


# ---------------------------------------------------------------- 参数校验

def _as_str(args, key, required=False, enum=None, default=None):
    value = args.get(key, default)
    if value is None:
        if required:
            raise BadArgs("缺少参数 %s" % key)
        return None
    if not isinstance(value, str):
        raise BadArgs("参数 %s 必须是字符串" % key)
    if required and not value.strip():
        raise BadArgs("参数 %s 不能为空" % key)
    if enum and value not in enum:
        raise BadArgs("参数 %s 必须是 %s 之一" % (key, "/".join(enum)))
    return value


def _as_int(args, key, default=None, minimum=None, maximum=None):
    value = args.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadArgs("参数 %s 必须是整数" % key)
    if minimum is not None and value < minimum:
        raise BadArgs("参数 %s 不能小于 %s" % (key, minimum))
    if maximum is not None and value > maximum:
        raise BadArgs("参数 %s 不能大于 %s" % (key, maximum))
    return value


def _as_bool(args, key, default=False):
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise BadArgs("参数 %s 必须是布尔值" % key)
    return value


def _as_env(args):
    value = args.get("env")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BadArgs("参数 env 必须是对象")
    out = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise BadArgs("env 的键必须是非空字符串")
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise BadArgs("env.%s 必须是字符串或数字" % key)
        out[key] = str(item)
    return out


def _as_str_list(args, key):
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise BadArgs("参数 %s 必须是字符串数组" % key)
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise BadArgs("参数 %s 的元素必须是非空字符串" % key)
        out.append(item)
    return out


# ---------------------------------------------------------------- 工具实现

def _render(payload, is_error):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def tool_task_create(args):
    command = _as_str(args, "command", required=True)
    task_id = _as_str(args, "task_id")
    cwd = _as_str(args, "cwd")
    level = _as_str(args, "level", enum=("L0", "L1", "L2", "L3"))
    timeout = _as_int(args, "timeout", minimum=0)
    description = _as_str(args, "description")
    env = _as_env(args)

    argv = ["create"]
    if task_id:
        argv.append(task_id)
    argv += ["--command", command]
    if cwd:
        argv += ["--cwd", cwd]
    if level:
        argv += ["--level", level]
    if timeout is not None:
        argv += ["--timeout", str(timeout)]
    if description:
        argv += ["--description", description]
    for key, value in env.items():
        argv += ["--env", "%s=%s" % (key, value)]
    return _call_json(ATS, argv, timeout=60)


def tool_task_run(args):
    task_id = _as_str(args, "task_id", required=True)
    approval = _as_str(args, "approval")
    wait = _as_bool(args, "wait", False)
    force = _as_bool(args, "force", False)

    argv = ["run", task_id]
    if approval:
        argv += ["--approval", approval]
    if force:
        argv += ["--force"]
    if wait:
        argv += ["--wait"]
    # --wait 的时长由任务自身的 timeout 决定，这里不额外截断
    return _call_json(ATS, argv, timeout=None)


def tool_task_status(args):
    task_id = _as_str(args, "task_id", required=True)
    return _call_json(ATS, ["status", task_id, "--json"], timeout=60)


def tool_task_list(args):
    status = _as_str(args, "status")
    limit = _as_int(args, "limit", default=20, minimum=1, maximum=500)

    argv = ["list", "--json"]
    if status:
        argv += ["--status", status]
    payload, is_error = _call_json(ATS, argv, timeout=60)
    if is_error:
        return payload, is_error

    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return payload, False
    total = payload.get("count", len(tasks))
    trimmed = tasks[:limit]
    return {"tasks": trimmed, "count": total, "returned": len(trimmed)}, False


def tool_task_logs(args):
    task_id = _as_str(args, "task_id", required=True)
    lines = _as_int(args, "lines", default=50, minimum=1, maximum=1000)
    return _call_json(ATS, ["show", task_id, "--lines", str(lines)], timeout=60)


def tool_task_stop(args):
    task_id = _as_str(args, "task_id", required=True)
    approval = _as_str(args, "approval")
    force = _as_bool(args, "force", False)

    argv = ["stop", task_id]
    if approval:
        argv += ["--approval", approval]
    if force:
        argv += ["--force"]
    return _call_json(ATS, argv, timeout=60)


def tool_task_check(args):
    task_id = _as_str(args, "task_id")
    argv = ["check"] + ([task_id] if task_id else [])
    return _call_json(ATS, argv, timeout=60)


def tool_approval_resolve(args):
    approval_ids = _as_str_list(args, "approval_ids")
    all_pending = _as_bool(args, "all", False)
    reject = _as_bool(args, "reject", False)
    note = _as_str(args, "note")

    if not approval_ids and not all_pending:
        raise BadArgs("必须提供 approval_ids，或把 all 设为 true")

    argv = ["approve"]
    if all_pending:
        argv.append("--all")
    else:
        argv += approval_ids
    if reject:
        argv.append("--reject")
    if note:
        argv += ["--note", note]
    return _call_json(ATS, argv, timeout=60)


def tool_plan_list(args):
    return _call_json(ATS_PLAN, ["list", "--json"], timeout=60)


def tool_plan_show(args):
    ident = _as_str(args, "ident", required=True)
    return _call_json(ATS_PLAN, ["show", ident, "--json"], timeout=60)


def tool_plan_run(args):
    plan_id = _as_str(args, "plan_id", required=True)
    approval = _as_str(args, "approval")
    fail_fast = _as_bool(args, "fail_fast", False)

    argv = ["run_plan", plan_id]
    if approval:
        argv += ["--approval", approval]
    if fail_fast:
        argv.append("--fail-fast")
    return _call_json(ATS_PLAN, argv, timeout=None)


def tool_plan_resume(args):
    ident = _as_str(args, "ident", required=True)
    approval = _as_str(args, "approval")
    fail_fast = _as_bool(args, "fail_fast", False)

    argv = ["resume", ident]
    if approval:
        argv += ["--approval", approval]
    if fail_fast:
        argv.append("--fail-fast")
    return _call_json(ATS_PLAN, argv, timeout=None)


def tool_schedule_check(args):
    schedule_id = _as_str(args, "schedule_id", required=True)
    return _call_json(ATS_PLAN, ["check_schedule", schedule_id], timeout=None)


def tool_schedule_pause(args):
    schedule_id = _as_str(args, "schedule_id", required=True)
    approval = _as_str(args, "approval")
    argv = ["pause", schedule_id]
    if approval:
        argv += ["--approval", approval]
    return _call_json(ATS_PLAN, argv, timeout=60)


def tool_schedule_resume(args):
    schedule_id = _as_str(args, "schedule_id", required=True)
    approval = _as_str(args, "approval")
    argv = ["resume_schedule", schedule_id]
    if approval:
        argv += ["--approval", approval]
    return _call_json(ATS_PLAN, argv, timeout=None)


def tool_schedule_cancel(args):
    schedule_id = _as_str(args, "schedule_id", required=True)
    approval = _as_str(args, "approval")
    argv = ["cancel", schedule_id]
    if approval:
        argv += ["--approval", approval]
    return _call_json(ATS_PLAN, argv, timeout=60)


HANDLERS = {
    "task_create": tool_task_create,
    "task_run": tool_task_run,
    "task_status": tool_task_status,
    "task_list": tool_task_list,
    "task_logs": tool_task_logs,
    "task_stop": tool_task_stop,
    "task_check": tool_task_check,
    "approval_resolve": tool_approval_resolve,
    "plan_list": tool_plan_list,
    "plan_show": tool_plan_show,
    "plan_run": tool_plan_run,
    "plan_resume": tool_plan_resume,
    "schedule_check": tool_schedule_check,
    "schedule_pause": tool_schedule_pause,
    "schedule_resume": tool_schedule_resume,
    "schedule_cancel": tool_schedule_cancel,
}


# ---------------------------------------------------------------- 工具清单

def _schema(props, required=None):
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


TOOLS = [
    {
        "name": "task_create",
        "description": ("Create a background task without starting it. Use when a command is "
                        "long-running and must not hold the conversation turn, or when a "
                        "high-risk command needs an L0-L3 approval gate. Returns task id and state."),
        "inputSchema": _schema({
            "command": {"type": "string",
                        "description": ("Shell command to run later. Append ` @enc <converter args>` "
                                        "to register an encoding conversion as a post-hook that "
                                        "runs only if the main command exits 0.")},
            "task_id": {"type": "string", "description": "Optional stable id; auto-generated if omitted."},
            "cwd": {"type": "string", "description": "Working directory for the command."},
            "level": {"type": "string", "enum": ["L0", "L1", "L2", "L3"],
                      "description": "Permission tier: L0 no gate, L3 highest risk."},
            "timeout": {"type": "integer", "minimum": 0,
                        "description": "Seconds before the supervisor kills the task; 0 means no limit."},
            "env": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "Extra environment variables for the command."},
            "description": {"type": "string", "description": "Human note stored with the task."},
        }, ["command"]),
    },
    {
        "name": "task_run",
        "description": ("Start a task created by task_create. wait=false returns immediately and the "
                        "task keeps running; wait=true blocks until it finishes. If policy requires "
                        "approval, returns approval_required with an approval_id."),
        "inputSchema": _schema({
            "task_id": {"type": "string"},
            "wait": {"type": "boolean", "default": False},
            "approval": {"type": "string", "description": "Approval id when policy requires one."},
            "force": {"type": "boolean", "default": False,
                      "description": "Allow rerunning an already finished task."},
        }, ["task_id"]),
    },
    {
        "name": "task_status",
        "description": ("Lightweight status for one task: state, exit code, pid, start/end timestamps "
                        "and approval id. Refreshes liveness first, so a dead supervisor is reconciled. "
                        "There is no progress percentage in the underlying state; do not infer one."),
        "inputSchema": _schema({"task_id": {"type": "string"}}, ["task_id"]),
    },
    {
        "name": "task_list",
        "description": ("List tasks, optionally filtered by status. Use status='running' or "
                        "'failed,timeout' etc. limit caps how many rows are returned."),
        "inputSchema": _schema({
            "status": {"type": "string", "description": "Comma-separated statuses: pending, starting, running, succeeded, failed, timeout, killed."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20},
        }),
    },
    {
        "name": "task_logs",
        "description": ("Return task detail plus stdout_tail and stderr_tail (last N lines each). "
                        "Use after task_status when a run failed or looks stuck."),
        "inputSchema": _schema({
            "task_id": {"type": "string"},
            "lines": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50},
        }, ["task_id"]),
    },
    {
        "name": "task_stop",
        "description": ("Stop a starting/running task; kills its process tree. May require approval, "
                        "in which case pass the approval id returned by a prior call."),
        "inputSchema": _schema({
            "task_id": {"type": "string"},
            "approval": {"type": "string"},
            "force": {"type": "boolean", "default": False},
        }, ["task_id"]),
    },
    {
        "name": "task_check",
        "description": ("Reconcile liveness for one task or all tasks. Use when a task still says "
                        "running after a crash; returns changed ids, running ids and next_poll."),
        "inputSchema": _schema({"task_id": {"type": "string"}}),
    },
    {
        "name": "approval_resolve",
        "description": ("Approve or reject pending approvals. approval_ids come from task_run "
                        "(approval_required) or task_status (approval_id). Set all=true to resolve "
                        "every pending approval, and reject=true to deny. Requires the human "
                        "passphrase, so this call returns passphrase_required by design: resolve "
                        "approvals in the console or CLI where a human can type the passphrase."),
        "inputSchema": _schema({
            "approval_ids": {"type": "array", "items": {"type": "string"}},
            "all": {"type": "boolean", "default": False},
            "reject": {"type": "boolean", "default": False},
            "note": {"type": "string"},
        }),
    },
    {
        "name": "plan_list",
        "description": ("List schedules (one-shot and periodic) with status, run counts, current "
                        "plan id and step counts. The CLI has no separate plan-only list; use "
                        "plan_show with a plan id for detail."),
        "inputSchema": _schema({}),
    },
    {
        "name": "plan_show",
        "description": "Show a plan or schedule by id, including current step summary.",
        "inputSchema": _schema({"ident": {"type": "string"}}, ["ident"]),
    },
    {
        "name": "plan_run",
        "description": ("Run a submitted plan and block until its stages finish. Pass fail_fast "
                        "to stop at the first failing step."),
        "inputSchema": _schema({
            "plan_id": {"type": "string"},
            "approval": {"type": "string"},
            "fail_fast": {"type": "boolean", "default": False},
        }, ["plan_id"]),
    },
    {
        "name": "plan_resume",
        "description": "Resume an interrupted plan or a paused schedule by id.",
        "inputSchema": _schema({
            "ident": {"type": "string"},
            "approval": {"type": "string"},
            "fail_fast": {"type": "boolean", "default": False},
        }, ["ident"]),
    },
    {
        "name": "schedule_check",
        "description": ("Drive a periodic schedule: triggers it when due and returns next_run / "
                        "next_poll. Call again on the interval the response asks for."),
        "inputSchema": _schema({"schedule_id": {"type": "string"}}, ["schedule_id"]),
    },
    {
        "name": "schedule_pause",
        "description": "Pause a periodic schedule. May require approval.",
        "inputSchema": _schema({"schedule_id": {"type": "string"}, "approval": {"type": "string"}},
                               ["schedule_id"]),
    },
    {
        "name": "schedule_resume",
        "description": "Resume a paused periodic schedule. May require approval.",
        "inputSchema": _schema({"schedule_id": {"type": "string"}, "approval": {"type": "string"}},
                               ["schedule_id"]),
    },
    {
        "name": "schedule_cancel",
        "description": "Cancel a schedule and skip remaining steps. May require approval.",
        "inputSchema": _schema({"schedule_id": {"type": "string"}, "approval": {"type": "string"}},
                               ["schedule_id"]),
    },
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}


# ---------------------------------------------------------------- 方法分发

def _initialize(params):
    params = params if isinstance(params, dict) else {}
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _tools_call(params):
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("缺少工具名")
    if name not in TOOL_NAMES:
        raise KeyError(name)
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise BadArgs("arguments 必须是对象")

    try:
        payload, is_error = HANDLERS[name](arguments)
    except BadArgs as exc:
        return _render({"error": "invalid_arguments", "message": str(exc)}, True)
    return _render(payload, is_error)


def _handle(message):
    """处理单条消息；notification 返回 None。"""
    if not isinstance(message, dict):
        return _error(None, -32600, "Invalid Request")
    msg_id = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")
    params = message.get("params")

    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return None if is_notification else _error(msg_id, -32600, "Invalid Request")

    if method == "initialize":
        return _result(msg_id, _initialize(params))
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        try:
            return _result(msg_id, _tools_call(params))
        except KeyError as exc:
            return _error(msg_id, -32602, "Unknown tool: %s" % exc.args[0])
        except ValueError as exc:
            return _error(msg_id, -32602, str(exc))
        except Exception:
            _log("tools/call 崩溃:\n%s" % traceback.format_exc())
            return _result(msg_id, _render({"error": "internal_error"}, True))

    if is_notification:
        return None
    return _error(msg_id, -32601, "Method not found: %s" % method)


def _handle_line(line):
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        return _error(None, -32700, "Parse error", {"detail": str(exc)})

    if isinstance(message, list):
        if not message:
            return _error(None, -32600, "Invalid Request")
        replies = [r for r in (_handle(item) for item in message) if r is not None]
        return replies or None
    return _handle(message)


# ---------------------------------------------------------------- 入口

def main(argv=None):
    global ROOT
    parser = argparse.ArgumentParser(
        prog="ats_mcp.py",
        description="agent-task-scheduler MCP adapter (stdio, JSON-RPC 2.0)")
    parser.add_argument("--root", default=None, help="ATS_ROOT 透传给 ats.py / ats_plan.py")
    args = parser.parse_args(argv)
    ROOT = args.root

    if not ATS.is_file() or not ATS_PLAN.is_file():
        _log("缺少 CLI：%s / %s" % (ATS, ATS_PLAN))
        return 2

    _log("ready: %s v%s, root=%s" % (SERVER_NAME, SERVER_VERSION, ROOT or "(default)"))
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            _send(_error(None, -32700, "Parse error: 输入不是 UTF-8"))
            continue
        reply = _handle_line(text)
        if reply is not None:
            _send(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
