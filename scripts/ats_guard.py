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

用法（--root 省略时默认 <ats 本体目录>/.ats，即仓库根目录下的 .ats；也可用 ATS_ROOT 环境变量）：
    python ats_guard.py verify [--root DIR] [--json]
    python ats_guard.py seal   [--root DIR] --confirm [--passphrase-stdin]
    python ats_guard.py init   [--root DIR] --confirm [--passphrase-stdin]
    python ats_guard.py auth   [--root DIR] [--passphrase-stdin]
    python ats_guard.py self
    python ats_guard.py visa issue --number N --time MINUTES --passphrase-stdin [--note TEXT]
    python ats_guard.py visa issue --time MINUTES --scope-type command
                         --scope-hash SHA256 --task-id TASK [--number N]
    python ats_guard.py visa issue --time MINUTES --scope-type plan
                         --scope-hash SHA256 --plan-id PLAN [--number unlimited]
    python ats_guard.py visa list
    python ats_guard.py visa peek --id <visa_id>
    python ats_guard.py visa revoke <visa_id>
    python ats_guard.py visa consume --id <visa_id>
                         [--scope-hash SHA256 --task-id TASK --plan-id PLAN]
    python ats_guard.py plugin seal   [plugin_id] --confirm [--passphrase-stdin]
    python ats_guard.py plugin verify [plugin_id]
    python ats_guard.py plugin list

========================= 签证（Visa）=========================
签证是「改了核心脚本、还没重封签，但要先跑一轮测试」的临时通行证，存于
<root>/visas/<visa_id>.json。按 scope_type 分三种，互不通用：

  digest   —— 老签证的默认值，也等于字段缺省。只跳过完整性校验，不跳过审批门。
  command  —— Task Visa。绑定 task_id + 命令原文的 SHA256，只放行那一条命令，
              且**不覆盖完整性校验**：改了核心脚本仍须重封签或另用 digest 签证。
  plan     —— Plan Visa。绑定 plan_id + Plan 的 DAG 指纹，只放行该 Plan 的 Step，
              配额可写 "unlimited"（Plan 跑完前不限次数）。

共同底线：
  * 签发必须持人类口令（visa issue --passphrase-stdin），否则 AI 能自己给自己
    发免死金牌；
  * 每次消费消耗 1 次配额，扣减后立刻落盘，扣到 0 时文件当场删除；
  * 带 scope 的签证必须带对应 scope 参数消费，参数对不上直接 visa_mismatch，
    **绝不跨任务**：哈希或 ID 不符就不放行，也不回落到审批；
  * 过期即失效，过期时间由 --time 分钟数决定，不接受「永不过期」。

========================= 插件封签（plugin）=========================
plugins/<plugin_id>/ 是插件**代码**，与 extend 插件（只读清单 + 子进程执行）相反：
它的 Injection.py 由 ats 主进程 exec，因此封签是它唯一的闸门。

  * 基线单独一份：<root>/ats_guard_plugin_manifest.json，不进核心清单，
    插件换版只重封插件，核心脚本不用动；
  * 未封签 / 哈希不符 / 目录缺失 → 一律拒绝导入，不回落到审批；
  * 插件目录内的字节码（__pycache__、*.pyc）不参与封签，但也不得存在 ——
    加载器全局设 sys.dont_write_bytecode，写出来的缓存只会让封签失配；
  * 封签同样必须持人类口令，与 seal 同级。

退出码：0 通过 / 2 用法错误 / 3 清单缺失 / 4 校验失败
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
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

VISA_DIR_NAME = "visas"
VISA_DEFAULT_NUMBER = 1              # 不传 --number 时默认只放行一次
VISA_DEFAULT_MINUTES = 30            # 不传 --time 时默认 30 分钟，绝不「永不过期」
VISA_ID_PREFIX = "v-"
VISA_ID_HEX_LEN = 8
VISA_LOCK_NAME = ".visa.lock"
VISA_LOCK_TIMEOUT = 2.0
VISA_SCOPE_DIGEST = "digest"         # 字段缺省即老签证：只管完整性校验
VISA_SCOPE_COMMAND = "command"       # Task Visa：绑定 task_id + 命令哈希
VISA_SCOPE_PLAN = "plan"             # Plan Visa：绑定 plan_id + DAG 指纹
VISA_SCOPE_TYPES = (VISA_SCOPE_DIGEST, VISA_SCOPE_COMMAND, VISA_SCOPE_PLAN)
VISA_UNLIMITED = "unlimited"         # 只允许 plan 场景：Plan 跑完前不限次数
VISA_SUBS = ("issue", "list", "peek", "revoke", "consume")

# —— 封包态热修改（3.2）：白名单文件可在封签状态下改，只更新它自己的哈希
# 默认拒绝：只有明确列进 HOT_PATCH_SAFE 的文件才可热改，其余一律走 unpack/repack。
HOT_PATCH_SAFE = ("ats_download.py", "ats_torrent.py")
HOT_PATCH_REDLINE = (GUARD_FILE, KEY_NAME, "policy.json")   # 改动即全面停机
SNAPSHOT_DIR_NAME = "snapshots"      # <root>/snapshots/ 存封签态原文，供 patch 生成 Diff
HOT_PATCH_DIFF_LIMIT = 200           # Diff 最多回传的行数，防止刷屏吃掉上下文
HOT_PATCH_LOG_LIMIT = 50             # 清单里保留的热修改记录条数上限


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

    默认取脚本所在目录的父目录（即仓库根目录），而不是当前工作目录：
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
                "hint": "密钥文件不存在，口令校验无从进行：由人执行 %s 创建"
                        % gcmd("init", "--confirm", "--passphrase-stdin")}
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


def gcmd(*parts):
    """看门狗命令的可复制形式：`python scripts/ats_guard.py <子命令>`。

    所有 hint 里给命令的地方都走这里，避免写出 `seal --confirm` 这种
    「在仓库根目录敲会找不到文件」的半截命令。
    """
    tail = " ".join(str(p) for p in parts if p is not None and str(p) != "")
    return "python scripts/%s %s" % (GUARD_FILE, tail)


def cmd_self():
    path = SCRIPT_DIR / GUARD_FILE
    return emit({"ok": True, "file": GUARD_FILE, "path": str(path),
                 "sha256": sha256_file(path)}, EXIT_OK)


