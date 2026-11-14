#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ats_guard.py — ATS 独立看门狗（零依赖 / 零 ctypes / 零内存读写）

========================= 硬编码安全警告 =========================
本文件是整条信任链的**终点**，它无法校验自己。

如果本文件的 SHA256 与 ats_guard_manifest.json 记录不符，说明看门狗
本身已被篡改 —— 此时本进程输出的任何「校验通过」都不可信，因为篡改者
可以同时改掉本文件的比对逻辑和期望值。

唯一正确动作：
  1. 人工用 git 历史 / 原始副本逐行比对 ats_guard.py；
  2. 确认改动来源可信后，再由人执行 seal --confirm 重新封签；
  3. 不要相信任何自动化流程给出的「已修复 / 已校验」结论。

防无限套娃的落点不在本文件内，而在 ats.py：
  * ats.py 启动时先自行比对 ats_guard.py 的 SHA256 与清单，再调用本文件
    校验其余核心文件 —— 不把信任完全委托给本进程；
  * ats.py 的 --skip-guard 仅供人类紧急调试，且强制触发 L3 审批；
  * 清单被改写无法由代码检测（本地写权限攻击），这条边界只能由人守：
    seal 必须由人执行，并且必须显式加 --confirm。

========================= 口令闸门 =========================
防的是「AI 与我同等权限」这一威胁模型：AI 能敲命令行、能改文件，所以
「谁在敲」不能靠参数判断（实测 AI 通道 stdin.isatty() 同样是 True）。
改为靠「知不知道口令」：

  * ats_guard_key.json 只存 PBKDF2-HMAC-SHA256 的盐与摘要，不存明文；
  * seal / init / approve / 削弱型 policy --set 都要求口令；
  * 密钥文件本身的 SHA256 记进清单 —— 否则换成攻击者自己的口令摘要即可；
  * init 在清单已存在时拒绝执行 —— 没有「重置」入口，丢了只能靠 git 恢复。

残余边界（不隐瞒）：同时删掉清单与密钥文件后重跑 init + seal 仍可重建
信任基线。本地文件权限相等时，代码无法阻止删除，这条只能由人守。
==================================================================

用法（--root 省略时默认 <ats 本体目录>/.ats，即 OCSKIS/.ats；也可用 ATS_ROOT 环境变量）：
    python ats_guard.py verify [--root DIR] [--json]
    python ats_guard.py seal   [--root DIR] --confirm [--passphrase-stdin]
    python ats_guard.py init   [--root DIR] --confirm [--passphrase-stdin]
    python ats_guard.py auth   [--root DIR] [--passphrase-stdin]
    python ats_guard.py self

退出码：0 通过 / 2 用法错误 / 3 清单缺失 / 4 校验失败
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

MANIFEST_NAME = "ats_guard_manifest.json"
MANIFEST_VERSION = 1
GUARD_FILE = "ats_guard.py"
SCRIPT_DIR = Path(__file__).resolve().parent

KEY_NAME = "ats_guard_key.json"
KEY_VERSION = 1
KDF_NAME = "pbkdf2-sha256"
KDF_ITERATIONS = 200000
PASSPHRASE_MIN = 8

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_MANIFEST = 3
EXIT_MISMATCH = 4


def sha256_file(path):
    """流式算 sha256；文件不存在或读失败返回 None，不抛异常。"""
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def core_files():
    """核心文件集合：scripts 目录下全部 .py，按名字排序。"""
    return sorted(p.name for p in SCRIPT_DIR.glob("*.py"))


def resolve_root(root):
    """解析状态根。优先级：显式 root > 环境变量 ATS_ROOT > <ats 本体目录>/.ats。

    默认取脚本所在目录的父目录（即 OCSKIS/），而不是当前工作目录：
    否则从不同 cwd 调用会各自生成一份 .ats，信任根被拆散。
    """
    if root:
        return Path(root)
    env = os.environ.get("ATS_ROOT")
    if env:
        return Path(env)
    return SCRIPT_DIR.parent / ".ats"


