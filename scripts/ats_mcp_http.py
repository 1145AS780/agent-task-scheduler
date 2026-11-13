#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-task-scheduler — 可启停的本地 MCP HTTP 端点 (v1.0)

传输选型：MCP 2025-06-18 的 Streamable HTTP。
  * 单端点 POST /mcp 收发 JSON-RPC，initialize 时用 Mcp-Session-Id 响应头下发会话；
  * GET /mcp 按规范返回 405（本适配器没有服务端主动推送的通知，不假装有 SSE 流）；
  * DELETE /mcp 结束会话。
  不选旧版 HTTP+SSE（2024-11-05）：那套要两个端点加一条长连接 GET 流，
  而本适配器没有任何服务端推送，多出来的是空壳；Streamable HTTP 是当前规范，
  客户端用 {"type": "http", "url": ...} 直接接入。

复用而非复写：工具表与调用逻辑直接 import 同目录 ats_mcp.py，
  使用它的 TOOLS 与 _handle()，stdio 入口（ats_mcp.py 自身 main）保持原样可用。

安全：只绑 127.0.0.1；校验 Host 与 Origin（浏览器来源必须等于本端点）；
  不返回任何 CORS 头；开启动作本身由控制台要求显式确认。

纯标准库，无第三方依赖。
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.dont_write_bytecode = True

try:
    import ats_mcp
except Exception:  # 同目录缺 ats_mcp.py 时端点仍可创建，只是 available=false
    ats_mcp = None

HOST = "127.0.0.1"
SESSION_IDLE = 30 * 60          # 会话空闲 30 分钟后从客户端计数里剔除
MAX_BODY = 8 * 1024 * 1024      # 单条 JSON-RPC 请求体上限，防止被灌爆


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False   # Windows 上 SO_REUSEADDR 会掩盖端口占用，显式关掉


