#!/usr/bin/env python3
"""ats_download.py — agent-task-scheduler 的下载 worker。

由 `ats.py download` 自动派生，**不面向人直接使用**。它读取一份 download spec
（JSON），把条目下载到本地，把进度以 JSONL 打到 stdout（由 supervisor 收集）。

特性
- HTTP(S) 单文件 / 批量清单
- Range 断点续传（半成品落在 `<目标>.part`）
- 每项独立重试 + 线性退避
- 大小 / SHA-256 完整性校验
- 全局速率限制（字节/秒，跨并发共享）
- 并发下载（线程池）
- 代理、自定义请求头、可跳过证书校验

退出码
- 0 全部成功（含「目标已存在且校验通过」的跳过）
- 1 至少一项失败
- 2 spec 非法

stdout 协议（JSONL，每行一个对象）
- {"type":"item_start", ...}
- {"type":"item_retry", ...}
- {"type":"item_done", ...}
- {"type":"item_error", ...}
- {"type":"summary", ...}      最后一行
"""

import argparse
import http.client
import hashlib
import json
import os
import ssl
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import error, request

CHUNK = 64 * 1024
USER_AGENT = "ats-download/1.0"


# ---------------------------------------------------------------- 工具

def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or time.time()))


def log(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def guess_name(url):
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name or "download.bin"


class RateLimiter:
    """全局限速：所有线程共用一条「已传字节 / 已耗时」曲线。"""

    def __init__(self, bps):
        try:
            self.bps = int(bps or 0)
        except (TypeError, ValueError):
            self.bps = 0
        self.start = time.time()
        self.total = 0
        self.lock = threading.Lock()

    def consume(self, n):
        if self.bps <= 0:
            return
        with self.lock:
            self.total += n
            expected = self.total / float(self.bps)
            elapsed = time.time() - self.start
            if expected > elapsed:
                time.sleep(expected - elapsed)


def make_opener(spec):
    handlers = []
    proxy = spec.get("proxy")
    if proxy:
        handlers.append(request.ProxyHandler({"http": proxy, "https": proxy}))
    if spec.get("insecure"):
        ctx = ssl._create_unverified_context()
        handlers.append(request.HTTPSHandler(context=ctx))
    return request.build_opener(*handlers) if handlers else request.build_opener()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify_file(path, item, spec):
    """校验已存在的文件。返回 (ok, detail)；没有可校验信息时 ok 但 detail 标记 unverified。"""
    size = item.get("size")
    sha = (item.get("sha256") or "").lower()
    actual = path.stat().st_size
    if spec.get("verify_size", True) and size:
        if int(size) != actual:
            return False, "size_mismatch(expected=%s,actual=%s)" % (size, actual)
    if spec.get("verify_sha256", True) and sha:
        got = file_sha256(path)
        if got.lower() != sha:
            return False, "sha256_mismatch(expected=%s,actual=%s)" % (sha, got)
    if not size and not sha:
        # 既无 size 也无 sha256：无法判断已有文件是否完整，必须显式告知，不能静默当成已校验
        return True, "unverified(no_size_no_sha256)"
    return True, "ok"


# ---------------------------------------------------------------- 单项下载

class ItemError(Exception):
    pass


def build_request(url, item, spec, offset):
    headers = {"User-Agent": USER_AGENT}
    headers.update({str(k): str(v) for k, v in (spec.get("headers") or {}).items()})
    headers.update({str(k): str(v) for k, v in (item.get("headers") or {}).items()})
    if offset > 0:
        headers["Range"] = "bytes=%d-" % offset
    return request.Request(url, headers=headers)


def attempt_once(item, spec, limiter, opener, base_dir):
    url = item["url"]
    save_as = item.get("save_as") or guess_name(url)
    target = Path(base_dir) / save_as
    part = target.with_name(target.name + ".part")

    if target.exists() and not spec.get("overwrite"):
        ok, detail = verify_file(target, item, spec)
        if ok:
            if detail != "ok":
                # 无 size/sha256 时无法确认已有文件完整，明确告警而不是静默放行
                log({"type": "item_warning", "url": url, "save_as": save_as,
                     "warning": "existing_file_unverified", "detail": detail,
                     "hint": "spec 未提供 size/sha256；如需重下请加 --overwrite"})
            return {"status": "existing", "path": str(target),
                    "bytes": target.stat().st_size, "verify": detail}
        if spec.get("verify_sha256", True) and (item.get("sha256") or item.get("size")):
            # 已有文件校验不通过：按覆盖处理，重新下载
            pass

    offset = part.stat().st_size if (spec.get("resume", True) and part.exists()) else 0
    resumed_from = offset

    req = build_request(url, item, spec, offset)
    timeout = int(spec.get("io_timeout") or 30)
    try:
        resp = opener.open(req, timeout=timeout)
    except error.HTTPError as exc:
        if exc.code == 416 and offset > 0:
            total = _total_from_range_error(exc)
            if total is not None and offset == total:
                _finish(part, target)
                return {"status": "ok", "path": str(target), "bytes": offset,
                        "resumed_from": resumed_from, "verify": "ok"}
        raise ItemError("http_%s" % exc.code)
    except error.URLError as exc:
        raise ItemError("url_error: %s" % exc.reason)
    except http.client.HTTPException as exc:
        raise ItemError("protocol_error: %s" % exc)

    code = getattr(resp, "status", None) or resp.getcode()
    if code == 206:
        mode = "ab"
    elif code == 200:
        offset = 0                     # 服务端不支持 Range，只能重下
        resumed_from = 0               # 实际从 0 开始写，不能沿用旧的断点值上报
        mode = "wb"
    else:
        resp.close()
        raise ItemError("http_%s" % code)

    total = _total_size(resp, offset)
    digest = None
    sha_expected = (item.get("sha256") or "").lower()
    if spec.get("verify_sha256", True) and sha_expected and offset > 0:
        # 续传时先把已有半成品纳入哈希
        digest = hashlib.sha256()
        with open(part, "rb") as fh:
            while True:
                block = fh.read(CHUNK)
                if not block:
                    break
                digest.update(block)
    elif spec.get("verify_sha256", True) and sha_expected:
        digest = hashlib.sha256()

    written = 0
    with open(part, mode) as fh:
        try:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                fh.write(block)
                written += len(block)
                limiter.consume(len(block))
                if digest is not None:
                    digest.update(block)
        except (error.URLError, OSError, http.client.HTTPException) as exc:
            resp.close()
            raise ItemError("stream_error: %s" % exc)
    resp.close()

    actual = offset + written
    if total is not None and actual < total:
        raise ItemError("incomplete(%s/%s)" % (actual, total))
    size_expected = item.get("size")
    if spec.get("verify_size", True) and size_expected and int(size_expected) != actual:
        raise ItemError("size_mismatch(expected=%s,actual=%s)" % (size_expected, actual))
    if digest is not None and sha_expected and digest.hexdigest().lower() != sha_expected:
        try:
            part.unlink()
        except OSError:
            pass
        raise ItemError("sha256_mismatch(expected=%s,actual=%s)"
                        % (sha_expected, digest.hexdigest()))

    _finish(part, target)
    return {"status": "ok", "path": str(target), "bytes": actual,
            "resumed_from": resumed_from, "verify": "ok"}


def _total_from_range_error(exc):
    value = exc.headers.get("Content-Range", "") if exc.headers else ""
    if "/" in value:
        try:
            return int(value.rsplit("/", 1)[1])
        except ValueError:
            return None
    return None


def _total_size(resp, offset):
    value = resp.headers.get("Content-Range", "")
    if "/" in value:
        try:
            return int(value.rsplit("/", 1)[1])
        except ValueError:
            return None
    length = resp.headers.get("Content-Length")
    if length:
        try:
            return offset + int(length)
        except ValueError:
            return None
    return None


def _finish(part, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(part), str(target))


def download_item(item, spec, limiter, opener, base_dir):
    url = item["url"]
    save_as = item.get("save_as") or guess_name(url)
    attempts = int(spec.get("retry", 2) or 0) + 1
    backoff = int(spec.get("retry_backoff", 3) or 0)
    log({"type": "item_start", "url": url, "save_as": save_as})
    last = None
    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            result = attempt_once(item, spec, limiter, opener, base_dir)
            log({"type": "item_done", "url": url, "path": result["path"],
                 "status": result["status"], "bytes": result["bytes"],
                 "resumed_from": result.get("resumed_from", 0),
                 "attempt": attempt, "elapsed": round(time.time() - started, 2)})
            return {"url": url, "ok": True, "path": result["path"],
                    "status": result["status"], "bytes": result["bytes"],
                    "attempts": attempt, "error": None}
        except (ItemError, OSError) as exc:
            last = str(exc)
            log({"type": "item_retry" if attempt < attempts else "item_error",
                 "url": url, "attempt": attempt, "error": last})
            if attempt < attempts and backoff > 0:
                time.sleep(backoff * attempt)
    return {"url": url, "ok": False, "path": None, "status": "failed",
            "bytes": 0, "attempts": attempts, "error": last}


# ---------------------------------------------------------------- 入口

def load_spec(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("spec_invalid")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("spec_missing_items")
    for item in items:
        if not isinstance(item, dict) or not item.get("url"):
            raise ValueError("item_missing_url")
    return data


def run(spec):
    base_dir = Path(spec.get("outputs") or ".").expanduser()
    base_dir.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(spec.get("rate_limit"))
    opener = make_opener(spec)
    concurrency = max(1, int(spec.get("concurrency") or 1))
    started = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(
            lambda it: download_item(it, spec, limiter, opener, base_dir),
            spec["items"]))

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    summary = {
        "type": "summary",
        "total": len(results),
        "succeeded": len(ok),
        "failed": len(bad),
        "bytes": sum(int(r["bytes"] or 0) for r in ok),
        "elapsed": round(time.time() - started, 2),
        "outputs": str(base_dir),
        "items": results,
    }
    log(summary)
    return 0 if not bad else 1


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="ats_download.py")
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