def manifest_path(root):
    env = os.environ.get("ATS_GUARD_MANIFEST")
    if env:
        return Path(env)
    return Path(root) / MANIFEST_NAME


def read_manifest(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:                    # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def write_manifest(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def key_path(root):
    return Path(root) / KEY_NAME


def read_key(root):
    path = key_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                    # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def hash_passphrase(passphrase, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"),
                               bytes.fromhex(salt), iterations).hex()


def read_passphrase(use_stdin):
    """口令只从 stdin 或交互输入读，绝不从 argv 读（argv 会留在进程列表与历史里）。

    交互提示要求 stdin 与 stdout 同时是终端：实测只看 stdin.isatty() 不够，
    被管道抓取输出时它仍返回 True，此时 getpass 会去读控制台并永久阻塞。
    非交互通道一律快速失败，由调用方给出「用 --passphrase-stdin」的提示。
    """
    if use_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n") if line else ""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return ""
    except Exception:                    # noqa: BLE001
        return ""
    try:
        import getpass
        return getpass.getpass("passphrase: ")
    except Exception:                    # noqa: BLE001
        return ""


def check_passphrase(root, passphrase):
    """校验口令；密钥文件缺失/损坏/格式不符一律判失败（fail-closed）。"""
    if not passphrase:
        return {"ok": False, "error": "passphrase_missing",
                "hint": "口令不能为空：用 --passphrase-stdin 从标准输入提供一行，"
                        "或直接在终端里交互输入"}
    key = read_key(root)
    if key is None:
        return {"ok": False, "error": "key_missing", "key": str(key_path(root)),
                "hint": "密钥文件不存在：由人执行 init --confirm 创建"}
    if key.get("kdf") != KDF_NAME or key.get("version") != KEY_VERSION:
        return {"ok": False, "error": "key_format",
                "kdf": key.get("kdf"), "version": key.get("version")}
    try:
        iterations = int(key.get("iterations"))
        salt = key.get("salt")
        expected = key.get("hash")
        actual = hash_passphrase(passphrase, salt, iterations)
    except Exception:                    # noqa: BLE001
        return {"ok": False, "error": "key_corrupt", "key": str(key_path(root))}
    if actual != expected:
        return {"ok": False, "error": "passphrase_mismatch"}
    return {"ok": True, "iterations": iterations}


def collect(root):
    files = {}
    for name in core_files():
        files[name] = sha256_file(SCRIPT_DIR / name)
    policy_path = Path(root) / "policy.json"
    policy = None
    if policy_path.is_file():
        policy = {"path": str(policy_path), "sha256": sha256_file(policy_path)}
    key_file = key_path(root)
    key = None
    if key_file.is_file():
        key = {"path": str(key_file), "sha256": sha256_file(key_file)}
    return files, policy, key


def emit(payload, code):
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def cmd_self():
    path = SCRIPT_DIR / GUARD_FILE
    return emit({"ok": True, "file": GUARD_FILE, "path": str(path),
                 "sha256": sha256_file(path)}, EXIT_OK)