class Endpoint:
    """一个 Console 进程内的 MCP HTTP 端点，可反复启停。"""

    def __init__(self, root, port, log=None):
        self.root = str(root)
        self.port = int(port)
        self._log = log
        self._lock = threading.Lock()
        self._sessions_lock = threading.Lock()
        self._sessions = {}
        self._server = None
        self._thread = None
        self._error = None
        self._started_at = None

    # ------------------------------------------------------------ 状态

    @property
    def url(self):
        return "http://%s:%d/mcp" % (HOST, self.port)

    def _log_line(self, text):
        if self._log:
            try:
                self._log(text)
                return
            except Exception:
                pass
        try:
            sys.stderr.write(text + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def state(self):
        running = self._server is not None
        sessions = self._prune_sessions()
        status = "running" if running else ("failed" if self._error else "stopped")
        return {
            "available": ats_mcp is not None,
            "status": status,
            "host": HOST,
            "port": self.port,
            "url": self.url,
            "sessions": sessions,
            "started_at": self._started_at,
            "error": self._error,
            "transport": "streamable-http",
            "protocol_version": (ats_mcp.SUPPORTED_PROTOCOLS[0] if ats_mcp else None),
            "config": self.config(),
        }

    def config(self):
        return {"mcpServers": {"agent-task-scheduler": {
            "type": "http",
            "url": self.url,
        }}}

    # ------------------------------------------------------------ 启停

    def start(self):
        """返回 (ok, error)。端口占用等失败原因原样带回，由界面展示。"""
        with self._lock:
            if self._server is not None:
                return True, None
            if ats_mcp is None:
                self._error = "同目录缺少 ats_mcp.py，无法复用工具表，MCP 端点不可用"
                return False, self._error
            try:
                server = _Server((HOST, self.port), _McpHandler)
            except OSError as exc:
                self._error = "端口 %s:%d 无法监听：%s（多半已被其他进程占用）" % (
                    HOST, self.port, exc)
                return False, self._error
            server.endpoint = self
            thread = threading.Thread(target=server.serve_forever,
                                      name="ats-mcp-http", daemon=True)
            thread.start()
            self._server = server
            self._thread = thread
            self._started_at = int(time.time())
            self._error = None
            self._log_line("[ats-mcp-http] listening on %s" % self.url)
            return True, None

    def stop(self):
        """关闭监听并释放端口；已在处理的请求自然结束。"""
        with self._lock:
            server = self._server
            self._server = None
            self._thread = None
            if server is None:
                self._error = None
                with self._sessions_lock:
                    self._sessions.clear()
                return True, None
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
            self._error = None
            with self._sessions_lock:
                self._sessions.clear()
            self._log_line("[ats-mcp-http] stopped, port %d released" % self.port)
            return True, None

    # ------------------------------------------------------------ 会话

    def new_session(self, user_agent=""):
        sid = secrets.token_hex(16)
        now = time.time()
        with self._sessions_lock:
            self._sessions[sid] = {"created": now, "seen": now,
                                   "ua": (user_agent or "")[:120]}
        return sid

    def touch_session(self, sid):
        if not sid:
            return None
        with self._sessions_lock:
            item = self._sessions.get(sid)
            if item is None:
                return None
            item["seen"] = time.time()
            return sid

    def drop_session(self, sid):
        with self._sessions_lock:
            return self._sessions.pop(sid, None) is not None

    def _prune_sessions(self):
        cutoff = time.time() - SESSION_IDLE
        with self._sessions_lock:
            for sid in [k for k, v in self._sessions.items() if v["seen"] < cutoff]:
                self._sessions.pop(sid, None)
            return len(self._sessions)

    # ------------------------------------------------------------ 分发

    def handle_rpc(self, message):
        """复用 ats_mcp 的协议分发（含 initialize 协商、错误码、isError 结果）。"""
        ats_mcp.ROOT = self.root
        return ats_mcp._handle(message)


class _McpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AtsMcp/1.0"

    # ------------------------------------------------------------ 基础

    def log_message(self, fmt, *args):
        ep = self._ep()
        if ep:
            ep._log_line("[ats-mcp-http] %s %s" % (self.command, fmt % args))

    def _ep(self):
        return getattr(self.server, "endpoint", None)

    def _send(self, obj, code=200, headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_empty(self, code, headers=None):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _error(self, code, err, message):
        self._send({"error": err, "message": message}, code)

    def _check_origin(self):
        """Host 必须是 127.0.0.1/localhost 加本端口；浏览器 Origin 只允许同源。"""
        ep = self._ep()
        host = (self.headers.get("Host") or "").strip().lower()
        allowed = {"%s:%d" % (HOST, ep.port), "localhost:%d" % ep.port}
        if host and host not in allowed:
            self._error(403, "forbidden_host",
                        "只接受 Host: %s:%d 或 localhost:%d" % (HOST, ep.port, ep.port))
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin:
            origins = {"http://%s:%d" % (HOST, ep.port),
                       "http://localhost:%d" % ep.port}
            if origin not in origins:
                self._error(403, "forbidden_origin",
                            "Origin %s 不受信任：MCP 端点只接受本机同源请求，"
                            "且不开放 CORS" % origin)
                return False
        return True

    def _path(self):
        return urlsplit(self.path).path

    # ------------------------------------------------------------ POST /mcp

    def do_POST(self):
        ep = self._ep()
        if ep is None:
            self._error(503, "not_running", "MCP 端点未启动")
            return
        if self._path() != "/mcp":
            self._error(404, "not_found", "未知路径，MCP 端点是 POST /mcp")
            return
        if not self._check_origin():
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._error(415, "unsupported_media_type",
                        "Content-Type 必须是 application/json")
            return
        accept = (self.headers.get("Accept") or "").lower()
        if accept and not any(x in accept for x in
                              ("application/json", "text/event-stream", "*/*")):
            self._error(406, "not_acceptable",
                        "Accept 需包含 application/json 或 text/event-stream")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._error(400, "empty_body", "请求体为空")
            return
        if length > MAX_BODY:
            self._error(413, "body_too_large", "请求体超过 %d 字节" % MAX_BODY)
            return
        raw = self.rfile.read(length)
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._send({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error: %s" % exc}})
            return

        sid = self.headers.get("Mcp-Session-Id")
        is_init = isinstance(message, dict) and message.get("method") == "initialize"
        headers = {}
        if sid:
            sid = ep.touch_session(sid)
            if sid is None:
                self._error(404, "session_not_found",
                            "Mcp-Session-Id 已失效，请重新 initialize")
                return
        elif is_init:
            sid = ep.new_session(self.headers.get("User-Agent") or "")
        if is_init and sid:
            headers["Mcp-Session-Id"] = sid

        try:
            reply = ep.handle_rpc(message)
        except Exception as exc:  # 单个请求的意外不应该打断进程
            self._send({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32603, "message": "Internal error: %s" % exc}},
                       headers=headers)
            return
        if reply is None:
            self._send_empty(202, headers)   # 通知：按协议不回响应体
            return
        self._send(reply, 200, headers)

    # ------------------------------------------------------------ GET / DELETE

    def do_GET(self):
        ep = self._ep()
        if ep is None:
            self._error(503, "not_running", "MCP 端点未启动")
            return
        if not self._check_origin():
            return
        path = self._path()
        if path == "/health":
            self._send({"ok": True, "state": ep.state()})
            return
        if path == "/mcp":
            # 规范：服务端不提供 SSE 流时必须回 405。
            self._send({"error": "method_not_allowed",
                        "message": "本端点不提供服务端推送流；请用 POST /mcp"},
                       code=405, headers={"Allow": "POST, DELETE"})
            return
        self._error(404, "not_found", "未知路径")

    def do_DELETE(self):
        ep = self._ep()
        if ep is None:
            self._error(503, "not_running", "MCP 端点未启动")
            return
        if self._path() != "/mcp":
            self._error(404, "not_found", "未知路径")
            return
        if not self._check_origin():
            return
        sid = self.headers.get("Mcp-Session-Id")
        if not sid or not ep.drop_session(sid):
            self._error(404, "session_not_found", "会话不存在或已结束")
            return
        self._send({"ok": True, "closed": sid})

    def do_OPTIONS(self):
        self._send({"error": "method_not_allowed",
                    "message": "MCP 端点不开放 CORS"},
                   code=405, headers={"Allow": "POST, DELETE"})
