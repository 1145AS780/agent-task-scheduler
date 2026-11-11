#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ats_preflight.py — 动态沙箱预演（Pre-flight Sandbox）。

定位：**沙箱是证据源，不是判决者**。

三层结构：
  1. 静态层（零副作用，始终执行）：Python AST + shell 词法，只产出风险标签
     fs_delete / fs_write_outside / net_egress / priv_escalation / obfuscated_eval。
  2. 诱饵试跑层（策略开关，默认关）：独立系统 temp root、重定向 HOME/TEMP/
     USERPROFILE/XDG_*、哨兵文件、超时、Windows Job Object kill-on-close。
     只能产出**阳性证据**（哨兵被删/改）；没命中一律记为 inconclusive，绝不等于安全。
  3. 决策层：由调用方（ats.py / ats_plan.py）把 verdict 落成
     allow / pass_with_evidence / deny_injection。

硬约束：
  * 零第三方依赖；
  * 只使用系统临时目录（tempfile.mkdtemp），**绝不写 ATS_ROOT 下任何文件**；
  * finally + Job Object 兜底：不留孤儿进程、不留残留目录；
  * 沙箱失败绝不阻断调度：任何内部异常都退化为 skipped/allow，由权限门与审批继续把关。
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TAG_FS_DELETE = "fs_delete"
TAG_FS_WRITE_OUTSIDE = "fs_write_outside"
TAG_NET_EGRESS = "net_egress"
TAG_PRIV_ESCALATION = "priv_escalation"
TAG_OBFUSCATED_EVAL = "obfuscated_eval"

ALL_TAGS = (TAG_FS_DELETE, TAG_FS_WRITE_OUTSIDE, TAG_NET_EGRESS,
            TAG_PRIV_ESCALATION, TAG_OBFUSCATED_EVAL)

LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}

MAX_SOURCE_BYTES = 512 * 1024
DEFAULT_DRY_RUN_TIMEOUT = 8.0

# 哨兵：相对路径 -> 初始内容。相对路径的删除/改写说明命令确实在 cwd 里动手了。
SENTINELS = {
    "sentinel.txt": "ATS-PREFLIGHT-SENTINEL\n",
    "canary/keep.txt": "ATS-PREFLIGHT-KEEP\n",
    "canary/sub/nested.txt": "ATS-PREFLIGHT-NESTED\n",
    "secret.env": "ATS_PREFLIGHT_SECRET=canary\n",
}

# ---------------------------------------------------------------- 静态层：Python AST

DELETION_CALLS = {
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename",
    "shutil.rmtree", "shutil.move", "pathlib.path.unlink", "pathlib.path.rmdir",
    "path.unlink", "path.rmdir",
}
NET_CALLS = {
    "socket.socket", "socket.create_connection", "socket.socket.connect",
    "urllib.request.urlopen", "urllib.urlopen", "urllib.request.urlretrieve",
    "http.client.httpconnection", "http.client.httpsconnection",
    "ftplib.ftp", "smtplib.smtp", "telnetlib.telnet",
    "requests.get", "requests.post", "requests.put", "requests.request",
    "httpx.get", "httpx.post", "aiohttp.clientSession",
}
PRIV_CALLS = {
    "ctypes.windll", "ctypes.cdll", "ctypes.winapi",
    "os.setuid", "os.seteuid", "os.setgid", "os.setegid",
    "winreg.createsubkey", "winreg.setvalue", "winreg.deletekey",
}
SHELL_EXEC_CALLS = {
    "os.system", "os.popen", "subprocess.run", "subprocess.call",
    "subprocess.popen", "subprocess.check_output", "subprocess.check_call",
    "subprocess.getoutput", "subprocess.getstatusoutput",
}