def cmd_init(root, confirm, passphrase):
    """创建密钥文件。清单或密钥任一已存在时拒绝 —— 没有「重置」入口。"""
    if not confirm:
        return emit({"ok": False, "error": "confirm_required",
                     "hint": "创建密钥会建立信任根，必须由人显式确认：init --confirm"},
                    EXIT_USAGE)
    # 只删清单、留下密钥，曾能用新口令覆盖信任根 —— 密钥本身不可合法再生，
    # 它存在就说明本 root 已初始化过，因此两个产物任一存在都拒绝。
    existing = [str(p) for p in (manifest_path(root), key_path(root)) if p.is_file()]
    if existing:
        return emit({"ok": False, "error": "already_initialized",
                     "manifest": str(manifest_path(root)),
                     "existing": existing,
                     "hint": "清单或密钥已存在，禁止重建密钥。本仓的 ats_guard_key.json "
                             "与 ats_guard_manifest.json 当前未被 git 跟踪，无法从 git 恢复；"
                             "确认要全新开始，须由人先手动删除这两个文件"},
                    EXIT_MISMATCH)
    if len(passphrase) < PASSPHRASE_MIN:
        return emit({"ok": False, "error": "passphrase_too_short",
                     "min_length": PASSPHRASE_MIN}, EXIT_USAGE)
    salt = os.urandom(16).hex()
    record = {
        "version": KEY_VERSION,
        "kdf": KDF_NAME,
        "iterations": KDF_ITERATIONS,
        "salt": salt,
        "hash": hash_passphrase(passphrase, salt, KDF_ITERATIONS),
        "created_at": int(time.time()),
        "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = key_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(path, record)
    return emit({"ok": True, "key": str(path), "kdf": KDF_NAME,
                 "iterations": KDF_ITERATIONS}, EXIT_OK)


def cmd_auth(root, passphrase):
    result = check_passphrase(root, passphrase)
    return emit(result, EXIT_OK if result.get("ok") else EXIT_MISMATCH)


def cmd_seal(root, confirm, passphrase):
    if not confirm:
        return emit({"ok": False, "error": "confirm_required",
                     "hint": "封签会重建信任基线，必须由人显式确认：seal --confirm"},
                    EXIT_USAGE)
    auth = check_passphrase(root, passphrase)
    if not auth.get("ok"):
        return emit({"ok": False, "error": "passphrase_required",
                     "detail": auth,
                     "hint": "重建信任基线需要人工口令。密钥文件：%s" % key_path(root)},
                    EXIT_MISMATCH)
    if sha256_file(SCRIPT_DIR / "ats.py") is None:
        return emit({"ok": False, "error": "ats_missing",
                     "hint": "ats.py 不在本目录，拒绝生成不完整清单"}, EXIT_USAGE)

    path = manifest_path(root)
    previous = read_manifest(path) or {}
    files, policy, key = collect(root)

    old_files = previous.get("files") or {}
    changed = [name for name, digest in files.items() if old_files.get(name) != digest]
    old_policy = (previous.get("policy") or {}).get("sha256")
    new_policy = (policy or {}).get("sha256")
    if old_policy != new_policy:
        changed.append("policy.json")
    old_key = (previous.get("key") or {}).get("sha256")
    new_key = (key or {}).get("sha256")
    if old_key != new_key:
        changed.append(KEY_NAME)

    record = {
        "version": MANIFEST_VERSION,
        "sealed_at": int(time.time()),
        "sealed_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sealed_by": "human-with-passphrase",
        "files": files,
        "policy": policy,
        "key": key,
    }
    write_manifest(path, record)
    return emit({"ok": True, "manifest": str(path), "files": files,
                 "policy": policy, "key": key, "changed": changed}, EXIT_OK)


def cmd_verify(root):
    path = manifest_path(root)
    manifest = read_manifest(path)
    if manifest is None:
        # 清单缺失有两种前置条件，给出的下一步不能一样：seal 需要口令校验，
        # 而全新 root 还没有密钥文件，照着「去 seal」跑只会撞 key_missing。
        if key_path(root).is_file():
            return emit({"ok": False, "error": "manifest_missing",
                         "manifest": str(path),
                         "next_action": "seal",
                         "hint": "清单缺失但密钥仍在：由人执行 seal --confirm 重新封签，"
                                 "口令须与 init 时一致"},
                        EXIT_NO_MANIFEST)
        return emit({"ok": False, "error": "manifest_missing",
                     "manifest": str(path),
                     "next_action": "init",
                     "hint": "本 root 尚未建立信任根：由人先执行 init --confirm 创建密钥，"
                             "再执行 seal --confirm 建立基线"},
                    EXIT_NO_MANIFEST)
    if manifest.get("version") != MANIFEST_VERSION:
        return emit({"ok": False, "error": "manifest_version",
                     "manifest": str(path),
                     "expected": MANIFEST_VERSION,
                     "actual": manifest.get("version")}, EXIT_MISMATCH)

    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        return emit({"ok": False, "error": "manifest_empty",
                     "manifest": str(path)}, EXIT_MISMATCH)

    current = {name: sha256_file(SCRIPT_DIR / name) for name in core_files()}
    problems = []
    for name, digest in current.items():
        if name not in expected:
            problems.append({"file": name, "error": "unsealed_file"})
        elif digest != expected.get(name):
            problems.append({"file": name, "error": "digest_mismatch"})
    for name in expected:
        if name not in current:
            problems.append({"file": name, "error": "missing_file"})

    policy_path = Path(root) / "policy.json"
    actual_policy = sha256_file(policy_path) if policy_path.is_file() else None
    entry = manifest.get("policy")
    if isinstance(entry, dict):
        if actual_policy is None:
            problems.append({"file": "policy.json", "error": "missing_file"})
        elif actual_policy != entry.get("sha256"):
            problems.append({"file": "policy.json", "error": "digest_mismatch"})
    elif actual_policy is not None:
        problems.append({"file": "policy.json", "error": "policy_unsealed"})

    key_file = key_path(root)
    actual_key = sha256_file(key_file) if key_file.is_file() else None
    key_entry = manifest.get("key")
    if isinstance(key_entry, dict):
        if actual_key is None:
            problems.append({"file": KEY_NAME, "error": "missing_file"})
        elif actual_key != key_entry.get("sha256"):
            problems.append({"file": KEY_NAME, "error": "digest_mismatch"})
    elif actual_key is not None:
        problems.append({"file": KEY_NAME, "error": "key_unsealed"})

    # 密钥丢失是唯一「照着 seal 走也修不好」的失败：seal 要先口令校验，
    # 口令校验要读密钥文件。这里必须把下一步说清，否则调用方只会提示重封签。
    key_missing = any(p.get("file") == KEY_NAME and p.get("error") == "missing_file"
                      for p in problems)
    payload = {"ok": not problems, "manifest": str(path),
               "checked": len(current), "problems": problems}
    if key_missing:
        payload["next_action"] = "restore_key"
        payload["hint"] = ("密钥文件丢失，看门狗无法自愈：seal 需要口令校验，"
                           "而口令校验要读该文件。请从 git 恢复 ats_guard_key.json "
                           "与 ats_guard_manifest.json；确认要全新开始，须由人先手动"
                           "删除这两个文件，再重跑 init --confirm 与 seal --confirm")
    return emit(payload, EXIT_OK if not problems else EXIT_MISMATCH)


def parse_args(argv):
    root = None
    confirm = False
    passphrase_stdin = False
    action = None
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in ("--root",):
            if i + 1 < len(argv):
                root = argv[i + 1]
                i += 2
                continue
            return None, None, False, False
        if token.startswith("--root="):
            root = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--confirm":
            confirm = True
            i += 1
            continue
        if token == "--passphrase-stdin":
            passphrase_stdin = True
            i += 1
            continue
        if token == "--json":
            i += 1
            continue
        if action is None and not token.startswith("-"):
            action = token
            i += 1
            continue
        return None, None, False, False
    return action, root, confirm, passphrase_stdin


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action, root, confirm, passphrase_stdin = parse_args(argv)
    if action not in ("verify", "seal", "init", "auth", "self"):
        return emit({"ok": False, "error": "usage",
                     "usage": "python ats_guard.py verify|seal|init|auth|self "
                              "[--root DIR] [--confirm] [--passphrase-stdin] [--json]"},
                    EXIT_USAGE)
    root = resolve_root(root)
    if action == "verify":
        return cmd_verify(root)
    if action == "auth":
        return cmd_auth(root, read_passphrase(passphrase_stdin))
    if action == "init":
        return cmd_init(root, confirm, read_passphrase(passphrase_stdin))
    if action == "seal":
        return cmd_seal(root, confirm, read_passphrase(passphrase_stdin))
    return cmd_self()


if __name__ == "__main__":
    sys.exit(main())
