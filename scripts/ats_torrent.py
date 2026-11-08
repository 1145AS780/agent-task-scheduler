#!/usr/bin/env python3
"""ats_torrent.py — agent-task-scheduler 的 BitTorrent worker (v2.7)。

由 `ats.py torrent` 自动派生，**不面向人直接使用**。它读取一份 torrent spec
（JSON），拉起一个只监听 127.0.0.1 的 aria2c RPC 实例，把种子下到目标目录，
并把进度以 JSONL 打到 stdout（由 supervisor 收集）。

依赖
- aria2c：优先读环境变量 ATS_ARIA2，其次 PATH，再次 WinGet / scoop / choco 常见目录

退出码
- 0 下载完成
- 1 下载失败 / 运行时错误
- 2 spec 非法
- 3 找不到 aria2c

stdout 协议（JSONL，每行一个对象）
- {"type":"aria2_start","exe":...,"port":...}
- {"type":"torrent_start","gid":...,"source":...}
- {"type":"torrent_metadata","gid":...,"followed_by":...,"name":...}
- {"type":"torrent_progress","gid":...,"status":...,"percent":...}
- {"type":"torrent_done",...}
- {"type":"torrent_error",...}
- {"type":"summary",...}      最后一行
"""

import argparse
import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

RPC_TIMEOUT = 10
START_TIMEOUT = 20
POLL_DEFAULT = 2
LOG_EVERY = 5
SHUTDOWN_TIMEOUT = 10

STATUS_KEYS = ["gid", "status", "totalLength", "completedLength", "downloadSpeed",
               "uploadSpeed", "connections", "numSeeders", "seeder", "errorCode",
               "errorMessage", "files", "bittorrent", "followedBy", "following", "dir"]


# ---------------------------------------------------------------- 工具

def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or time.time()))