def _dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return (base + "." + node.attr) if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _const_str(node):
    """字符串常量 + 简单加号折叠；非静态可判定返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _const_str(node.left)
        right = _const_str(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _call_arg(node, index):
    if isinstance(node, ast.Call) and index < len(node.args):
        return node.args[index]
    return None


def _kw(node, name):
    if isinstance(node, ast.Call):
        for kw in node.keywords:
            if kw.arg == name:
                return kw.value
    return None


def is_outside_path(value) -> bool:
    """绝对路径、盘符路径、~ 开头或含 .. 的路径都算“沙箱外”。"""
    if not value:
        return False
    text = str(value).strip().strip("\"'")
    if not text:
        return False
    norm = text.replace("\\", "/")
    if norm.startswith("..") or "/../" in norm or norm.endswith("/.."):
        return True
    if norm.startswith("~"):
        return True
    if os.path.isabs(text):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", text))


class _PyScan(ast.NodeVisitor):
    def __init__(self, filename):
        self.filename = filename
        self.tags = set()
        self.evidence = []
        self._seen = set()

    def _add(self, tag, node, detail, outside=False):
        line = getattr(node, "lineno", None)
        key = (tag, line, detail)
        if key in self._seen:
            return
        self._seen.add(key)
        self.tags.add(tag)
        self.evidence.append({"tag": tag, "source": "ast", "file": self.filename,
                              "line": line, "detail": detail, "outside": bool(outside)})

    def visit_Call(self, node):
        name = _dotted(node.func)
        low = name.lower()

        if low in DELETION_CALLS or low.endswith(".rmtree") or low.endswith(".unlink"):
            target = _const_str(_call_arg(node, 0))
            self._add(TAG_FS_DELETE, node,
                      "delete %s" % (target if target is not None else "<dynamic>"),
                      outside=is_outside_path(target))
        elif low in NET_CALLS:
            self._add(TAG_NET_EGRESS, node, "network call %s" % name)
        elif low in PRIV_CALLS:
            self._add(TAG_PRIV_ESCALATION, node, "privileged call %s" % name)

        if low.split(".")[-1] in ("eval", "exec", "compile") and low in (
                "eval", "exec", "compile", "builtins.eval", "builtins.exec",
                "builtins.compile"):
            self._add(TAG_OBFUSCATED_EVAL, node, "dynamic code %s" % name)

        if low in ("getattr", "builtins.getattr"):
            attr = _const_str(_call_arg(node, 1))
            self._add(TAG_OBFUSCATED_EVAL, node,
                      "getattr(%s)" % (attr if attr is not None else "<dynamic>"))

        if low in ("__import__", "builtins.__import__"):
            mod = _const_str(_call_arg(node, 0))
            if mod is None:
                self._add(TAG_OBFUSCATED_EVAL, node, "dynamic __import__")

        if low in ("importlib.import_module", "importlib.__import__"):
            mod = _const_str(_call_arg(node, 0))
            if mod is None:
                self._add(TAG_OBFUSCATED_EVAL, node, "dynamic import_module")

        if low in SHELL_EXEC_CALLS:
            cmd = _const_str(_call_arg(node, 0))
            shell_kw = _kw(node, "shell")
            shell_on = bool(isinstance(shell_kw, ast.Constant) and shell_kw.value is True)
            if cmd is not None:
                _scan_shell_text(cmd, self.tags, self.evidence, source="ast-shell",
                                 filename=self.filename, lineno=getattr(node, "lineno", None))
            elif shell_on:
                self._add(TAG_OBFUSCATED_EVAL, node, "dynamic shell command")

        if low in ("open", "io.open", "builtins.open") or low.endswith(".open"):
            mode = _const_str(_call_arg(node, 1))
            if mode is None:
                mode = _const_str(_kw(node, "mode"))
            if mode and any(ch in mode for ch in ("w", "a", "x", "+")):
                target = _const_str(_call_arg(node, 0))
                outside = is_outside_path(target)
                if outside:
                    self._add(TAG_FS_WRITE_OUTSIDE, node,
                              "write %s mode=%s" % (target, mode), outside=True)
                else:
                    self.evidence.append({"tag": TAG_FS_WRITE_OUTSIDE, "source": "ast",
                                          "file": self.filename,
                                          "line": getattr(node, "lineno", None),
                                          "detail": "write %s mode=%s"
                                                    % (target if target else "<dynamic>", mode),
                                          "outside": False})

        self.generic_visit(node)


# ---------------------------------------------------------------- 静态层：shell 词法

SHELL_DELETE_RE = re.compile(
    r"(?i)(?:^|[\s;&|])(?:rm|rmdir|del|erase|rd\s+/s|remove-item)\s")
SHELL_WRITE_RE = re.compile(
    r"(?i)\b(set-content|out-file|copy-item|move-item|robocopy|xcopy|tee-object)\b")
SHELL_NET_RE = re.compile(
    r"(?i)(?:^|[\s;&|])(?:curl|wget|invoke-webrequest|invoke-restmethod|"
    r"start-bitstransfer|nc|ncat|telnet|ssh|scp|ftp|aria2c)\s")
SHELL_PRIV_RE = re.compile(
    r"(?i)\b(runas|sudo|setcap|start-process\s+-verb\s+runas|reg\s+add|"
    r"reg\s+delete|sc\s+create|schtasks|net\s+user|takeown|icacls|diskpart|format\s+[a-z]:)\b")
SHELL_REDIRECT_RE = re.compile(r"(?:^|[^<&>])(>>?)\s*(\"[^\"]+\"|'[^']+'|[^\s|;&]+)")


def _scan_shell_text(text, tags, evidence, source="shell", filename=None, lineno=None):
    if not text:
        return
    for tag, pattern, detail in (
            (TAG_FS_DELETE, SHELL_DELETE_RE, "shell delete token"),
            (TAG_FS_WRITE_OUTSIDE, SHELL_WRITE_RE, "shell write token"),
            (TAG_NET_EGRESS, SHELL_NET_RE, "shell network token"),
            (TAG_PRIV_ESCALATION, SHELL_PRIV_RE, "shell privileged token")):
        if pattern.search(text):
            tags.add(tag)
            evidence.append({"tag": tag, "source": source, "file": filename,
                             "line": lineno, "detail": detail, "outside": False})
    for match in SHELL_REDIRECT_RE.finditer(text):
        target = match.group(2).strip("\"'")
        outside = is_outside_path(target)
        if outside:
            tags.add(TAG_FS_WRITE_OUTSIDE)
        evidence.append({"tag": TAG_FS_WRITE_OUTSIDE, "source": source, "file": filename,
                         "line": lineno, "detail": "redirect > %s" % target,
                         "outside": outside})


# ---------------------------------------------------------------- 静态层：入口

def _split_command(command):
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = str(command).split()
    return [t.strip("\"'") for t in tokens if t]


def _python_sources(command, context=None):
    """从命令行里找出可静态分析的 Python 源码：<file>.py 或 -c "<code>"。"""
    context = dict(context or {})
    sources = []
    tokens = _split_command(command)
    for index, token in enumerate(tokens):
        if token.lower().endswith(".py"):
            candidate = Path(token)
            if not candidate.is_absolute():
                base = context.get("cwd")
                if base:
                    candidate = Path(base) / candidate
            try:
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    if len(text) <= MAX_SOURCE_BYTES:
                        sources.append((str(candidate), text))
            except OSError:
                continue
        if token == "-c" and index + 1 < len(tokens):
            sources.append(("<command -c>", tokens[index + 1]))
    if not sources and str(command).strip().lower().endswith(".py"):
        candidate = Path(str(command).strip())
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
                if len(text) <= MAX_SOURCE_BYTES:
                    sources.append((str(candidate), text))
        except OSError:
            pass
    return sources


def _confidence(tags, evidence):
    if TAG_OBFUSCATED_EVAL in tags or TAG_PRIV_ESCALATION in tags:
        return "high"
    outside_hits = [e for e in evidence if e.get("outside")]
    if outside_hits:
        return "high"
    if TAG_FS_DELETE in tags and TAG_NET_EGRESS in tags:
        return "high"
    if tags:
        return "medium"
    return "none"


def static_review(command, context=None):
    """零副作用静态扫描：只出标签与置信度，不做判决。"""
    context = dict(context or {})
    tags = set()
    evidence = []
    parsed = []

    for name, source in _python_sources(command, context):
        try:
            tree = ast.parse(source, filename=name)
        except SyntaxError as exc:
            parsed.append({"file": name, "ok": False, "error": "syntax_error:%s" % exc})
            continue
        scanner = _PyScan(name)
        scanner.visit(tree)
        tags |= scanner.tags
        evidence.extend(scanner.evidence)
        parsed.append({"file": name, "ok": True})

    _scan_shell_text(str(command or ""), tags, evidence, source="shell-lex")

    confidence = _confidence(tags, evidence)
    return {
        "tags": [t for t in ALL_TAGS if t in tags],
        "evidence": evidence[:40],
        "confidence": confidence,
        "sources": parsed,
    }


# ---------------------------------------------------------------- 诱饵试跑层

def _sandbox_env(sbx):
    """把 HOME/TEMP/USERPROFILE/XDG_* 全部指向沙箱内部。"""
    dirs = {name: Path(sbx) / name for name in
            ("home", "tmp", "appdata", "localappdata", "xdg_config",
             "xdg_cache", "xdg_data", "xdg_state", "userbase")}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "HOME": str(dirs["home"]),
        "USERPROFILE": str(dirs["home"]),
        "TEMP": str(dirs["tmp"]),
        "TMP": str(dirs["tmp"]),
        "APPDATA": str(dirs["appdata"]),
        "LOCALAPPDATA": str(dirs["localappdata"]),
        "XDG_CONFIG_HOME": str(dirs["xdg_config"]),
        "XDG_CACHE_HOME": str(dirs["xdg_cache"]),
        "XDG_DATA_HOME": str(dirs["xdg_data"]),
        "XDG_STATE_HOME": str(dirs["xdg_state"]),
        "PYTHONUSERBASE": str(dirs["userbase"]),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    return env


def _seed_sentinels(sbx):
    marks = {}
    for rel, content in SENTINELS.items():
        path = Path(sbx) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        marks[rel] = (content, path.stat().st_mtime_ns)
    return marks


def _check_sentinels(sbx, marks):
    touched = []
    for rel, (content, mtime) in marks.items():
        path = Path(sbx) / rel
        try:
            if not path.exists():
                touched.append({"path": rel, "change": "deleted"})
                continue
            current = path.read_text(encoding="utf-8", errors="replace")
            if current != content:
                touched.append({"path": rel, "change": "modified"})
            elif path.stat().st_mtime_ns != mtime:
                touched.append({"path": rel, "change": "touched"})
        except OSError:
            touched.append({"path": rel, "change": "unreadable"})
    return touched


# ---- Windows Job Object（仅用于 kill-on-close，不是隔离层）

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _win_job_assign(pid):
    """把进程放进 KILL_ON_JOB_CLOSE 的 Job；失败返回 None，由 taskkill 兜底。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit),
                        ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, wintypes.BOOL, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        handle = kernel32.OpenProcess(0x0100 | 0x0001, False, int(pid))  # SET_QUOTA|TERMINATE
        if not handle:
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, handle):
            kernel32.CloseHandle(handle)
            kernel32.CloseHandle(job)
            return None
        kernel32.CloseHandle(handle)
        return {"handle": job, "kernel32": kernel32}
    except Exception:
        return None