def cmd_init(root, confirm, passphrase):
    """创建密钥文件。清单或密钥任一已存在时拒绝 —— 没有「重置」入口。"""
    if not confirm:
        return emit({"ok": False, "error": "confirm_required",
                     "hint": "创建密钥会建立信任根，必须由人显式确认：%s"
                             % gcmd("init", "--confirm", "--passphrase-stdin")},
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
                             "确认要全新开始，须由人先手动删除这两个文件，再执行 %s"
                             % gcmd("init", "--confirm", "--passphrase-stdin")},
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
                     "hint": "封签会重建信任基线，必须由人显式确认：%s"
                             % gcmd("seal", "--confirm", "--passphrase-stdin")},
                    EXIT_USAGE)
    auth = check_passphrase(root, passphrase)
    if not auth.get("ok"):
        return emit({"ok": False, "error": "passphrase_required",
                     "detail": auth,
                     "hint": "重建信任基线需要人工口令。密钥文件：%s" % key_path(root)},
                    EXIT_MISMATCH)
    if sha256_file(SCRIPT_DIR / "ats.py") is None:
        return emit({"ok": False, "error": "ats_missing",
                     "path": str(SCRIPT_DIR / "ats.py"),
                     "hint": "ats.py 不在本目录，拒绝生成不完整清单；"
                             "从仓库恢复 scripts/ats.py 后重跑 %s"
                             % gcmd("seal", "--confirm", "--passphrase-stdin")},
                    EXIT_USAGE)

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
    refresh_snapshots(root)
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
                         "hint": "清单缺失但密钥仍在：由人执行 %s 重新封签，"
                                 "口令须与 init 时一致"
                                 % gcmd("seal", "--confirm", "--passphrase-stdin")},
                        EXIT_NO_MANIFEST)
        return emit({"ok": False, "error": "manifest_missing",
                     "manifest": str(path),
                     "next_action": "init",
                     "hint": "本 root 尚未建立信任根：由人先执行 %s 创建密钥，"
                             "再执行 %s 建立基线"
                             % (gcmd("init", "--confirm", "--passphrase-stdin"),
                                gcmd("seal", "--confirm", "--passphrase-stdin"))},
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


# ---------------------------------------------------------------- 封包态热修改（3.2）

def snapshot_dir(root):
    return Path(root) / SNAPSHOT_DIR_NAME


def snapshot_path(root, name):
    return snapshot_dir(root) / name


def refresh_snapshots(root):
    """把白名单文件的当前内容留作「封签基线」，供 patch 生成 Diff。

    只处理 HOT_PATCH_SAFE：其余文件不落副本，避免每次 seal 复制整目录。
    内容与已有快照一致时跳过写入 —— 大多数 seal 都是重封签、文件没动。
    """
    for name in HOT_PATCH_SAFE:
        src = SCRIPT_DIR / name
        if not src.is_file():
            continue
        dst = snapshot_path(root, name)
        if dst.is_file() and sha256_file(dst) == sha256_file(src):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.write_bytes(src.read_bytes())
        except OSError:
            # 快照只是「更好的 Diff」的辅助，写不进去不该让 seal 整体失败。
            continue


def _read_text_lines(path):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text.splitlines()


def make_diff(root, name):
    """生成封签基线 -> 当前内容的统一 Diff；无快照时返回 None。"""
    old_lines = _read_text_lines(snapshot_path(root, name))
    new_lines = _read_text_lines(SCRIPT_DIR / name)
    if old_lines is None or new_lines is None:
        return None
    lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="%s（封签基线）" % name, tofile="%s（当前）" % name, lineterm=""))
    truncated = len(lines) > HOT_PATCH_DIFF_LIMIT
    lines = lines[:HOT_PATCH_DIFF_LIMIT]
    if truncated:
        lines.append("...（Diff 过长，已截断，完整内容请用 diff 工具比对）")
    return "\n".join(lines)