def log(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def find_aria2c():
    """按 环境变量 -> PATH -> 常见安装目录 的顺序解析 aria2c 可执行文件。"""
    env = os.environ.get("ATS_ARIA2")
    if env and Path(env).is_file():
        return str(Path(env))
    for name in ("aria2c.exe", "aria2c"):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if base.is_dir():
            # winget 解包目录名形如 aria2.aria2_Microsoft.Winget.Source_<hash>/aria2-<ver>-win-64bit-build1/
            candidates.extend(sorted(base.glob("aria2*/**/aria2c.exe"), reverse=True))
    candidates.extend([
        Path(r"C:\Program Files\aria2\aria2c.exe"),
        Path(r"C:\Program Files (x86)\aria2\aria2c.exe"),
        Path.home() / "scoop" / "shims" / "aria2c.exe",
        Path(r"C:\ProgramData\chocolatey\bin\aria2c.exe"),
    ])
    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message or "rpc_error"


def rpc_call(port, secret, method, params=None, timeout=RPC_TIMEOUT):
    payload = {
        "jsonrpc": "2.0",
        "id": "ats",
        "method": method,
        "params": ["token:" + secret] + list(params or []),
    }
    req = urllib.request.Request(
        "http://127.0.0.1:%d/jsonrpc" % port,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(body)
    except ValueError:
        raise RpcError(-1, "invalid_rpc_response")
    if isinstance(parsed, dict) and parsed.get("error"):
        err = parsed["error"]
        raise RpcError(err.get("code"), err.get("message"))
    return parsed.get("result") if isinstance(parsed, dict) else None


def wait_rpc(port, secret, proc, seconds=START_TIMEOUT):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            rpc_call(port, secret, "aria2.getVersion", timeout=2)
            return True
        except (urllib.error.URLError, OSError, RpcError, ValueError):
            time.sleep(0.3)
    return False


def tail(path, lines=6):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


# ---------------------------------------------------------------- aria2 生命周期

def start_aria2(aria2, spec, port, secret, out_dir):
    log_path = str(Path(spec.get("log") or (Path(out_dir) / "aria2.log")))
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    argv = [
        aria2,
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        "--rpc-listen-port=%d" % port,
        "--rpc-secret=%s" % secret,
        "--dir=%s" % out_dir,
        "--continue=true",
        "--file-allocation=none",
        "--seed-time=%s" % spec.get("seed_time", 0),
        "--max-overall-download-limit=%d" % int(spec.get("max_download") or 0),
        "--max-overall-upload-limit=%d" % int(spec.get("max_upload") or 0),
        "--auto-file-renaming=false",
        "--auto-save-interval=10",
        "--quiet=true",
        "--summary-interval=0",
        "--log=%s" % log_path,
        "--log-level=notice",
    ]
    if os.name == "nt":
        # 父进程（本 worker）消失时 aria2 自行退出，避免 taskkill 只杀掉 worker 而留下孤儿
        argv.append("--stop-with-process=%d" % os.getpid())
    creationflags = 0x00000200 if os.name == "nt" else 0   # CREATE_NEW_PROCESS_GROUP
    out_log = Path(log_path).with_name("aria2.out.log")
    fh = out_log.open("a", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                            creationflags=creationflags, close_fds=True)
    return proc, fh, log_path


def stop_aria2(port, secret, proc):
    try:
        rpc_call(port, secret, "aria2.shutdown", timeout=3)
    except (urllib.error.URLError, OSError, RpcError, ValueError):
        pass
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT)
        return True
    except subprocess.TimeoutExpired:
        try:
            rpc_call(port, secret, "aria2.forceShutdown", timeout=3)
        except (urllib.error.URLError, OSError, RpcError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        return False


# ---------------------------------------------------------------- 下载

def add_download(port, secret, spec):
    source = spec["source"]
    if source["type"] == "magnet":
        gid = rpc_call(port, secret, "aria2.addUri", [[source["value"]]])
        return gid, "magnet"
    blob = base64.b64encode(Path(source["path"]).read_bytes()).decode("ascii")
    gid = rpc_call(port, secret, "aria2.addTorrent", [blob, []])
    return gid, "torrent"


def tell(port, secret, gid):
    return rpc_call(port, secret, "aria2.tellStatus", [gid, STATUS_KEYS])


def torrent_name(status):
    info = ((status.get("bittorrent") or {}).get("info") or {})
    name = info.get("name")
    if name:
        return name
    for item in status.get("files") or []:
        path = item.get("path")
        if path:
            return Path(path).name
    return None


def selected_files(status):
    out = []
    for item in status.get("files") or []:
        if str(item.get("selected", "true")).lower() == "false":
            continue
        path = item.get("path")
        if path and not path.endswith(".aria2"):
            out.append({"path": path, "length": int(item.get("length") or 0)})
    return out


def is_fully_downloaded(status):
    total = int(status.get("totalLength") or 0)
    done = int(status.get("completedLength") or 0)
    return total > 0 and done >= total


def progress_line(gid, status, elapsed, seed_time, download_done_at):
    total = int(status.get("totalLength") or 0)
    done = int(status.get("completedLength") or 0)
    percent = round(done * 100.0 / total, 2) if total else 0.0
    seeding = str(status.get("seeder") or "").lower() == "true" and is_fully_downloaded(status)
    row = {
        "type": "torrent_progress",
        "gid": gid,
        "status": status.get("status"),
        "percent": percent,
        "completed": done,
        "total": total,
        "download_speed": int(status.get("downloadSpeed") or 0),
        "upload_speed": int(status.get("uploadSpeed") or 0),
        "connections": int(status.get("connections") or 0),
        "seeders": int(status.get("numSeeders") or 0),
        "seeding": seeding,
        "name": torrent_name(status),
        "elapsed": round(elapsed, 2),
    }
    if seeding and seed_time > 0 and download_done_at is not None:
        row["seed_remaining"] = max(0, int(seed_time * 60 - (time.time() - download_done_at)))
    return row


def wait_download(port, secret, gid, poll, seed_time):
    started = time.time()
    last_log = 0.0
    last_status = None
    last_snapshot = None
    download_done_at = None
    while True:
        try:
            status = tell(port, secret, gid)
        except RpcError as exc:
            if last_snapshot is not None and is_fully_downloaded(last_snapshot):
                return {"ok": True, "status": last_snapshot,
                        "elapsed": round(time.time() - started, 2)}
            return {"ok": False, "error": "download_not_found",
                    "detail": exc.message, "rpc_code": exc.code,
                    "elapsed": round(time.time() - started, 2)}
        followed = status.get("followedBy") or []
        if followed and followed[-1] != gid:
            log({"type": "torrent_metadata", "gid": gid,
                 "followed_by": followed[-1], "name": torrent_name(status)})
            # 磁力链的元数据 gid 下完时报 complete（totalLength 只是 .torrent 的大小），
            # 真实内容在 followedBy 指向的新 gid 里——必须立刻换过去，不能按旧 gid 判定完成
            gid = followed[-1]
            last_snapshot = None
            last_status = None
            time.sleep(poll)
            continue
        last_snapshot = status
        state = status.get("status")
        now = time.time()
        if is_fully_downloaded(status):
            if download_done_at is None:
                download_done_at = now
            if state == "complete":
                return {"ok": True, "status": status, "gid": gid,
                        "elapsed": round(now - started, 2)}
            if seed_time <= 0:
                return {"ok": True, "status": status, "gid": gid,
                        "elapsed": round(now - started, 2)}
            if now - download_done_at >= float(seed_time) * 60:
                return {"ok": True, "status": status, "gid": gid,
                        "seeded": True, "elapsed": round(now - started, 2)}
        if state == "error":
            return {"ok": False, "gid": gid,
                    "error": "aria2_error_%s" % (status.get("errorCode") or "unknown"),
                    "detail": status.get("errorMessage"),
                    "elapsed": round(now - started, 2)}
        if state == "removed":
            return {"ok": False, "gid": gid, "error": "download_removed",
                    "elapsed": round(now - started, 2)}
        if now - last_log >= LOG_EVERY or state != last_status:
            log(progress_line(gid, status, now - started, seed_time, download_done_at))
            last_log = now
        last_status = state
        time.sleep(poll)


# ---------------------------------------------------------------- 入口

def load_spec(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("spec_invalid")
    source = data.get("source")
    if not isinstance(source, dict):
        raise ValueError("spec_missing_source")
    kind = source.get("type")
    if kind == "magnet":
        value = str(source.get("value") or "")
        if not value.startswith("magnet:"):
            raise ValueError("magnet_invalid")
    elif kind == "torrent":
        target = Path(str(source.get("path") or ""))
        if not target.is_file():
            raise ValueError("torrent_not_found")
    else:
        raise ValueError("source_type_invalid")
    return data


def run(spec):
    out_dir = Path(spec.get("outputs") or ".").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    poll = max(1.0, float(spec.get("poll") or POLL_DEFAULT))
    seed_time = max(0.0, float(spec.get("seed_time") or 0))

    pinned = spec.get("aria2")
    aria2 = pinned if pinned and Path(str(pinned)).is_file() else find_aria2c()
    if not aria2:
        log({"type": "torrent_error", "error": "aria2_not_found",
             "hint": "安装 aria2（winget install --id aria2.aria2 -e），"
                     "或用环境变量 ATS_ARIA2 指向 aria2c.exe"})
        log({"type": "summary", "status": "failed", "error": "aria2_not_found"})
        return 3

    port = free_port()
    secret = secrets.token_hex(12)
    log({"type": "aria2_start", "exe": aria2, "port": port,
         "outputs": str(out_dir), "seed_time": seed_time})
    proc, fh, log_path = start_aria2(aria2, spec, port, secret, out_dir)
    started = time.time()
    try:
        if not wait_rpc(port, secret, proc):
            code = proc.poll()
            detail = tail(log_path) or tail(Path(log_path).with_name("aria2.out.log"))
            log({"type": "torrent_error", "error": "aria2_start_failed",
                 "exit_code": code, "detail": detail})
            log({"type": "summary", "status": "failed", "error": "aria2_start_failed",
                 "exit_code": code, "elapsed": round(time.time() - started, 2)})
            return 1
        try:
            gid, kind = add_download(port, secret, spec)
        except RpcError as exc:
            log({"type": "torrent_error", "error": "aria2_add_failed",
                 "rpc_code": exc.code, "detail": exc.message})
            log({"type": "summary", "status": "failed", "error": "aria2_add_failed",
                 "elapsed": round(time.time() - started, 2)})
            return 1
        log({"type": "torrent_start", "gid": gid, "source": kind,
             "outputs": str(out_dir)})

        result = wait_download(port, secret, gid, poll, seed_time)
        if result["ok"]:
            status = result["status"]
            files = selected_files(status)
            log({"type": "torrent_done",
                 "gid": result.get("gid") or gid,
                 "name": torrent_name(status),
                 "bytes": int(status.get("totalLength") or 0),
                 "files": files,
                 "outputs": str(out_dir),
                 "seeded": bool(result.get("seeded")),
                 "elapsed": result["elapsed"]})
            log({"type": "summary", "status": "complete",
                 "name": torrent_name(status),
                 "bytes": int(status.get("totalLength") or 0),
                 "files": len(files), "outputs": str(out_dir),
                 "elapsed": result["elapsed"]})
            return 0
        log({"type": "torrent_error", "error": result.get("error"),
             "detail": result.get("detail"), "gid": result.get("gid") or gid,
             "elapsed": result.get("elapsed")})
        log({"type": "summary", "status": "failed", "error": result.get("error"),
             "detail": result.get("detail"),
             "outputs": str(out_dir), "elapsed": result.get("elapsed")})
        return 1
    except Exception:
        log({"type": "torrent_error", "error": "internal_error",
             "traceback": traceback.format_exc()})
        log({"type": "summary", "status": "failed", "error": "internal_error",
             "elapsed": round(time.time() - started, 2)})
        return 1
    finally:
        stop_aria2(port, secret, proc)
        try:
            fh.close()
        except OSError:
            pass


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="ats_torrent.py")
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    try:
        spec = load_spec(args.spec)
    except (OSError, ValueError) as exc:
        log({"type": "spec_error", "error": str(exc), "spec": args.spec})
        return 2
    try:
        return run(spec)
    except Exception:
        log({"type": "spec_error", "error": "internal_error",
             "traceback": traceback.format_exc()})
        return 1


if __name__ == "__main__":
    sys.exit(main())