def _win_job_terminate(job):
    if not job:
        return
    try:
        job["kernel32"].TerminateJobObject(job["handle"], 1)
    except Exception:
        pass


def _win_job_close(job):
    if not job:
        return
    try:
        job["kernel32"].CloseHandle(job["handle"])
    except Exception:
        pass


def process_alive(pid) -> bool:
    """跨平台探测进程是否仍存活；用于测试“无孤儿进程”。"""
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, wintypes.BOOL, ctypes.c_uint32]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
            if not handle:
                return False
            code = ctypes.c_uint32(0)
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259      # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _kill_tree(proc, job):
    if proc is None:
        return
    if job:
        _win_job_terminate(job)
    if os.name == "nt":
        if not job:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, creationflags=0x08000000, timeout=10)
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _tail(path, limit=800):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def dry_run(command, timeout=DEFAULT_DRY_RUN_TIMEOUT, shell=True, context=None,
            isolate=True):
    """在独立系统 temp root 里试跑一次；只产出阳性证据。

    isolate 由策略 win32_isolation 控制：True 时尝试把进程放进
    KILL_ON_JOB_CLOSE 的 Job Object；False 时只靠 taskkill /T /F 兜底。
    """
    context = dict(context or {})
    started = time.time()
    sbx = Path(tempfile.mkdtemp(prefix="ats-pfsbx-"))
    job = None
    proc = None
    result = {
        "status": "skipped",
        "verdict": "inconclusive",
        "exit_code": None,
        "timed_out": False,
        "sentinel_touched": False,
        "sentinels": [],
        "duration_ms": 0,
        "pid": None,
        "log_tail": "",
        "sandbox_root": str(sbx),
    }
    try:
        marks = _seed_sentinels(sbx)
        env = _sandbox_env(sbx)
        log_path = sbx / "_sandbox.log"
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            proc = subprocess.Popen(
                str(command), cwd=str(sbx), env=env, shell=bool(shell),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"))
        job = _win_job_assign(proc.pid) if isolate else None
        timed_out = False
        code = None
        try:
            code = proc.wait(timeout=max(1.0, float(timeout)))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc, job)
            try:
                code = proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                code = -1
        touched = _check_sentinels(sbx, marks)
        result.update({
            "status": "timed_out" if timed_out else
                      ("crashed" if (code not in (0, None)) else "finished"),
            "verdict": "touched_sentinel" if touched else "inconclusive",
            "exit_code": code,
            "timed_out": timed_out,
            "sentinel_touched": bool(touched),
            "sentinels": touched,
            "pid": proc.pid,
            "log_tail": _tail(log_path),
            "job_mode": "job" if job else ("taskkill" if os.name == "nt" else "posix-group"),
        })
    except Exception as exc:                     # 沙箱自身故障不阻断调度
        result.update({"status": "error", "verdict": "inconclusive",
                       "error": "%s: %s" % (type(exc).__name__, exc)})
    finally:
        try:
            if proc is not None and proc.poll() is None:
                _kill_tree(proc, job)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            _win_job_close(job)
            shutil.rmtree(sbx, ignore_errors=True)
        result["duration_ms"] = int((time.time() - started) * 1000)
        result["leftover"] = bool(Path(sbx).exists())
        result.pop("sandbox_root", None)
    return result