def cmd_patch(root, name, confirm, passphrase):
    """封签态热修改：只允许白名单文件，Diff 供人确认后只更新该文件的哈希。

    红线（ats_guard.py / 密钥 / policy.json）不在此列：它们改动必须触发全面停机。
    """
    if not name:
        return emit({"ok": False, "error": "file_required",
                     "hint": "指定要热修改的文件，例如 %s"
                             % gcmd("patch", "--file", "ats_download.py"),
                     "whitelist": list(HOT_PATCH_SAFE)}, EXIT_USAGE)
    if name in HOT_PATCH_REDLINE:
        return emit({"ok": False, "error": "red_line_file", "file": name,
                     "hint": "红线文件不可热修改：改动它会触发全面停机，"
                             "必须由人走停机流程：%s -> 改代码 -> %s"
                             % (gcmd("unpack", "--confirm"),
                                gcmd("repack", "--confirm")),
                     "redline": list(HOT_PATCH_REDLINE)}, EXIT_MISMATCH)
    if name not in HOT_PATCH_SAFE:
        return emit({"ok": False, "error": "not_whitelisted", "file": name,
                     "hint": "该文件不在热修改白名单，改动会让看门狗判失配。"
                             "白名单文件可由人改代码后 %s；其余请走 %s / %s"
                             % (gcmd("patch", "--file", name),
                                gcmd("unpack", "--confirm"),
                                gcmd("repack", "--confirm")),
                     "whitelist": list(HOT_PATCH_SAFE)}, EXIT_MISMATCH)

    path = manifest_path(root)
    manifest = read_manifest(path)
    if manifest is None:
        return emit({"ok": False, "error": "manifest_missing",
                     "manifest": str(path),
                     "hint": "本 root 还没有封签基线：由人先执行 %s"
                             % gcmd("seal", "--confirm", "--passphrase-stdin")},
                    EXIT_NO_MANIFEST)

    current = sha256_file(SCRIPT_DIR / name)
    if current is None:
        return emit({"ok": False, "error": "file_missing", "file": name,
                     "hint": "白名单文件不存在，无法热修改；确认文件是否被误删"},
                    EXIT_MISMATCH)
    expected = (manifest.get("files") or {}).get(name)
    if current == expected:
        return emit({"ok": True, "changed": False, "file": name,
                     "sha256": current,
                     "note": "与封签基线一致，无需热修改"}, EXIT_OK)

    diff = make_diff(root, name)
    if not confirm:
        payload = {"ok": False, "error": "confirm_required", "file": name,
                   "old_sha256": expected, "new_sha256": current,
                   "diff": diff,
                   "hint": "请人先看上面的 Diff；确认无误后执行 %s（需口令）"
                           % gcmd("patch", "--file", name, "--confirm",
                                  "--passphrase-stdin")}
        if diff is None:
            payload["diff_note"] = ("没有封签基线快照，无法生成 Diff（基线可能来自旧版本）。"
                                    "请人用 git diff 或编辑器自行核对后再确认")
        return emit(payload, EXIT_USAGE)

    auth = check_passphrase(root, passphrase)
    if not auth.get("ok"):
        return emit({"ok": False, "error": "passphrase_required", "detail": auth,
                     "hint": "热修改会改动信任基线，必须由人确认口令：%s"
                             % gcmd("patch", "--file", name, "--confirm",
                                    "--passphrase-stdin")},
                    EXIT_MISMATCH)

    manifest.setdefault("files", {})[name] = current
    records = manifest.get("hot_patches")
    if not isinstance(records, list):
        records = []
    records.append({"file": name, "old": expected, "new": current,
                    "at": int(time.time()),
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "by": "human-with-passphrase"})
    manifest["hot_patches"] = records[-HOT_PATCH_LOG_LIMIT:]
    write_manifest(path, manifest)

    dst = snapshot_path(root, name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.write_bytes((SCRIPT_DIR / name).read_bytes())
    except OSError:
        pass

    return emit({"ok": True, "changed": True, "file": name,
                 "old_sha256": expected, "new_sha256": current,
                 "manifest": str(path), "diff": diff,
                 "note": "已只更新该文件的哈希，其余基线未动"}, EXIT_OK)


# ---------------------------------------------------------------- 签证（Visa）

def visa_dir(root):
    return Path(root) / VISA_DIR_NAME


def valid_visa_id(visa_id):
    """签证 ID 只允许 v- 加 8 位小写 hex，防止用 ../ 之类拼出任意路径。"""
    if not isinstance(visa_id, str) or not visa_id.startswith(VISA_ID_PREFIX):
        return False
    body = visa_id[len(VISA_ID_PREFIX):]
    return len(body) == VISA_ID_HEX_LEN and all(c in "0123456789abcdef" for c in body)


def visa_path(root, visa_id):
    return visa_dir(root) / (visa_id + ".json")


def read_visa(root, visa_id):
    path = visa_path(root, visa_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                    # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def visa_scope_type(record):
    """签证的作用域类型；字段缺省按 digest 处理，保证老签证行为不变。"""
    kind = record.get("scope_type") if isinstance(record, dict) else None
    if isinstance(kind, str) and kind in VISA_SCOPE_TYPES:
        return kind
    return VISA_SCOPE_DIGEST


def visa_number(record):
    """解析配额：返回 int 或 VISA_UNLIMITED；非法值抛 ValueError。"""
    raw = record.get("number")
    if isinstance(raw, str) and raw.strip().lower() == VISA_UNLIMITED:
        return VISA_UNLIMITED
    return int(raw)


def visa_state(record, now=None):
    """返回 (状态, 剩余次数)。状态：valid / expired / exhausted / corrupt。

    剩余次数可能是 int，也可能是 "unlimited"（只有 plan 场景允许）。
    """
    if not isinstance(record, dict):
        return "corrupt", 0
    try:
        number = visa_number(record)
        expires_at = int(record.get("expires_at"))
    except (TypeError, ValueError):
        return "corrupt", 0
    now = int(time.time()) if now is None else int(now)
    if expires_at <= now:
        return "expired", number
    if number != VISA_UNLIMITED and number <= 0:
        return "exhausted", number
    return "valid", number


def acquire_visa_lock(root):
    """扣减配额用独立锁文件做互斥：tmp+replace 只保证「写」原子，不保证「读改写」原子。"""
    lock = visa_dir(root) / VISA_LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + VISA_LOCK_TIMEOUT
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock
        except FileExistsError:
            if time.time() >= deadline:
                return None
            time.sleep(0.05)
        except OSError:
            return None


def release_visa_lock(lock):
    if lock is None:
        return
    try:
        Path(lock).unlink()
    except OSError:
        pass


def _bad_scope(payload):
    return None, emit(payload, EXIT_USAGE)


def normalize_scope(scope_type, scope_hash, task_id, plan_id):
    """校验作用域参数，返回 (scope_dict, error_payload)。

    三种作用域互斥，且都必须把「作用对象」绑死：
      digest  —— 不许带任何 scope 参数（带了就是调用方搞错了意图）；
      command —— 必须同时给 scope_hash 与 task_id，缺一个就签不成；
      plan    —— 必须同时给 scope_hash 与 plan_id。
    这样签出来的签证天然不可跨任务使用：哈希与 ID 都写进文件，消费时逐字比对。
    """
    kind = (scope_type or VISA_SCOPE_DIGEST)
    if not isinstance(kind, str) or kind not in VISA_SCOPE_TYPES:
        return _bad_scope({"ok": False, "error": "bad_scope_type",
                           "hint": "--scope-type 只能是 digest / command / plan，"
                                   "照这个形式复制：%s"
                                   % gcmd("visa", "issue", "--time", "30",
                                          "--scope-type", "digest",
                                          "--passphrase-stdin")})
    digest = (scope_hash or "").strip().lower()
    task = (task_id or "").strip()
    plan = (plan_id or "").strip()
    if kind == VISA_SCOPE_DIGEST:
        if digest or task or plan:
            return _bad_scope({"ok": False, "error": "scope_not_applicable",
                               "hint": "digest 签证不绑任何对象；要绑任务或计划请显式给 "
                                       "--scope-type command / plan，并同时带 --scope-hash "
                                       "与 --task-id（或 --plan-id）。现有签证用 %s 查看"
                                       % gcmd("visa", "list")})
        return {"scope_type": kind}, None
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return _bad_scope({"ok": False, "error": "bad_scope_hash",
                           "hint": "%s 签证的 --scope-hash 必须是 64 位小写十六进制 "
                                   "SHA-256；用这条命令现算（把命令原文从标准输入喂进去）："
                                   "python -c \"import hashlib,sys;"
                                   "print(hashlib.sha256(sys.stdin.buffer.read())"
                                   ".hexdigest())\"" % kind})
    if kind == VISA_SCOPE_COMMAND:
        if not task or plan:
            return _bad_scope({"ok": False, "error": "bad_scope_target",
                               "hint": "command 签证必须同时给 --scope-hash 与 --task-id，"
                                       "且不能带 --plan-id。现有签证用 %s 查看"
                                       % gcmd("visa", "list")})
        return {"scope_type": kind, "scope_hash": digest, "task_id": task}, None
    if not plan or task:
        return _bad_scope({"ok": False, "error": "bad_scope_target",
                           "hint": "plan 签证必须同时给 --scope-hash 与 --plan-id，"
                                   "且不能带 --task-id。现有签证用 %s 查看"
                                   % gcmd("visa", "list")})
    return {"scope_type": kind, "scope_hash": digest, "plan_id": plan}, None


def cmd_visa_issue(root, number, minutes, note, passphrase,
                   scope_type=None, scope_hash=None, task_id=None, plan_id=None):
    """签发签证：必须持人类口令，否则 AI 就能自己给自己发免死金牌。"""
    auth = check_passphrase(root, passphrase)
    if not auth.get("ok"):
        return emit({"ok": False, "error": "passphrase_required", "detail": auth,
                     "hint": "签发签证要人类口令（密钥文件：%s）。用 --passphrase-stdin "
                             "从标准输入提供一行，或直接在终端交互输入" % key_path(root)},
                    EXIT_MISMATCH)
    scope, err = normalize_scope(scope_type, scope_hash, task_id, plan_id)
    if err is not None:
        return err
    if minutes is None:
        minutes = VISA_DEFAULT_MINUTES
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return emit({"ok": False, "error": "bad_number",
                     "hint": "--time 必须是正整数，单位是分钟"}, EXIT_USAGE)
    if minutes < 1:
        return emit({"ok": False, "error": "bad_number",
                     "hint": "--time 必须 >= 1，单位是分钟"}, EXIT_USAGE)
    if number is None:
        number = VISA_DEFAULT_NUMBER
    if isinstance(number, str) and number.strip().lower() == VISA_UNLIMITED:
        # unlimited 只对 plan 开放：Plan 有明确终点（DAG 跑完即散），
        # 而 command 场景下的无限次等于给单条命令开了永久免死金牌。
        if scope["scope_type"] != VISA_SCOPE_PLAN:
            return emit({"ok": False, "error": "bad_number",
                         "hint": "--number unlimited 只允许 scope-type=plan；"
                                 "command / digest 必须给正整数配额"}, EXIT_USAGE)
        number = VISA_UNLIMITED
    else:
        try:
            number = int(number)
        except (TypeError, ValueError):
            return emit({"ok": False, "error": "bad_number",
                         "hint": "--number 必须是正整数，plan 场景可写 unlimited"},
                        EXIT_USAGE)
        if number < 1:
            return emit({"ok": False, "error": "bad_number",
                         "hint": "--number 必须 >= 1，plan 场景可写 unlimited"},
                        EXIT_USAGE)
    created_at = int(time.time())
    record = {"visa_id": "", "number": number, "created_at": created_at,
              "expires_at": created_at + minutes * 60, "note": note or ""}
    record.update(scope)
    for _ in range(8):
        candidate = VISA_ID_PREFIX + os.urandom(4).hex()
        if not visa_path(root, candidate).exists():
            record["visa_id"] = candidate
            break
    if not record["visa_id"]:
        return emit({"ok": False, "error": "visa_id_collision",
                     "hint": "生成签证 ID 连续冲突，稍后重试"}, EXIT_MISMATCH)
    write_manifest(visa_path(root, record["visa_id"]), record)
    return emit({"ok": True, "visa_id": record["visa_id"], "number": number,
                 "created_at": created_at, "expires_at": record["expires_at"],
                 "expires_iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                              time.localtime(record["expires_at"]))
                 if record["expires_at"] > 0 else "",
                 "note": record["note"],
                 "scope_type": record["scope_type"],
                 "scope_hash": record.get("scope_hash") or "",
                 "task_id": record.get("task_id") or "",
                 "plan_id": record.get("plan_id") or "",
                 "path": str(visa_path(root, record["visa_id"]))}, EXIT_OK)


def cmd_visa_list(root):
    """列出有效签证与剩余配额；过期/损坏的单独放在 invalid 里，便于排查。"""
    base = visa_dir(root)
    visas = []
    invalid = []
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            record = read_visa(root, path.stem)
            if record is None:
                invalid.append({"visa_id": path.stem, "status": "corrupt"})
                continue
            state, number = visa_state(record)
            entry = {"visa_id": record.get("visa_id") or path.stem,
                     "number": number,
                     "created_at": record.get("created_at"),
                     "expires_at": record.get("expires_at"),
                     "note": record.get("note") or "",
                     "scope_type": visa_scope_type(record),
                     "scope_hash": record.get("scope_hash") or "",
                     "task_id": record.get("task_id") or "",
                     "plan_id": record.get("plan_id") or "",
                     "status": state}
            try:
                entry["expires_iso"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(int(entry["expires_at"])))
            except (TypeError, ValueError, OSError):
                pass
            (visas if state == "valid" else invalid).append(entry)
    return emit({"ok": True, "count": len(visas), "visas": visas,
                 "invalid": invalid, "dir": str(base)}, EXIT_OK)


def cmd_visa_revoke(root, visa_id):
    if not valid_visa_id(visa_id):
        return emit({"ok": False, "error": "bad_visa_id",
                     "hint": "签证 ID 形如 v-0123abcd，即 v- 加 8 位小写十六进制；"
                             "先用 %s 看现有签证" % gcmd("visa", "list")},
                    EXIT_USAGE)
    path = visa_path(root, visa_id)
    if not path.is_file():
        return emit({"ok": False, "error": "visa_not_found", "visa_id": visa_id,
                     "hint": "该签证不存在，先用 %s 查看现有签证"
                             % gcmd("visa", "list")}, EXIT_MISMATCH)
    try:
        path.unlink()
    except OSError as exc:
        return emit({"ok": False, "error": "visa_revoke_failed", "visa_id": visa_id,
                     "detail": "%s: %s" % (type(exc).__name__, exc),
                     "hint": "删除签证文件失败，检查 %s 的写权限后重试"
                             % visa_path(root, visa_id)}, EXIT_MISMATCH)
    return emit({"ok": True, "visa_id": visa_id, "revoked": True,
                 "path": str(path)}, EXIT_OK)


def cmd_visa_peek(root, visa_id):
    """只读查看：不扣配额，用于审批门前预检（决定要不要走到扣减那一步）。"""
    if not valid_visa_id(visa_id):
        return emit({"ok": False, "error": "visa_invalid", "reason": "bad_id",
                     "visa_id": visa_id,
                     "hint": "签证 ID 形如 v-0123abcd；先用 %s 看现有签证，"
                             "不带签证时只能走 %s"
                             % (gcmd("visa", "list"),
                                gcmd("seal", "--confirm", "--passphrase-stdin"))},
                    EXIT_MISMATCH)
    record = read_visa(root, visa_id)
    if record is None:
        return emit({"ok": False, "error": "visa_invalid", "reason": "not_found",
                     "visa_id": visa_id,
                     "hint": "签证不存在或已被销毁，由人重新签发：%s"
                             % gcmd("visa", "issue", "--time", "30",
                                    "--passphrase-stdin")},
                    EXIT_MISMATCH)
    state, number = visa_state(record)
    return emit({"ok": state == "valid", "visa_id": visa_id, "status": state,
                 "number": number, "expires_at": record.get("expires_at"),
                 "scope_type": visa_scope_type(record),
                 "scope_hash": record.get("scope_hash") or "",
                 "task_id": record.get("task_id") or "",
                 "plan_id": record.get("plan_id") or ""},
                EXIT_OK if state == "valid" else EXIT_MISMATCH)


def scope_mismatch(record, scope):
    """比对签证作用域与本次执行对象；返回空字符串表示匹配，否则返回原因码。

    规则（与签发时的绑定逐字对应）：
      digest  —— 调用方不许带任何 scope 参数（带了就是拿老签证办新事）；
      command —— 命令哈希必须相等；给了 task_id 时还必须是同一任务；
      plan    —— plan_id 相等即放行（Plan 级联匹配）；plan_id 没给时按 DAG 指纹比。
    任何一条对不上都返回原因码，由调用方直接拒绝，**不回落到审批**。
    """
    kind = visa_scope_type(record)
    digest = (scope.get("scope_hash") or "").strip().lower()
    task = (scope.get("task_id") or "").strip()
    plan = (scope.get("plan_id") or "").strip()
    if kind == VISA_SCOPE_DIGEST:
        return "digest_scope" if (digest or task or plan) else ""
    if not (digest or task or plan):
        return "scope_required"
    if kind == VISA_SCOPE_COMMAND:
        if task and task != (record.get("task_id") or ""):
            return "task_mismatch"
        if not digest:
            return "hash_required"
        return "" if digest == (record.get("scope_hash") or "") else "hash_mismatch"
    if plan:
        return "" if plan == (record.get("plan_id") or "") else "plan_mismatch"
    return "" if digest == (record.get("scope_hash") or "") else "hash_mismatch"


def cmd_visa_consume(root, visa_id, scope=None):
    """握手期消耗一次配额：扣减立刻落盘，扣到 0 时文件当场删除（用完即焚）。

    scope 为空表示「老式 digest 签证」调用（ats.py 完整性校验用）；
    带 scope 时先比对作用域，不匹配则一次配额都不扣。
    """
    scope = scope or {}
    if not valid_visa_id(visa_id):
        return emit({"ok": False, "error": "visa_invalid", "reason": "bad_id",
                     "visa_id": visa_id,
                     "hint": "签证 ID 形如 v-0123abcd；先用 %s 看现有签证，"
                             "不带签证时只能走 %s"
                             % (gcmd("visa", "list"),
                                gcmd("seal", "--confirm", "--passphrase-stdin"))},
                    EXIT_MISMATCH)
    lock = acquire_visa_lock(root)
    if lock is None:
        return emit({"ok": False, "error": "visa_invalid", "reason": "locked",
                     "visa_id": visa_id,
                     "hint": "签证扣减被其他进程占用，稍后重试；"
                             "确认占用者是否已退出可用 %s 看配额是否已变"
                             % gcmd("visa", "list")}, EXIT_MISMATCH)
    try:
        record = read_visa(root, visa_id)
        if record is None:
            return emit({"ok": False, "error": "visa_invalid", "reason": "not_found",
                         "visa_id": visa_id,
                         "hint": "签证不存在或已被销毁，由人重新签发：%s"
                                 % gcmd("visa", "issue", "--time", "30",
                                        "--passphrase-stdin")},
                        EXIT_MISMATCH)
        state, number = visa_state(record)
        if state != "valid":
            return emit({"ok": False, "error": "visa_invalid", "reason": state,
                         "visa_id": visa_id,
                         "hint": "签证已过期或配额耗尽，由人重新签发：%s"
                                 % gcmd("visa", "issue", "--time", "30",
                                        "--passphrase-stdin")},
                        EXIT_MISMATCH)
        reason = scope_mismatch(record, scope)
        if reason:
            return emit({"ok": False, "error": "visa_mismatch", "reason": reason,
                         "visa_id": visa_id,
                         "scope_type": visa_scope_type(record),
                         "expected_hash": record.get("scope_hash") or "",
                         "expected_task": record.get("task_id") or "",
                         "expected_plan": record.get("plan_id") or "",
                         "given_hash": (scope.get("scope_hash") or "").strip().lower(),
                         "given_task": (scope.get("task_id") or "").strip(),
                         "given_plan": (scope.get("plan_id") or "").strip(),
                         "hint": "签证只对它签发时绑定的对象生效：哈希或 ID 不符就是"
                                 "跨任务使用，请由人按 expected_* 重新签发对应签证：%s"
                                 % gcmd("visa", "issue", "--time", "30",
                                        "--scope-type", visa_scope_type(record),
                                        "--passphrase-stdin")},
                        EXIT_MISMATCH)
        if number == VISA_UNLIMITED:
            return emit({"ok": True, "visa_id": visa_id,
                         "remaining": VISA_UNLIMITED, "destroyed": False,
                         "scope_type": visa_scope_type(record),
                         "expires_at": record.get("expires_at")}, EXIT_OK)
        remaining = number - 1
        if remaining <= 0:
            try:
                visa_path(root, visa_id).unlink()
            except OSError as exc:
                return emit({"ok": False, "error": "visa_invalid",
                             "reason": "unlink_failed", "visa_id": visa_id,
                             "detail": "%s: %s" % (type(exc).__name__, exc),
                             "hint": "签证文件无法删除，为免配额被重复使用，本次不放行；"
                                     "由人检查 %s 的写权限后 %s"
                                     % (visa_path(root, visa_id),
                                        gcmd("visa", "revoke", visa_id))},
                            EXIT_MISMATCH)
            # 只写 stderr：stdout 必须是纯 JSON，否则 ats.py 的 _run_guard 解析不到结果。
            sys.stderr.write("[Visa] 配额已耗尽，签证已销毁，"
                             "后续执行请重新封签或申请新签证。\n")
            return emit({"ok": True, "visa_id": visa_id, "remaining": 0,
                         "destroyed": True,
                         "scope_type": visa_scope_type(record)}, EXIT_OK)
        record["number"] = remaining
        write_manifest(visa_path(root, visa_id), record)
        return emit({"ok": True, "visa_id": visa_id, "remaining": remaining,
                     "destroyed": False, "scope_type": visa_scope_type(record),
                     "expires_at": record.get("expires_at")}, EXIT_OK)
    finally:
        release_visa_lock(lock)


def cmd_visa(root, sub, opts, passphrase):
    if sub == "issue":
        return cmd_visa_issue(root, opts.get("number"), opts.get("minutes"),
                              opts.get("note"), passphrase,
                              scope_type=opts.get("scope_type"),
                              scope_hash=opts.get("scope_hash"),
                              task_id=opts.get("task_id"),
                              plan_id=opts.get("plan_id"))
    if sub == "list":
        return cmd_visa_list(root)
    if sub == "peek":
        return cmd_visa_peek(root, opts.get("visa_id") or "")
    if sub == "revoke":
        return cmd_visa_revoke(root, opts.get("visa_id") or "")
    return cmd_visa_consume(root, opts.get("visa_id") or "",
                            {"scope_hash": opts.get("scope_hash"),
                             "task_id": opts.get("task_id"),
                             "plan_id": opts.get("plan_id")})


# ========================= 插件封签（Python 插件层）=========================
# plugins/<plugin_id>/ 是**代码**，不是运行态，因此不进 core_files() 的基线：
# 插件换版只重封插件清单，核心脚本不用动，避免「改个插件就要重封全仓」。
#
# 代价说清楚：插件封签清单是第二份基线，**只有**插件目录里的文件受它约束。
# 插件里的 Injection.py 最终仍在主进程里跑（这点与 extend 插件的子进程模型相反），
# 所以封签就是它唯一的闸门：清单缺失 / 哈希不符一律不导入，不回落到审批。
PLUGIN_DIR_NAME = "plugins"
PLUGIN_MANIFEST_NAME = "ats_guard_plugin_manifest.json"
PLUGIN_MANIFEST_VERSION = 1
PLUGIN_JSON_NAME = "plugin.json"
PLUGIN_DEFAULT_INJECTION = "Injection.py"
PLUGIN_SKIP_DIRS = ("__pycache__",)
PLUGIN_SKIP_SUFFIX = (".pyc", ".pyo")
PLUGIN_SUBS = ("seal", "verify", "list")
PLUGIN_MAX_FILES = 5000
PLUGIN_MAX_TOTAL_BYTES = 128 * 1024 * 1024
PLUGIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def plugins_root():
    """插件代码根目录。ATS_PLUGINS_ROOT 仅供 CI / 自测隔离，与 ats_plugin.py 同口径。"""
    env = os.environ.get("ATS_PLUGINS_ROOT")
    if env:
        return Path(env)
    return SCRIPT_DIR.parent / PLUGIN_DIR_NAME


def plugin_manifest_path(root):
    return Path(root) / PLUGIN_MANIFEST_NAME


def plugin_dirs():
    """待封签的插件目录：跳过符号链接与隐藏目录，符号链接会把基线引到仓外。"""
    base = plugins_root()
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        out.append(entry)
    return out


def plugin_files(plugin_dir):
    """插件目录内全部文件的 {相对路径: sha256}；跳过字节码，否则封签永远追不上缓存。"""
    base = Path(plugin_dir)
    files = {}
    total = 0
    for root, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs
                         if d not in PLUGIN_SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.endswith(PLUGIN_SKIP_SUFFIX):
                continue
            path = Path(root) / name
            rel = path.relative_to(base).as_posix()
            digest = sha256_file(path)
            if digest is None:
                files[rel] = None
                continue
            total += path.stat().st_size
            files[rel] = digest
    return files, total


def plugin_declared(plugin_dir):
    """plugin.json 的极简校验：能解析、是对象、入口指向目录内的真实文件。

    这里只做「能不能封签」的判断，字段级校验在 ats_plugin.py（导入前那一层）；
    两处口径不必重复，但入口防穿越两边都必须挡 —— 封签封的正是那个入口文件。
    """
    try:
        data = json.loads((Path(plugin_dir) / PLUGIN_JSON_NAME)
                          .read_text(encoding="utf-8-sig"))
    except Exception as exc:             # noqa: BLE001
        return None, "plugin_manifest_invalid", "%s: %s" % (type(exc).__name__, exc)
    if not isinstance(data, dict):
        return None, "plugin_manifest_invalid", "plugin.json 顶层必须是对象"
    injection = str(data.get("injection") or data.get("entry_point")
                    or PLUGIN_DEFAULT_INJECTION).strip()
    base = Path(plugin_dir).resolve()
    try:
        resolved = (base / injection).resolve()
    except OSError as exc:
        return None, "plugin_injection_invalid", "入口路径无法解析: %s" % exc
    if resolved != base and base not in resolved.parents:
        return None, "plugin_injection_invalid", "入口必须落在插件目录内: %r" % injection
    if not resolved.is_file():
        return None, "plugin_injection_missing", "入口文件不存在: %s" % injection
    return data, None, None


def cmd_plugin_seal(root, confirm, passphrase, plugin_id=None):
    """重建插件封签清单。单插件模式只重封一个，其余条目不删不改。"""
    if not confirm:
        return emit({"ok": False, "error": "confirm_required",
                     "hint": "插件封签会重建插件信任基线，必须由人显式确认：%s"
                             % gcmd("plugin", "seal", "--confirm", "--passphrase-stdin")},
                    EXIT_USAGE)
    auth = check_passphrase(root, passphrase)
    if not auth.get("ok"):
        return emit({"ok": False, "error": "passphrase_required",
                     "detail": auth,
                     "hint": "重建插件基线需要人工口令。密钥文件：%s" % key_path(root)},
                    EXIT_MISMATCH)

    if plugin_id is not None:
        text = str(plugin_id).strip()
        if not PLUGIN_ID_PATTERN.match(text):
            return emit({"ok": False, "error": "bad_plugin_id",
                         "plugin_id": text,
                         "hint": "插件 id 只允许字母、数字、下划线、短横线"}, EXIT_USAGE)
        target = plugins_root() / text
        if target.is_symlink() or not target.is_dir():
            return emit({"ok": False, "error": "plugin_not_found",
                         "plugin_id": text,
                         "plugin_dir": str(plugins_root()),
                         "hint": "该插件目录不存在；用 %s 看现有插件"
                                 % gcmd("plugin", "list")}, EXIT_USAGE)
        targets = [target]
    else:
        targets = plugin_dirs()

    path = plugin_manifest_path(root)
    previous = read_manifest(path) or {}
    old_plugins = previous.get("plugins")
    old_plugins = old_plugins if isinstance(old_plugins, dict) else {}

    sealed = dict(old_plugins) if plugin_id is not None else {}
    problems = []
    changed = []
    dropped = []
    if plugin_id is None:
        dropped = sorted(set(old_plugins) - {p.name for p in targets})

    for plugin_dir in targets:
        pid = plugin_dir.name
        declared, code, detail = plugin_declared(plugin_dir)
        if code:
            problems.append({"plugin_id": pid, "error": code, "detail": detail})
            continue
        files, total = plugin_files(plugin_dir)
        if not files:
            problems.append({"plugin_id": pid, "error": "plugin_empty",
                             "detail": "目录里没有任何文件"})
            continue
        if len(files) > PLUGIN_MAX_FILES or total > PLUGIN_MAX_TOTAL_BYTES:
            problems.append({"plugin_id": pid, "error": "plugin_too_large",
                             "detail": "%d 个文件 / %d 字节，超过封签上限 %d / %d"
                                       % (len(files), total, PLUGIN_MAX_FILES,
                                          PLUGIN_MAX_TOTAL_BYTES)})
            continue
        if any(digest is None for digest in files.values()):
            problems.append({"plugin_id": pid, "error": "plugin_unreadable",
                             "detail": "有文件读不出来，拒绝生成半份清单"})
            continue
        record = {"state": "active", "files": files,
                  "entry": str(declared.get("injection")
                               or declared.get("entry_point")
                               or PLUGIN_DEFAULT_INJECTION),
                  "version": str(declared.get("version") or ""),
                  "sealed_at": int(time.time()),
                  "sealed_iso": time.strftime("%Y-%m-%dT%H:%M:%S")}
        old_files = (old_plugins.get(pid) or {}).get("files")
        if isinstance(old_files, dict):
            for name, digest in files.items():
                if old_files.get(name) != digest:
                    changed.append("%s/%s" % (pid, name))
            for name in old_files:
                if name not in files:
                    changed.append("%s/%s" % (pid, name))
        else:
            changed.append("%s/*" % pid)
        sealed[pid] = record

    if problems and plugin_id is not None:
        # 单插件模式：拒绝写盘，旧条目原样保留，避免「封失败还把旧基线抹了」。
        return emit({"ok": False, "error": "plugin_seal_failed",
                     "manifest": str(path), "problems": problems,
                     "hint": "先修 problems 再重封；旧清单未被改动"}, EXIT_MISMATCH)

    record = {
        "version": PLUGIN_MANIFEST_VERSION,
        "sealed_at": int(time.time()),
        "sealed_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sealed_by": "human-with-passphrase",
        "plugins_root": str(plugins_root()),
        "plugins": sealed,
    }
    write_manifest(path, record)
    return emit({"ok": True, "manifest": str(path),
                 "plugins_root": str(plugins_root()),
                 "plugins": {pid: {"files": len(entry.get("files") or {}),
                                   "version": entry.get("version"),
                                   "entry": entry.get("entry")}
                             for pid, entry in sorted(sealed.items())},
                 "changed": sorted(set(changed)),
                 "dropped": dropped,
                 "problems": problems}, EXIT_OK)


def cmd_plugin_verify(root, plugin_id=None):
    """比对插件字节与插件清单。只报事实，不修不封。"""
    path = plugin_manifest_path(root)
    manifest = read_manifest(path)
    if manifest is None:
        return emit({"ok": False, "error": "plugin_manifest_missing",
                     "manifest": str(path),
                     "next_action": "plugin_seal",
                     "hint": "插件封签清单缺失：由人执行 %s 建立插件基线"
                             % gcmd("plugin", "seal", "--confirm", "--passphrase-stdin")},
                    EXIT_NO_MANIFEST)
    if manifest.get("version") != PLUGIN_MANIFEST_VERSION:
        return emit({"ok": False, "error": "plugin_manifest_version",
                     "manifest": str(path),
                     "expected": PLUGIN_MANIFEST_VERSION,
                     "actual": manifest.get("version")}, EXIT_MISMATCH)

    sealed = manifest.get("plugins")
    sealed = sealed if isinstance(sealed, dict) else {}
    base = plugins_root()
    wanted = [str(plugin_id)] if plugin_id is not None else None
    ids = sorted(set(list(sealed) + [p.name for p in plugin_dirs()]))
    if wanted is not None:
        ids = wanted

    result = {}
    all_problems = []
    for pid in ids:
        entry = sealed.get(pid)
        plugin_dir = base / pid
        problems = []
        if entry is None:
            state = "unsealed"
            problems.append({"error": "plugin_unsealed",
                             "detail": "该插件不在封签清单里"})
        elif plugin_dir.is_symlink() or not plugin_dir.is_dir():
            state = "missing"
            problems.append({"error": "plugin_dir_missing",
                             "detail": "清单里有、目录不见了"})
        else:
            current, _total = plugin_files(plugin_dir)
            expected = entry.get("files")
            expected = expected if isinstance(expected, dict) else {}
            for name, digest in current.items():
                if name not in expected:
                    problems.append({"file": name, "error": "unsealed_file"})
                elif digest != expected[name]:
                    problems.append({"file": name, "error": "digest_mismatch"})
            for name in expected:
                if name not in current:
                    problems.append({"file": name, "error": "missing_file"})
            state = "modified" if problems else "active"
        result[pid] = {"state": state, "problems": problems}
        all_problems.extend(dict(p, plugin_id=pid) for p in problems)

    ok = all(item["state"] == "active" for item in result.values())
    payload = {"ok": ok, "manifest": str(path),
               "plugins_root": str(base),
               "checked": len(result), "plugins": result,
               "problems": all_problems}
    if not ok:
        payload["next_action"] = "plugin_seal"
        payload["hint"] = ("插件字节与封签不符，拒绝导入；确认改动可信后由人重封：%s"
                           % gcmd("plugin", "seal", "--confirm", "--passphrase-stdin"))
    return emit(payload, EXIT_OK if ok else EXIT_MISMATCH)


def cmd_plugin_list(root):
    path = plugin_manifest_path(root)
    manifest = read_manifest(path)
    sealed = (manifest or {}).get("plugins")
    sealed = sealed if isinstance(sealed, dict) else {}
    entries = []
    for pid in sorted(set(list(sealed) + [p.name for p in plugin_dirs()])):
        entry = sealed.get(pid) or {}
        files = entry.get("files") if isinstance(entry.get("files"), dict) else {}
        entries.append({"plugin_id": pid,
                        "sealed": pid in sealed,
                        "files": len(files),
                        "version": entry.get("version"),
                        "entry": entry.get("entry"),
                        "sealed_iso": entry.get("sealed_iso")})
    return emit({"ok": True, "manifest": str(path),
                 "manifest_exists": manifest is not None,
                 "plugins_root": str(plugins_root()),
                 "count": len(entries), "plugins": entries}, EXIT_OK)


def cmd_plugin(root, sub, opts, passphrase):
    if sub == "seal":
        return cmd_plugin_seal(root, opts.get("confirm"), passphrase,
                               opts.get("plugin_id"))
    if sub == "verify":
        return cmd_plugin_verify(root, opts.get("plugin_id"))
    return cmd_plugin_list(root)


VALUE_FLAGS = {
    "--root": "root",
    "--number": "number",
    "--time": "minutes",
    "--note": "note",
    "--id": "visa_id",
    "--file": "patch_file",
    "--scope-type": "scope_type",
    "--scope-hash": "scope_hash",
    "--task-id": "task_id",
    "--plan-id": "plan_id",
}


def parse_args(argv):
    """解析参数，返回 (action, opts)；用法错误返回 (None, None)。

    --number / --time / --note / --id / --file / --scope-type / --scope-hash /
    --task-id / --plan-id 只在各自子命令里有意义，其余子命令带上
    也不会被消费（保持旧调用方按位置解析的兼容性）。
    """
    opts = {"root": None, "confirm": False, "passphrase_stdin": False,
            "number": None, "minutes": None, "note": None,
            "visa_id": None, "sub": None, "scope_type": None,
            "scope_hash": None, "task_id": None, "plan_id": None,
            "patch_file": None, "plugin_id": None}
    words = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--confirm":
            opts["confirm"] = True
            i += 1
            continue
        if token == "--passphrase-stdin":
            opts["passphrase_stdin"] = True
            i += 1
            continue
        if token == "--json":
            i += 1
            continue
        name, sep, inline = token.partition("=")
        if sep and name in VALUE_FLAGS:
            opts[VALUE_FLAGS[name]] = inline
            i += 1
            continue
        if token in VALUE_FLAGS:
            if i + 1 >= len(argv):
                return None, None
            opts[VALUE_FLAGS[token]] = argv[i + 1]
            i += 2
            continue
        if token.startswith("-"):
            return None, None
        words.append(token)
        i += 1
    if not words:
        return None, None
    action = words[0]
    if action == "visa":
        if len(words) < 2 or words[1] not in VISA_SUBS:
            return None, None
        if len(words) > 3:
            return None, None
        opts["sub"] = words[1]
        if len(words) == 3:
            opts["visa_id"] = words[2]
        return action, opts
    if action == "plugin":
        # 插件封签：seal [plugin_id] / verify [plugin_id] / list。
        # id 走位置参数而不是 --plugin-id，与 visa 的 `revoke <id>` 保持一致。
        if len(words) < 2 or words[1] not in PLUGIN_SUBS:
            return None, None
        if len(words) > 3:
            return None, None
        opts["sub"] = words[1]
        if len(words) == 3:
            if words[1] == "list":
                return None, None
            opts["plugin_id"] = words[2]
        return action, opts
    if len(words) > 1:
        return None, None
    return action, opts


USAGE = ("python ats_guard.py verify|seal|init|auth|self|patch "
         "[--root DIR] [--confirm] [--passphrase-stdin] [--json]；"
         "patch: --file <白名单文件> [--confirm --passphrase-stdin]；"
         "visa: issue --time MINUTES [--number N|unlimited] [--note TEXT] "
         "[--scope-type digest|command|plan --scope-hash SHA256 "
         "--task-id TASK|--plan-id PLAN] | list | peek --id <visa_id> | "
         "revoke <visa_id> | consume --id <visa_id> "
         "[--scope-hash SHA256] [--task-id TASK] [--plan-id PLAN]；"
         "plugin: seal [plugin_id] --confirm --passphrase-stdin | "
         "verify [plugin_id] | list")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action, opts = parse_args(argv)
    if action not in ("verify", "seal", "init", "auth", "self", "visa", "patch",
                      "plugin"):
        return emit({"ok": False, "error": "usage", "usage": USAGE}, EXIT_USAGE)
    root = resolve_root(opts.get("root"))
    if action == "visa":
        sub = opts.get("sub")
        if sub not in VISA_SUBS:
            return emit({"ok": False, "error": "usage", "usage": USAGE}, EXIT_USAGE)
        passphrase = ""
        if sub == "issue":
            passphrase = read_passphrase(opts.get("passphrase_stdin"))
        return cmd_visa(root, sub, opts, passphrase)
    if action == "plugin":
        sub = opts.get("sub")
        if sub not in PLUGIN_SUBS:
            return emit({"ok": False, "error": "usage", "usage": USAGE}, EXIT_USAGE)
        passphrase = ""
        if sub == "seal":
            passphrase = read_passphrase(opts.get("passphrase_stdin"))
        return cmd_plugin(root, sub, opts, passphrase)
    if action == "verify":
        return cmd_verify(root)
    if action == "auth":
        return cmd_auth(root, read_passphrase(opts.get("passphrase_stdin")))
    if action == "init":
        return cmd_init(root, opts.get("confirm"),
                        read_passphrase(opts.get("passphrase_stdin")))
    if action == "seal":
        return cmd_seal(root, opts.get("confirm"),
                        read_passphrase(opts.get("passphrase_stdin")))
    if action == "patch":
        return cmd_patch(root, opts.get("patch_file"), opts.get("confirm"),
                         read_passphrase(opts.get("passphrase_stdin")))
    return cmd_self()


if __name__ == "__main__":
    sys.exit(main())