# ---------------------------------------------------------------- 决策层

def review(root, command, level="L1", context=None, pol=None, shell=True):
    """返回预演结论。调用方据此决定 allow / pass_with_evidence / deny_injection。

    * 静态 high 置信 -> deny_injection（无论诱饵层是否开启）。
    * 诱饵命中 -> deny_injection（强证据）。
    * 超时/崩溃/有标签 -> pass_with_evidence（进审批，由人看证据）。
    * 其余 -> allow。
    """
    pol = dict(pol or {})
    out = {
        "status": "skipped",
        "verdict": "allow",
        "reason": None,
        "tags": [],
        "confidence": "none",
        "static": None,
        "dry_run": None,
        "at": int(time.time()),
        "duration_ms": 0,
    }
    if not command or str(command).strip() == "":
        return out
    if context and context.get("argv"):
        out["reason"] = "argv_task"          # 下载/种子任务：不走 shell，静态意义有限
        return out
    if context and context.get("skip_preflight"):
        out["reason"] = "policy_skip"
        return out

    started = time.time()
    try:
        static = static_review(command, context)
        out["static"] = static
        out["tags"] = static["tags"]
        out["confidence"] = static["confidence"]
        out["status"] = "static_only"

        if static["confidence"] == "high":
            out["verdict"] = "deny_injection"
            out["reason"] = "static_high_confidence"
            out["duration_ms"] = int((time.time() - started) * 1000)
            return out

        use_dry = bool(pol.get("preflight_dry_run")) \
            and LEVEL_RANK.get(str(level).upper(), 0) >= LEVEL_RANK["L2"]
        if use_dry:
            dry = dry_run(command,
                          timeout=float(pol.get("preflight_timeout_seconds")
                                        or DEFAULT_DRY_RUN_TIMEOUT),
                          shell=shell, context=context,
                          isolate=bool(pol.get("win32_isolation", False)))
            out["dry_run"] = dry
            out["status"] = "dry_run"
            if dry.get("sentinel_touched"):
                out["verdict"] = "deny_injection"
                out["reason"] = "sentinel_touched"
            elif dry.get("status") in ("timed_out", "crashed"):
                out["verdict"] = "pass_with_evidence"
                out["reason"] = "dry_run_%s" % dry.get("status")
            elif static["tags"]:
                out["verdict"] = "pass_with_evidence"
                out["reason"] = "static_tags"
            else:
                out["verdict"] = "allow"
        elif static["tags"]:
            out["verdict"] = "pass_with_evidence"
            out["reason"] = "static_tags"

        out["duration_ms"] = int((time.time() - started) * 1000)
        return out
    except Exception as exc:
        fail_closed = pol.get("preflight_fail_closed")
        if fail_closed is None:
            fail_closed = True
        else:
            fail_closed = str(fail_closed).strip().lower() in ("1", "true", "yes", "on", "y")
        out.update({"status": "error",
                    "verdict": "deny_injection" if fail_closed else "allow",
                    "reason": "preflight_error",
                    "error": "%s: %s" % (type(exc).__name__, exc)})
        return out
