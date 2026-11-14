#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-task-scheduler — Task 层执行器 (v3.3.0-R0)

命令：
  create <id> --command "..." [--cwd DIR] [--level L0..L3] [--timeout SEC]
                [--env K=V ...] [--no-shell] [--spec FILE] [--force]
  run <id> [--approval AP] [--wait]
  download --url URL... | --file MANIFEST... [--out DIR] [--name NAME]
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
  * 所有状态落在 ATS_ROOT（默认 <ats 本体目录>/.ats，即仓库根的 .ats），可用环境变量覆盖。
  * Task 层事件统一写 <root>/events.jsonl；Plan 层事件写
    <root>/schedules/<schedule_id>/events.jsonl（由 ats_plan.py 触发）。
  * 审批记录 <root>/approvals/<approval_id>.json，命令原文绑定 + 有效期。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode = True

try:                                    # 预演模块缺失时退化为纯权限门行为
    import ats_preflight
except Exception:                       # noqa: BLE001
    ats_preflight = None

try:                                    # 扩展发现层缺失时 extend 子命令直接报错
    import ats_extend
except Exception:                       # noqa: BLE001
    ats_extend = None

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

# —— 看门狗（ats_guard.py）：启动准入、跳过审批、运行期句柄与策略快照
GUARD_FILE = "ats_guard.py"
GUARD_MANIFEST = "ats_guard_manifest.json"
GUARD_MANIFEST_ENV = "ATS_GUARD_MANIFEST"
GUARD_SKIP_COMMAND = "__ats_guard_skip__"
GUARD_SKIP_LEVEL = "L3"                # 跳过看门狗的最低审批等级，只允许调高
GUARD_PASSPHRASE = None                # 启动时从 stdin 读入，绝不进 argv
GUARD_HANDLES = []                     # 运行期持有的核心脚本只读句柄
GUARD_SKIP_ACTIVE = False              # 本次进程是否以 --skip-guard 放行
GUARD_POLICY_SNAPSHOT = None           # 握手时冻结的策略快照
GUARD_VISA_ENV = "ATS_VISA_ID"         # 签证 ID 的环境变量形式，与 --visa-id 等价
GUARD_VISA_ID = None                   # 本次进程携带的签证 ID，权限门凭它匹配 Task/Plan 作用域

DEFAULT_LEASE_AUTONOMY = {
    "enabled": False,
    "max_level": "L2",
    "rules": [],
    "deny_rules": [],
}
DEFAULT_LEASE_STALE = 8.0             # 与 py_consent 的 STALE_AFTER 对齐

POLICY_DEFAULTS = {
    "max_level": DEFAULT_MAX_LEVEL,
    "actor_level": DEFAULT_ACTOR_LEVEL,
    "allow_rules": [],
    "approval_ttl_seconds": DEFAULT_APPROVAL_TTL,
    "keep_runs_per_plan": KEEP_RUNS_DEFAULT,
    "stall_seconds": STALL_SECONDS_DEFAULT,
    "lease_autonomy": DEFAULT_LEASE_AUTONOMY,
    "lease_file": "",
    # —— Pre-flight Sandbox / 审批开关（定案时只改 policy.json，不用回改代码）
    "approval_mode": "ask",
    "approval_mode_by_level": {"L0": "auto", "L1": "auto", "L2": "ask", "L3": "ask"},
    "preflight_dry_run": False,
    "preflight_timeout_seconds": 8,
    "denied_injection_allow_override": True,
    "win32_isolation": False,
    "preflight_fail_closed": True,
    "operation_levels": {"stop": "L2", "delete": "L3"},
    # —— 看门狗跳过审批的等级；只允许比 L3 更高，不允许调低
    "guard_skip_level": GUARD_SKIP_LEVEL,
}

POLICY_ENV = {
    "max_level": "ATS_MAX_LEVEL",
    "actor_level": "ATS_ACTOR_LEVEL",
    "allow_rules": "ATS_ALLOW_RULES",
    "approval_ttl_seconds": "ATS_APPROVAL_TTL",
    "keep_runs_per_plan": "ATS_KEEP_RUNS",
    "stall_seconds": "ATS_STALL",
    "lease_autonomy": "ATS_LEASE_AUTONOMY",
    "lease_file": "ATS_LEASE_FILE",
    "approval_mode": "ATS_APPROVAL_MODE",
    "approval_mode_by_level": "ATS_APPROVAL_MODE_BY_LEVEL",
    "preflight_dry_run": "ATS_PREFLIGHT_DRY_RUN",
    "preflight_timeout_seconds": "ATS_PREFLIGHT_TIMEOUT",
    "denied_injection_allow_override": "ATS_DENIED_INJECTION_OVERRIDE",
    "win32_isolation": "ATS_WIN32_ISOLATION",
    "preflight_fail_closed": "ATS_PREFLIGHT_FAIL_CLOSED",
    "operation_levels": "ATS_OPERATION_LEVELS",
    "guard_skip_level": "ATS_GUARD_SKIP_LEVEL",
}

INT_POLICY_KEYS = ("approval_ttl_seconds", "keep_runs_per_plan", "stall_seconds",
                   "preflight_timeout_seconds")
BOOL_POLICY_KEYS = ("preflight_dry_run", "denied_injection_allow_override", "win32_isolation",
                    "preflight_fail_closed")
APPROVAL_MODES = ("auto", "ask", "deny-only")

# 放松型键：改这些等于放宽执行许可，必须持口令（任何 --set 都要求口令，
# 这里只是把「会放宽的键」在拒绝载荷里点名，便于人判断风险）
RESTRICTED_POLICY_KEYS = ("max_level", "actor_level", "allow_rules", "approval_mode",
                          "approval_mode_by_level", "operation_levels",
                          "denied_injection_allow_override", "win32_isolation",
                          "preflight_fail_closed", "preflight_dry_run",
                          "lease_autonomy", "lease_file", "guard_skip_level")


# ---------------------------------------------------------------- 基础 IO

def ats_root(root: str | None = None) -> Path:
    """解析状态根。优先级：显式 root > 环境变量 ATS_ROOT > <ats 本体目录>/.ats。

    默认取脚本所在目录的父目录（即 OCSKIS/），而不是当前工作目录：
    否则从不同 cwd 调用会各自生成一份 .ats，信任根被拆散。
    """
    if root:
        return Path(root)
    env = os.environ.get("ATS_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / ".ats"


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

def rule_list(value):
    """把规则归一成字符串列表：支持 list / "a||b" 字符串；非法项直接丢弃。"""
    if isinstance(value, str):
        value = [x.strip() for x in value.split("||") if x.strip()]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def normalize_lease_autonomy(value):
    """把 lease_autonomy 归一成固定四字段；任何不合法输入一律回落到「关闭」。

    这是「租约免审」的总闸：enabled 为假时权限门根本不读租约，
    所以配置损坏、字段拼错、类型不对都不会意外放开审批。
    """
    cfg = {"enabled": False, "max_level": DEFAULT_LEASE_AUTONOMY["max_level"],
           "rules": [], "deny_rules": []}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return cfg
    if not isinstance(value, dict):
        return cfg
    enabled = value.get("enabled")
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
    cfg["enabled"] = bool(enabled)
    level = str(value.get("max_level") or DEFAULT_LEASE_AUTONOMY["max_level"]).upper()
    cfg["max_level"] = level if level in LEVEL_RANK else DEFAULT_LEASE_AUTONOMY["max_level"]
    cfg["rules"] = rule_list(value.get("rules"))
    cfg["deny_rules"] = rule_list(value.get("deny_rules"))
    return cfg


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def normalize_approval_mode(value, default="ask"):
    text = str(value or "").strip().lower().replace("_", "-")
    return text if text in APPROVAL_MODES else default


def normalize_mode_map(value):
    """按等级审批模式表；非法键/值直接丢弃，不阻断读取。"""
    out = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return out
    if not isinstance(value, dict):
        return out
    for key, mode in value.items():
        level = str(key).upper()
        if level in LEVEL_RANK:
            out[level] = normalize_approval_mode(mode, POLICY_DEFAULTS["approval_mode"])
    return out


def approval_mode_for(pol, level) -> str:
    """按等级取审批模式：approval_mode_by_level > approval_mode > ask。"""
    modes = pol.get("approval_mode_by_level") or {}
    mode = modes.get(str(level).upper()) or pol.get("approval_mode") or "ask"
    return normalize_approval_mode(mode)


def normalize_level_map(value):
    """操作等级表（stop/delete 等）；非法等级丢弃，不阻断读取。"""
    out = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return out
    if not isinstance(value, dict):
        return out
    for key, level in value.items():
        text = str(level).upper()
        if text in LEVEL_RANK:
            out[str(key)] = text
    return out


def operation_level(pol, name, default="L2") -> str:
    """取某操作型命令的等级；未配置或非法时回落到 default。"""
    levels = pol.get("operation_levels") or {}
    text = str(levels.get(str(name)) or "").upper()
    return text if text in LEVEL_RANK else str(default).upper()


DERIVE_L3_PATTERN = re.compile(
    r"(\b(rm|rmdir|del|erase|format|mkfs|shutdown|reboot)\b|"
    r"\breg\s+(add|delete)\b|"
    r"\bgit\s+push\s+(--force|-f)\b|"
    r"\bgit\s+reset\s+--hard\b|"
    r"\bgit\s+clean\s+-[a-z]*f\b|"
    r"\bgit\s+(checkout|restore)\s+(--\s+)?\.|"
    r"\bnpm\s+(i|install)\s+-g\b|"
    r"\bpip\s+install\b|"
    r"\bchmod\s+777\b|\bchown\b|"
    r"\bremove-item|"
    r"\bclear-(content|recyclebin|disk)\b|"
    r"\b(stop|restart)-computer\b|"
    r"\btruncate\b|"
    r"\bdd\s+if=|"
    r"\bsc\s+delete\b|"
    r"\bbcdedit\b|"
    r"\bdiskpart\b|"
    r"\brobocopy[^\n]*/mir\b|"
    r"\bcertutil[^\n]*-decode\b|"
    r"\bvssadmin\s+delete\b|"
    r"\bwbadmin\s+delete\b|"
    r"\bwevtutil\s+cl\b|"
    r"\bbitsadmin[^\n]*(?:/add|/transfer|/create|/set)\b|"
    r"\bnet\s+(user|localgroup)[^\n]*(?:/add|/delete)\b)",
    re.IGNORECASE)
DERIVE_L2_PATTERN = re.compile(
    r"([A-Za-z]:\\|/etc/|/usr/|/bin/|\.ssh|"
    r"\|\s*(sh|bash)\b|curl\s+\S+\s*\|\s*(sh|bash)|"
    r"invoke-expression|\biex\b|\biwr\b|>\s*[A-Za-z~/])",
    re.IGNORECASE)


def _risk_texts(command):
    """原文 + 静态解出的编码载荷：等级推导要看得到编码里面的东西。

    只看原文时 `powershell -EncodedCommand <base64>` 会被当成普通字符串，
    危险命令就此洗成 L1 静默直通；解码只做纯文本还原，不执行、不落盘。
    """
    texts = [str(command or "")]
    if ats_preflight is not None:
        try:
            texts.extend(ats_preflight.decoded_texts(command))
        except Exception:                # noqa: BLE001
            pass
    return texts


def derive_level(command, default=None) -> str:
    """未传 --level 时按命令文本推等级：只抬高风险，判不准回落到 default。

    正则匹配原文与静态解码后的载荷；仍看不懂变量拼接与脚本内部行为，
    它的用途是把明显的危险命令抬进审批，不是识别恶意。
    """
    for text in _risk_texts(command):
        if DERIVE_L3_PATTERN.search(text):
            return "L3"
        if DERIVE_L2_PATTERN.search(text):
            return "L2"
    return str(default or DEFAULT_TASK_LEVEL).upper()


DERIVE_FLOOR_L2_PATTERN = re.compile(
    r"(\|\s*(sh|bash)\b|curl\s+\S+\s*\|\s*(sh|bash)|"
    r"invoke-expression|\biex\b|\biwr\b|(?<!-)>\s*[A-Za-z~/]|\.ssh|"
    r"\bset-content\b|\btaskkill[^\n]*/f\b)",
    re.IGNORECASE)


def hard_floor_level(command):
    """硬危险特征的下限等级；路径类启发式不参与，避免误伤普通绝对路径。"""
    for text in _risk_texts(command):
        if DERIVE_L3_PATTERN.search(text):
            return "L3"
        if DERIVE_FLOOR_L2_PATTERN.search(text):
            return "L2"
    return None


def floor_level(level, command, default=None):
    """显式等级只能抬高，不能压过硬危险特征的下限。

    未命中硬危险特征时保持显式等级；命中则取两者较高者，
    避免 --level L0 把删除、管道执行一类命令送进 auto 直通。
    """
    chosen = str(level or default or DEFAULT_TASK_LEVEL).upper()
    hard = hard_floor_level(command)
    if hard and level_rank(hard) > level_rank(chosen):
        return hard
    return chosen


def pack_level(explicit, commands):
    """指令包整体等级：显式等级、逐条推导、逐条硬下限三者取最高。

    与 floor_level 的区别是包内每条命令各自算下限再取最大，
    避免同包里的安全命令把危险命令的下限稀释掉。
    """
    derived = max([derive_level(c) for c in commands] or [DEFAULT_TASK_LEVEL],
                  key=level_rank)
    floor = max([hard_floor_level(c) or DEFAULT_TASK_LEVEL for c in commands]
                or [DEFAULT_TASK_LEVEL], key=level_rank)
    return str(max([explicit or derived, derived, floor], key=level_rank)).upper()


def policy(root, data=None):
    """读取策略：默认值 < policy.json < 环境变量。

    返回 dict，键集合固定为 POLICY_DEFAULTS 的键；lease_autonomy 与 lease_file
    是「存活租约免审」的两个开关，默认关闭，必须显式配置才生效。

    看门狗握手成功后会把本次读到的策略冻结进 GUARD_POLICY_SNAPSHOT：运行中
    原地改写 policy.json 不影响本次决策（改策略要重封签，见 cmd_policy）。
    """
    if data is None:
        snap = GUARD_POLICY_SNAPSHOT
        if snap and snap.get("root") == _policy_root_key(root):
            return _copy_policy(snap.get("data") or {})
    pol = {key: (dict(value) if isinstance(value, dict) else
                 list(value) if isinstance(value, (list, tuple)) else value)
           for key, value in POLICY_DEFAULTS.items()}
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
        elif key in BOOL_POLICY_KEYS:
            pol[key] = as_bool(raw)
        elif key == "approval_mode_by_level":
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                pol[key] = parsed
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
    pol["preflight_timeout_seconds"] = max(1, min(60, pol["preflight_timeout_seconds"]))
    for key in BOOL_POLICY_KEYS:
        pol[key] = as_bool(pol[key])
    for key in ("max_level", "actor_level"):
        pol[key] = str(pol[key]).upper()
        if pol[key] not in LEVEL_RANK:
            pol[key] = POLICY_DEFAULTS[key]
    pol["approval_mode"] = normalize_approval_mode(pol.get("approval_mode"))
    pol["approval_mode_by_level"] = normalize_mode_map(pol.get("approval_mode_by_level"))
    pol["operation_levels"] = normalize_level_map(pol.get("operation_levels"))
    pol["lease_autonomy"] = normalize_lease_autonomy(pol.get("lease_autonomy"))
    pol["lease_file"] = str(pol.get("lease_file") or "").strip()
    pol["guard_skip_level"] = normalize_floor_level(pol.get("guard_skip_level"),
                                                    GUARD_SKIP_LEVEL)
    return pol


def _policy_root_key(root):
    """策略快照的归属键：同一 root 的不同路径写法要收敛到同一个键。"""
    try:
        return str(Path(root).resolve())
    except Exception:                    # noqa: BLE001
        return str(root)


def _copy_policy(data):
    return {key: (dict(value) if isinstance(value, dict) else
                  list(value) if isinstance(value, (list, tuple)) else value)
            for key, value in (data or {}).items()}


def normalize_floor_level(value, floor) -> str:
    """归一化「只允许调高」的等级：非法值退回 floor，低于 floor 一律抬到 floor。"""
    level = str(value or "").upper()
    if level not in LEVEL_RANK:
        return floor
    return level if level_rank(level) >= level_rank(floor) else floor


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


def find_pending_approval(root, command, context, statuses=("pending",)):
    """查找同一条命令、同一个任务实例的待决审批，用于去重复用。

    重复发起同一条被拦命令时复用既有待决记录，避免每试一次就堆一条待办。
    """
    context = dict(context or {})
    for p in sorted(approvals_dir(root).glob("ap-*.json")):
        rec = jread(p, {})
        if not isinstance(rec, dict):
            continue
        if rec.get("status") not in statuses:
            continue
        if rec.get("command") != command:
            continue
        if rec.get("source") != context.get("source", "operation"):
            continue
        if rec.get("task_id") != context.get("task_id"):
            continue
        if not approval_instance_match(rec, context):
            continue
        if approval_expired(rec):
            continue
        return rec
    return None


def approval_instance_match(record, context):
    """审批是否绑定在同一个任务实例上。

    删除/停止这类操作型审批的 command 由任务 id 拼成，而任务 id 可以被回收复用；
    若不校验实例，旧审批就能作用于同名的新任务。判据优先级：instance_id →
    created_at（兼容未写 instance_id 的老任务）→ 一律判不匹配（fail-closed）。
    """
    if not record:
        return False
    context = dict(context or {})
    task_id = context.get("task_id")
    if not task_id:
        return True
    if record.get("task_id") != task_id:
        return False
    instance = context.get("task_instance")
    if instance:
        return record.get("task_instance") == instance
    created = context.get("task_created_at")
    if created is not None:
        return record.get("task_created_at") == created
    return False


def _normalize_command(text) -> str:
    """命令归一化：只折叠空白，不改变语义。

    审批单上记的是人看到的那条命令，重试时多敲几个空格不该判 mismatch；
    但脚本名、参数一个字符都不能变。
    """
    return " ".join(str(text or "").split())


def command_scope_hash(text) -> str:
    """Task Visa 绑定的命令指纹：SHA-256(归一化原文)。

    归一化只折叠空白，与 `_normalize_command` 同一口径 —— 签发时按 task.json
    里的原文算，消费时按权限门实际看到的 gate_text 算，两边必须逐字一致，
    否则就是「拿这张签证去跑别的命令」，直接判 visa_mismatch。
    """
    return hashlib.sha256(_normalize_command(text).encode("utf-8")).hexdigest()


SCRIPT_SUFFIXES = (".py", ".ps1", ".sh", ".bat", ".cmd", ".js", ".mjs")


def artifact_digests(command, context=None):
    """命令里能静态定位的脚本文件指纹；把审批绑到「批准那一刻的内容」。

    只认后缀白名单且确实存在的文件：`echo hi`、`python -c "..."`、
    `git status` 这类没有可绑定实体的命令返回空列表。

    命令带 `@enc` 钩子时只看 `@enc` 之前的主命令：钩子参数是交给转换器的
    数据路径（常见就是待转码的 .py/.sh），既不执行也不是审批的执行体，
    算进指纹会让转码目标内容一变就误报 artifact_changed。
    """
    context = dict(context or {})
    cwd = context.get("cwd")
    text = str(command or "")
    m = HOOK_PATTERN.search(text)
    if m:
        text = text[:m.start()]
    out = []
    for token in text.split():
        token = token.strip("\"'")
        if not token.lower().endswith(SCRIPT_SUFFIXES):
            continue
        path = Path(token)
        if not path.is_absolute():
            if not cwd:
                continue
            path = Path(cwd) / path
        digest = _sha256_file(path)
        if digest is None:
            continue
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        out.append({"path": resolved, "sha256": digest})
    return out


def artifact_changed(record, command, context=None) -> bool:
    """审批时记下的脚本指纹与现在是否一致。

    老记录（没有 artifacts 字段）不参与判定，保持旧行为；
    新记录一律比对，脚本被换掉或删掉都算变化。
    """
    recorded = record.get("artifacts")
    if recorded is None:
        return False
    current = artifact_digests(command, context)
    if len(current) != len(recorded):
        return True
    for old, new in zip(recorded, current):
        if old.get("path") != new.get("path") or old.get("sha256") != new.get("sha256"):
            return True
    return False


def create_approval(root, command, required_level, context=None, ttl=None, resume=None,
                    preflight=None, status="pending"):
    context = dict(context or {})
    pol = policy(root)
    if ttl is None:
        ttl = int(pol.get("approval_ttl_seconds", DEFAULT_APPROVAL_TTL))
    created = now_ts()
    reuse = find_pending_approval(root, command, context, ("pending", "denied_injection"))
    if reuse:
        if preflight is not None:
            reuse["preflight"] = preflight
        if "artifacts" not in reuse:
            reuse["artifacts"] = artifact_digests(command, context)
        if status == "denied_injection" and reuse.get("status") == "pending":
            reuse["status"] = "denied_injection"
        save_approval(root, reuse)
        return reuse
    record = {
        "id": new_approval_id(),
        "command": command,
        "required_level": str(required_level).upper(),
        "status": status,
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
        "task_instance": context.get("task_instance"),
        "task_created_at": context.get("task_created_at"),
        "resume": resume or context.get("resume"),
        "artifacts": artifact_digests(command, context),
    }
    if preflight is not None:
        record["preflight"] = preflight
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


def approval_check(root, approval_id, command, context=None):
    """返回 {"ok", "error", "record"}；error ∈ missing/pending/rejected/expired/mismatch/artifact_changed。"""
    record = load_approval(root, approval_id)
    if not record:
        return {"ok": False, "error": "missing", "record": None}
    status = record.get("status")
    error = None
    if status == "rejected":
        error = "rejected"
    elif status == "expired":
        error = "expired"
    elif status == "denied_injection":
        error = "denied_injection"
    elif status == "approved" and approval_expired(record):
        error = "expired"
    elif status != "approved":
        error = "pending"
    elif _normalize_command(record.get("command")) != _normalize_command(command):
        error = "mismatch"
    elif artifact_changed(record, command, context):
        error = "artifact_changed"
    return {"ok": error is None, "error": error, "record": record}


def approval_allows(root, approval_id, command, context=None) -> bool:
    return approval_check(root, approval_id, command, context)["ok"]


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

def regex_match(rules, text):
    """在给定规则集里找第一条命中的规则；未命中返回 None。规则按正则、忽略大小写。"""
    for rule in rules or []:
        try:
            if re.search(rule, text or "", re.IGNORECASE):
                return rule
        except re.error:
            continue
    return None


def lease_path(root, pol=None) -> Path:
    """租约文件位置：默认取 ATS 根目录同级的 temp/py_lease.json。

    ATS_ROOT 可能指向 <repo>/.ats，也可能指向 <repo>/.autopilot/.ats；后者的同级
    temp 才是 py_consent 的落点。未显式指定时按候选顺序取第一个存在的，避免
    「配置没错却永远读不到租约」。候选都不存在则返回首个（判为 lease_absent）。
    """
    custom = str((pol or policy(root)).get("lease_file") or "").strip()
    if custom:
        path = Path(custom)
        return path if path.is_absolute() else Path(root) / path
    base = Path(root).parent
    candidates = [
        base / "temp" / "py_lease.json",
        base / ".autopilot" / "temp" / "py_lease.json",
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[0]


def lease_state(root, pol=None):
    """只读判定 py 授权租约是否存活，返回 (租约 dict 或 None, 判定码)。

    判据与 py_consent.check 一致：granted、心跳未过期、未到期限、宿主 PID 存活。
    任何一环读不出来都判为无效（fail-closed）；本函数只读，不写、不删、不杀进程。
    """
    path = lease_path(root, pol)
    if not path.exists():
        return None, "lease_absent"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None, "lease_unreadable"
    if not isinstance(data, dict) or not data.get("granted"):
        return None, "lease_invalid"
    try:
        beat = float(data.get("heartbeat_epoch", 0))
    except (TypeError, ValueError):
        return None, "lease_heartbeat_broken"
    try:
        stale = float(data.get("stale_after") or DEFAULT_LEASE_STALE)
    except (TypeError, ValueError):
        stale = DEFAULT_LEASE_STALE
    if time.time() - beat > stale:
        return None, "lease_stale"
    expires = data.get("expires_epoch")
    if expires:
        try:
            if time.time() > float(expires):
                return None, "lease_expired"
        except (TypeError, ValueError):
            return None, "lease_expiry_broken"
    if not pid_alive(data.get("pid")):
        return None, "lease_host_dead"
    return data, "lease_alive"


LEASE_CONSTRAINT_GUARDS = (
    ("删除", ("删除", "del ", "erase", "remove", "rm ", "rmdir", "unlink", "rimraf")),
    ("覆盖", ("覆盖", "overwrite", "copy ", "copy-item", "move ", "move-item",
              "mv ", "robocopy", "rename")),
    ("联网", ("联网", "下载", "download", "curl", "wget", "invoke-webrequest",
              "http://", "https://", "ftp://")),
)


def lease_constraint_block(record, text):
    """租约里勾了「额外禁止」时，命中对应动作就不给免审，退回人工审批。

    这是租约自带的收紧项，不是策略配置：宁可多问一次，也不越过用户勾过的边界。
    """
    haystack = (text or "").lower()
    for item in record.get("extra_constraints") or []:
        for keyword, patterns in LEASE_CONSTRAINT_GUARDS:
            if keyword not in str(item):
                continue
            for pattern in patterns:
                try:
                    if re.search(pattern, haystack, re.IGNORECASE):
                        return "%s约束命中 /%s/" % (keyword, pattern)
                except re.error:
                    continue
    return None


def create_lease_approval(root, command, required_level, lease, rule, context=None):
    """把「租约免审」落成一条已通过的审批记录，供事后追溯。

    与人工审批的区别写进 resolved_by / note / auto_approved 三个字段：
    它从诞生起就是 approved，不写 pending、不投递 approval_created，
    这样待办列表不会被虚假条目污染，但每次放行仍然留痕。
    """
    context = dict(context or {})
    created = now_ts()
    record = {
        "id": new_approval_id(),
        "command": command,
        "required_level": str(required_level).upper(),
        "status": "approved",
        "created_at": created,
        "created_iso": iso(created),
        "expires_at": None,
        "resolved_at": created,
        "resolved_iso": iso(created),
        "resolved_by": "py_lease:%s" % lease.get("token"),
        "note": "lease_autonomy: %s" % rule,
        "source": context.get("source", "operation"),
        "schedule_id": context.get("schedule_id"),
        "plan_id": context.get("plan_id"),
        "stage_id": context.get("stage_id"),
        "step_id": context.get("step_id"),
        "task_id": context.get("task_id"),
        "task_instance": context.get("task_instance"),
        "task_created_at": context.get("task_created_at"),
        "resume": None,
        "auto_approved": True,
        "lease_token": lease.get("token"),
        "lease_pid": lease.get("pid"),
        "lease_started_at": lease.get("started_at"),
        "lease_heartbeat_at": lease.get("heartbeat_at"),
    }
    save_approval(root, record)
    log_event(root, "approval_lease_auto", approval_id=record["id"],
              required_level=record["required_level"], rule=rule,
              lease_token=record["lease_token"], source=record["source"],
              task_id=record["task_id"], plan_id=record["plan_id"],
              schedule_id=record["schedule_id"])
    return record


def create_visa_approval(root, command, required_level, visa, scope, context=None):
    """把「签证自动放行」落成一条只供追溯的审批记录。

    与租约免审的关键区别：`expires_at` 直接写成创建时刻，即「落地即过期」。
    签证的每一次放行都必须现场消费配额，这条记录**不是可复用凭证** ——
    否则事后拿 `--approval <id>` 重放，就能绕开签证配额一直跑。
    status 仍是 approved（与租约一致，便于 approval prune 统一清理），
    真正的身份写在 resolved_by = visa:<visa_id> 与 auto_approved 两个字段上。
    """
    context = dict(context or {})
    created = now_ts()
    record = {
        "id": new_approval_id(),
        "command": command,
        "required_level": str(required_level).upper(),
        "status": "approved",
        "created_at": created,
        "created_iso": iso(created),
        "expires_at": created,
        "resolved_at": created,
        "resolved_iso": iso(created),
        "resolved_by": "visa:%s" % visa.get("visa_id"),
        "note": "visa_auto_approved: scope=%s" % (scope.get("scope_type") or ""),
        "source": context.get("source", "operation"),
        "schedule_id": context.get("schedule_id"),
        "plan_id": context.get("plan_id"),
        "stage_id": context.get("stage_id"),
        "step_id": context.get("step_id"),
        "task_id": context.get("task_id"),
        "task_instance": context.get("task_instance"),
        "task_created_at": context.get("task_created_at"),
        "resume": None,
        "auto_approved": True,
        "visa_id": visa.get("visa_id"),
        "visa_scope_type": scope.get("scope_type"),
        "visa_scope_hash": scope.get("scope_hash"),
        "visa_remaining": visa.get("remaining"),
    }
    save_approval(root, record)
    log_event(root, "visa_auto_approved", approval_id=record["id"],
              visa_id=record["visa_id"], scope_type=record["visa_scope_type"],
              scope_hash=record["visa_scope_hash"], remaining=record["visa_remaining"],
              required_level=record["required_level"], source=record["source"],
              task_id=record["task_id"], plan_id=record["plan_id"],
              schedule_id=record["schedule_id"], step_id=record["step_id"])
    emit_event(root, {"type": "visa_auto_approved", "visa_id": record["visa_id"],
                      "approval_id": record["id"],
                      "scope_type": record["visa_scope_type"],
                      "remaining": record["visa_remaining"],
                      "level": record["required_level"],
                      "task_id": record["task_id"], "plan_id": record["plan_id"],
                      "stage_id": record["stage_id"], "step_id": record["step_id"]})
    return record


def lease_autonomy(root, required_level, text, context=None):
    """存活租约 + 命中白名单 → 免审放行；不适用时返回 None，交回原有审批流程。

    五道关缺一不可：
      1) policy.lease_autonomy.enabled 为真（总闸，默认关）
      2) required_level ≤ min(policy 上限, 租约 Q4 授予上限)
      3) 租约存活（心跳未过期 / 未到期限 / 宿主进程仍在）
      4) 命令命中 rules 且不命中 deny_rules
      5) 租约自带的「额外禁止」约束未命中
    策略 max_level 的硬上限由调用方先行拦截，本函数不参与放宽上限。
    """
    pol = policy(root)
    cfg = pol.get("lease_autonomy") or {}
    if not cfg.get("enabled") or not cfg.get("rules"):
        return None
    record, state = lease_state(root, pol)
    if record is None:
        return None
    granted = record.get("ats_autonomy") or {}
    if not granted.get("granted"):
        return None
    cap = lower_level(str(cfg.get("max_level")).upper(),
                      str(granted.get("max_level") or "L2").upper())
    if level_rank(required_level) > level_rank(cap):
        return None
    hit = regex_match(cfg["rules"], text)
    if not hit:
        return None
    if regex_match(cfg.get("deny_rules"), text):
        return None
    blocked = lease_constraint_block(record, text)
    if blocked:
        log_event(root, "lease_autonomy_blocked", reason=blocked,
                  lease_token=record.get("token"), level=required_level)
        return None
    approval = create_lease_approval(root, text, required_level, record, hit, context)
    return {"allowed": True, "reason": "lease_autonomy", "rule": hit,
            "lease_token": record.get("token"), "lease_pid": record.get("pid"),
            "lease_state": state, "approval_id": approval["id"],
            "approval": approval}


def permission_gate(root, command, approval_id=None, context=None):
    """Task/Step 执行前的权限门。

    context 可含：required_level / task_id / schedule_id / plan_id / stage_id /
    step_id / source / whitelist_text。
    返回 {"allowed", "reason", "required_level", "actor_level", "policy_max",
          "effective_level", "whitelist", "approval_id", "approval_error", "approval",
          "lease"}。
    reason ∈ whitelist / level / lease_autonomy / approval / denied_policy /
             denied_mode / approval_required

    approval_mode（auto/ask/deny-only）是「是否走人工审批」的独立闸门：
      auto      —— 维持 actor_level 直通（旧行为）；
      ask       —— 受控等级不再直通，必须走审批；
      deny-only —— 受控等级直接拒绝，不产生可批准记录。
    """
    context = dict(context or {})
    pol = policy(root)
    required = str(context.get("required_level") or DEFAULT_TASK_LEVEL).upper()
    hard = hard_floor_level(command)
    if hard and level_rank(hard) > level_rank(required):
        required = hard
    # 跳过看门狗不降低任何执行等级：本次进程里所有命令的下限抬到 guard_skip_level
    if GUARD_SKIP_ACTIVE:
        required = normalize_floor_level(required, pol.get("guard_skip_level") or GUARD_SKIP_LEVEL)
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
        "lease": None,
        "allowed": False,
        "reason": None,
        "approval_mode": None,
    }
    rule = whitelist_match(root, context.get("whitelist_text") or command)
    if rule and not hard and not GUARD_SKIP_ACTIVE:
        result.update(allowed=True, reason="whitelist", whitelist=rule)
        return result
    if rule:
        result["whitelist_ignored"] = rule
    if level_rank(required) > level_rank(cap):
        result.update(allowed=False, reason="denied_policy")
        return result
    mode = approval_mode_for(pol, required)
    result["approval_mode"] = mode
    if mode == "deny-only":
        result.update(allowed=False, reason="denied_mode")
        return result
    if mode == "auto" and level_rank(required) <= level_rank(actor):
        result.update(allowed=True, reason="level")
        return result
    lease = lease_autonomy(root, required, context.get("whitelist_text") or command, context)
    if lease:
        result.update(allowed=True, reason="lease_autonomy", whitelist=lease["rule"],
                      approval_id=lease["approval_id"], approval=lease["approval"],
                      lease=lease)
        return result
    check = approval_check(root, approval_id, command, context)
    if check["ok"] and not approval_instance_match(check["record"], context):
        check = {"ok": False, "error": "mismatch", "record": check["record"]}
    result["approval"] = check["record"]
    result["approval_error"] = check["error"]
    if check["ok"]:
        result.update(allowed=True, reason="approval")
        return result
    visa = _visa_task_gate(root, command, context, result)
    if visa is not None:
        return visa
    result.update(allowed=False, reason="approval_required")
    return result


def gate_preview(root, command, required_level, context=None):
    """权限门的纯读预演：算等级、模式、白名单，不写审批、不判租约。

    与 permission_gate 的判定链对齐到「模式判定」为止，差在两步刻意不做：
      * 不调用 lease_autonomy —— 它命中后会 create_lease_approval，落盘；
      * 不做 approval_check —— 那是查已有审批，预演时本来就没有。
    所以 would 只表示「若不免审、不批，会怎样」，调用方必须看 lease_checked /
    approval_checked 两个标记，别把 would=allow 当成一定能直通。

    extend check 用它：预演阶段不能留下任何审批记录，否则「只读」就不成立。
    """
    context = dict(context or {})
    pol = policy(root)
    required = str(required_level or DEFAULT_TASK_LEVEL).upper()
    hard = hard_floor_level(command)
    if hard and level_rank(hard) > level_rank(required):
        required = hard
    if GUARD_SKIP_ACTIVE:
        required = normalize_floor_level(required,
                                         pol.get("guard_skip_level") or GUARD_SKIP_LEVEL)
    actor = str(pol.get("actor_level") or DEFAULT_ACTOR_LEVEL).upper()
    cap = str(pol.get("max_level") or DEFAULT_MAX_LEVEL).upper()
    out = {
        "required_level": required,
        "actor_level": actor,
        "policy_max": cap,
        "effective_level": lower_level(required, lower_level(actor, cap)),
        "hard_floor": hard,
        "approval_mode": None,
        "whitelist": None,
        "would": None,
        "reason": None,
        "lease_checked": False,
        "approval_checked": False,
    }
    rule = whitelist_match(root, context.get("whitelist_text") or command)
    if rule and not hard and not GUARD_SKIP_ACTIVE:
        out.update(would="allow", reason="whitelist", whitelist=rule)
        return out
    if rule:
        out["whitelist_ignored"] = rule
    if level_rank(required) > level_rank(cap):
        out.update(would="deny", reason="denied_policy")
        return out
    mode = approval_mode_for(pol, required)
    out["approval_mode"] = mode
    if mode == "deny-only":
        out.update(would="deny", reason="denied_mode")
        return out
    if mode == "auto" and level_rank(required) <= level_rank(actor):
        out.update(would="allow", reason="level")
        return out
    out.update(would="approval_required", reason="approval_required")
    return out


def preflight_review(root, command, level, context=None, gate=None, shell=True):
    """权限门之后、真正 spawn 之前的预演入口。

    whitelist 直接跳过；denied_policy / denied_mode 不再跑侵入式检查。
    预检自身异常时按 preflight_fail_closed 决定：True 判 deny_injection（要人看），
    False 才退化为 None（旧行为）。
    """
    if ats_preflight is None:
        return None
    gate = gate or {}
    if gate.get("reason") in ("whitelist", "denied_policy", "denied_mode"):
        return None
    ctx = dict(context or {})
    ctx.setdefault("source", "run")
    try:
        return ats_preflight.review(root, command, level, context=ctx,
                                    pol=policy(root), shell=bool(shell))
    except Exception as exc:             # noqa: BLE001
        try:
            fail_closed = as_bool(policy(root).get("preflight_fail_closed"))
        except Exception:                # noqa: BLE001
            fail_closed = bool(POLICY_DEFAULTS["preflight_fail_closed"])
        if not fail_closed:
            return None
        return {"status": "error", "verdict": "deny_injection",
                "reason": "preflight_error", "tags": [], "confidence": "none",
                "error": "%s: %s" % (type(exc).__name__, exc)}


def gate_operation(root, level, label, approval_id=None, context=None):
    """操作型（stop/delete）权限门。

    操作型命令走独立闸门：与 permission_gate 共用策略上限与存活租约逻辑，
    并读取 approval_mode：auto 且 level <= actor_level 才直接放行；
    ask / deny-only 一律不因 actor_level 或白名单而放行。
    失败时自动创建审批并投递事件。
    """
    context = dict(context or {})
    context.setdefault("source", "operation")
    pol = policy(root)
    actor = str(pol.get("actor_level") or DEFAULT_ACTOR_LEVEL).upper()
    cap = str(pol.get("max_level") or DEFAULT_MAX_LEVEL).upper()
    level = str(level).upper()
    # 跳过看门狗不降低任何执行等级：操作型闸门同样抬到 guard_skip_level
    if GUARD_SKIP_ACTIVE:
        level = normalize_floor_level(level, pol.get("guard_skip_level") or GUARD_SKIP_LEVEL)

    if level_rank(level) > level_rank(cap):
        return {"allowed": False, "reason": "denied_policy", "error": "denied_policy",
                "label": label, "level": level}
    mode = approval_mode_for(pol, level)
    if mode == "deny-only":
        return {"allowed": False, "reason": "denied_mode", "error": "denied_mode",
                "label": label, "level": level, "approval_mode": mode}
    rule = whitelist_match(root, context.get("whitelist_text") or label)
    if rule and mode == "auto":
        return {"allowed": True, "reason": "whitelist", "whitelist": rule,
                "label": label, "level": level, "approval_mode": mode}
    if mode == "auto" and level_rank(level) <= level_rank(actor):
        return {"allowed": True, "reason": "level", "label": label,
                "level": level, "approval_mode": mode}

    lease = lease_autonomy(root, level, context.get("whitelist_text") or label, context)
    if lease:
        return {"allowed": True, "reason": "lease_autonomy", "whitelist": lease["rule"],
                "label": label, "level": level, "approval_id": lease["approval_id"],
                "approval": lease["approval"], "lease": lease}

    check = approval_check(root, approval_id, label, context)
    if check["ok"] and not approval_instance_match(check["record"], context):
        check = {"ok": False, "error": "mismatch", "record": check["record"]}
    if check["ok"]:
        return {"allowed": True, "reason": "approval", "label": label,
                "approval": check["record"], "level": level, "approval_mode": mode}
    record = create_approval(root, label, level, context=context, resume=context.get("resume"))
    announce_approval(root, record)
    blocked = {"allowed": False, "reason": "approval_required", "error": "approval_required",
               "label": label, "level": level, "approval_error": check["error"],
               "approval_id": record["id"], "approval": record, "approval_mode": mode}
    if rule:
        blocked["whitelist_ignored"] = rule
    return blocked


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
        done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                              capture_output=True)
        if done.returncode == 0:
            return True
        return not pid_alive(pid)
    try:
        pgid = os.getpgid(pid)
        if pgid == os.getpgid(0):
            # 目标仍在调用者自己的进程组里：killpg 会连发起 stop 的进程一起杀掉。
            # 拒绝整组杀，让调用方看到 leftovers，而不是假装成功。
            return False
        os.killpg(pgid, signal.SIGKILL)
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
                             capture_output=True)
        return str(pid).encode() in (out.stdout or b"")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_process_tree(task, wait_seconds=2.0):
    """停止任务进程（树），返回 (leftovers, checked)。

    POSIX 下任务由 start_new_session 起成独立进程组，killpg 一次带走整组；
    Windows 下 taskkill /T 带走整棵子树。杀完轮询确认，仍有存活的再扫一次。
    """
    roots = [p for p in (task.get("pid"), task.get("supervisor_pid")) if p]
    if not roots:
        return [], 0
    pids = {int(r) for r in roots}
    for pid in pids:
        kill_pid(pid)

    alive = {p for p in pids if pid_alive(p)}
    deadline = time.time() + max(0.0, wait_seconds)
    while alive and time.time() < deadline:
        time.sleep(0.1)
        alive = {p for p in alive if pid_alive(p)}
    for pid in alive:
        kill_pid(pid)
    deadline = time.time() + 1.0
    while alive and time.time() < deadline:
        time.sleep(0.1)
        alive = {p for p in alive if pid_alive(p)}
    return sorted(alive), len(pids)


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
        fresh = load_task(root, task["id"])
        if fresh and fresh.get("finalized"):
            return fresh, True
        if code_file.exists():
            try:
                code = int(code_file.read_text(encoding="utf-8").strip() or "-1")
            except ValueError:
                code = -1
            status = "succeeded" if code == 0 else "failed"
            finalize_task(root, task, status, code)
            return task, True
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

    level = floor_level(args.level or spec.get("level"),
                        hook_gate_text(command, post_hook))
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
        "instance_id": uuid.uuid4().hex[:12],
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


def hook_gate_text(command, hook):
    """权限门与等级推导看到的完整原文：主命令 + `@enc` 参数。

    钩子参数不参与 shell 执行（见 split_hook_args），但它是用户写下的原文。
    不把它纳入判定，`echo hi @enc x; rm -rf /` 就能搭 L0 主命令的车直通执行。
    """
    if not hook or hook.get("error"):
        return command
    return "%s @enc %s" % (command, hook.get("args") or "")


def split_hook_args(text):
    """按空白切分钩子参数，单/双引号包裹的段保持完整。

    只分词，不解释 `; | > & $ ` 这类 shell 元字符 —— 钩子用 argv 列表执行，
    它们只会作为普通参数交给转换器，不会变成第二条命令。
    """
    tokens, buf, quote = [], [], None
    for ch in text or "":
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def resolve_encoder(task):
    """定位 encoding_converter.py：ATS_ENCODER_PATH > <cwd>/tools > 当前目录/tools。"""
    env_path = os.environ.get("ATS_ENCODER_PATH")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    bases = [Path(task["cwd"])] if task.get("cwd") else []
    bases.append(Path.cwd())
    for base in bases:
        cand = base / "tools" / "encoding_converter.py"
        if cand.is_file():
            return cand
    return None


HOOK_GLOB_CHARS = ("*", "?", "[")
DEFAULT_HOOK_GLOB_MAX = 256


def _hook_arg_roles(tokens):
    """标注每个钩子参数的角色：`flag` / `value` / `positional`。

    只用于判断「哪个 token 是路径」，不参与执行：以 `-` 开头的是 flag，
    `--x` 后面紧跟的 token 是它的值（`--x=v` 自带值，不吞下一个）。
    前一个 flag 是否吃值无法从参数串本身推断（转换器的 flag 表未知），所以
    以 `-` 开头的 token **永远不会**被当成值——否则 `--dry-run --from gbk`
    这种「无值开关 + 真 flag」的写法会把 `--from` 记成值、把 `gbk` 记成位置
    参数，进而让后面的通配误报 hook_glob_ambiguous。
    """
    roles, want_value = [], False
    for tok in tokens:
        if tok.startswith("-"):
            roles.append("flag")
            want_value = "=" not in tok
        elif want_value:
            roles.append("value")
            want_value = False
        else:
            roles.append("positional")
    return roles


def expand_hook_glob_args(task, tokens):
    """把钩子参数里唯一的通配路径展开成实际路径（相对 task cwd）。

    钩子不经 shell，`*` 不会被自动展开，所以由这里显式展开。返回 (参数列表, 信息 dict)；
    信息里 `template` / `idx` / `paths` 供逐条调用使用，`error` 表示应当整单失败。

    安全约束（本地文件同等权限下只能「不扩大爆炸半径」）：
      * 只有含 `* ? [` 且角色是 `positional` 的 token 才展开，flag 与其值一律不动；
      * 出现第二个 positional 时报 hook_glob_ambiguous —— 通配与手写路径混用含义不明，不猜；
      * 匹配 0 个报 hook_glob_no_match —— 绝不把字面量 `*.log` 当文件名交给转换器；
      * 匹配数超过上限（默认 256，`ATS_HOOK_GLOB_MAX` 可调）报 hook_glob_too_many；
      * 跳过符号链接 / junction，避免顺着链接改到 cwd 之外；
      * 结果排序后返回，同一任务重复执行时展开顺序一致。
    """
    info = {"glob": None, "idx": None, "template": tokens, "paths": [],
            "expanded": 0, "skipped_symlinks": 0}
    roles = _hook_arg_roles(tokens)
    hits = [i for i, (tok, role) in enumerate(zip(tokens, roles))
            if role == "positional" and any(ch in tok for ch in HOOK_GLOB_CHARS)]
    if not hits:
        return tokens, info
    if len(hits) > 1:
        info["error"] = "hook_glob_ambiguous"
        info["detail"] = "钩子里只能有一个通配路径，实际 %d 个" % len(hits)
        return tokens, info
    idx = hits[0]
    extra = [i for i, role in enumerate(roles) if role == "positional" and i != idx]
    if extra:
        info["error"] = "hook_glob_ambiguous"
        info["detail"] = "通配路径不能与手写路径同时出现（第 %d 个参数）" % (extra[0] + 1)
        return tokens, info

    pattern = tokens[idx]
    try:
        limit = int(os.environ.get("ATS_HOOK_GLOB_MAX") or DEFAULT_HOOK_GLOB_MAX)
    except (TypeError, ValueError):
        limit = DEFAULT_HOOK_GLOB_MAX
    if limit <= 0:
        limit = DEFAULT_HOOK_GLOB_MAX

    base = Path(task.get("cwd") or os.getcwd())
    full = pattern if os.path.isabs(pattern) else str(base / pattern)
    matches, skipped, overflow = [], 0, False
    for hit in glob.iglob(full, recursive=True):
        p = Path(hit)
        if p.is_symlink():
            skipped += 1
            continue
        matches.append(hit)
        if len(matches) > limit:
            overflow = True
            break

    info["glob"] = pattern
    info["skipped_symlinks"] = skipped
    if overflow:
        info["error"] = "hook_glob_too_many"
        info["detail"] = "通配 %s 匹配超过上限 %d 个，请缩小范围或用 ATS_HOOK_GLOB_MAX 调整" % (pattern, limit)
        return tokens, info
    if not matches:
        info["error"] = "hook_glob_no_match"
        info["detail"] = "通配 %s 在 %s 下没有匹配到任何文件" % (pattern, base)
        return tokens, info

    matches.sort()
    info["idx"] = idx
    info["template"] = tokens[:idx] + tokens[idx + 1:]
    info["paths"] = matches
    info["expanded"] = len(matches)
    return info["template"][:idx] + matches + info["template"][idx:], info


def run_post_hook(root, tdir, task, hook):
    """主命令成功后的 @enc 转换步骤；结果写 hook-enc.log 并发事件。

    钩子参数里的通配符在这里展开（钩子不经 shell，`*` 不会自动展开）；
    展开清单写进 hook-enc.log 头部，事后能核对「这次到底动了哪些文件」。
    转换器的契约是单个位置路径，所以展开出多个目标时逐条调用、遇非 0 即停。
    """
    started = now_ts()
    log_path = tdir / "hook-enc.log"
    encoder = resolve_encoder(task)
    tokens, ginfo = expand_hook_glob_args(task, split_hook_args(hook.get("args") or ""))
    result = {"type": "enc", "args": hook.get("args"), "status": "failed",
              "exit_code": None, "log": str(log_path), "started_at": started}
    if ginfo.get("glob"):
        result["glob"] = ginfo["glob"]
        result["expanded"] = ginfo.get("expanded") or 0
    if ginfo.get("skipped_symlinks"):
        result["skipped_symlinks"] = ginfo["skipped_symlinks"]

    if encoder is None:
        result["error_type"] = "encoder_not_found"
        try:
            log_path.write_text(
                "encoder_not_found: 未找到 tools/encoding_converter.py；"
                "可用环境变量 ATS_ENCODER_PATH 指定转换器路径\n", encoding="utf-8")
        except OSError:
            pass
    elif ginfo.get("error"):
        result["error_type"] = ginfo["error"]
        result["message"] = ginfo.get("detail") or ""
        try:
            log_path.write_text("%s: %s\n" % (ginfo["error"], ginfo.get("detail") or ""),
                                encoding="utf-8")
        except OSError:
            pass
    else:
        targets = ginfo.get("paths") or [None]
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (task.get("env") or {}).items()})
        timeout = int(task.get("timeout") or 0)
        runs, code = [], 0
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as fh:
                if ginfo.get("glob"):
                    fh.write("glob %s -> %d path(s)\n" % (ginfo["glob"], len(targets)))
                    for path in targets:
                        fh.write("  %s\n" % path)
                    if ginfo.get("skipped_symlinks"):
                        fh.write("  (跳过符号链接 %d 个)\n" % ginfo["skipped_symlinks"])
                    fh.flush()
                for i, target in enumerate(targets, 1):
                    if target is None:
                        cmdline = [sys.executable, str(encoder)] + tokens
                    else:
                        cmdline = ([sys.executable, str(encoder)]
                                   + ginfo["template"][:ginfo["idx"]] + [target]
                                   + ginfo["template"][ginfo["idx"]:])
                        if len(targets) > 1:
                            fh.write("--- run %d/%d: %s\n" % (i, len(targets), target))
                            fh.flush()
                    proc = subprocess.Popen(cmdline, cwd=task.get("cwd") or None, env=env,
                                            stdin=subprocess.DEVNULL, shell=False,
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
                    runs.append({"path": target, "exit_code": code})
                    if code != 0:
                        result["failed_on"] = target
                        break
            result["exit_code"] = code
            result["status"] = "ok" if code == 0 else "failed"
            if len(targets) > 1:
                result["runs"] = runs
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

    level = pack_level(args.level, [hook_gate_text(c, hook) for c, hook in parsed])
    if level not in LEVEL_RANK:
        return print_json({"error": "bad_level", "level": level,
                           "allowed": sorted(LEVEL_RANK),
                           "hint": "等级只接受 L0/L1/L2/L3；--level 从 allowed 里挑一个"},
                          EXIT_USAGE)
    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT
    try:
        timeout = int(timeout or 0)
    except (TypeError, ValueError):
        return print_json({"error": "bad_timeout",
                           "hint": "超时必须是整数秒：用 --timeout 600；省略则取默认值"},
                          EXIT_USAGE)
    cwd = args.cwd
    if cwd:
        cwd = str(Path(cwd).expanduser())
        if not Path(cwd).exists():
            return print_json({"error": "bad_cwd", "cwd": cwd,
                               "hint": "cwd 指向的目录不存在：先建目录，或改成已存在的绝对路径"},
                              EXIT_USAGE)

    pack_id = args.pack_id or ("pk-" + uuid.uuid4().hex[:6])
    total = len(items)
    results = []
    now = now_ts()
    for index, (command, hook) in enumerate(parsed, 1):
        task_id = new_task_id()
        task = {
            "id": task_id,
            "instance_id": uuid.uuid4().hex[:12],
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
        # 权限门与预检看的是「主命令 + @enc 参数」的完整原文：钩子参数虽然只以
        # argv 交给转换器，但它是用户写下的内容，审批要绑同一段文本才不会看漏。
        gate_text = hook_gate_text(task.get("command") or "", task.get("post_hook"))
        if task.get("status") not in ("pending", "failed", "timeout", "succeeded", "killed"):
            return {"error": "state_conflict", "task_id": task["id"],
                    "status": task.get("status")}, EXIT_CONFLICT
        if task.get("status") in ("failed", "timeout", "killed", "succeeded") and not force:
            return {"error": "rerun_requires_force", "task_id": task["id"],
                    "status": task.get("status")}, EXIT_CONFLICT

        gate = permission_gate(root, gate_text, approval,
                               {"required_level": task.get("level") or DEFAULT_TASK_LEVEL,
                                "source": "run", "task_id": task["id"],
                                "task_instance": task.get("instance_id"),
                                "task_created_at": task.get("created_at")})

        approved_rec = gate.get("approval") or {}
        human_override = gate["reason"] == "approval" and bool(approved_rec.get("override"))
        preflight = None
        if human_override:
            log_event(root, "preflight_override", task_id=task["id"],
                      approval_id=approved_rec.get("id"))
        elif gate["allowed"] or gate["reason"] == "approval_required":
            preflight = preflight_review(
                root, gate_text, task.get("level") or DEFAULT_TASK_LEVEL,
                {"source": "run", "task_id": task.get("id"),
                 "task_instance": task.get("instance_id"),
                 "task_created_at": task.get("created_at"),
                 "cwd": task.get("cwd"), "argv": bool(task.get("argv"))},
                gate, shell=bool(task.get("shell", True)))

        if preflight and preflight.get("verdict") == "deny_injection":
            allow_override = bool(policy(root).get("denied_injection_allow_override"))
            rec = create_approval(
                root, gate_text, gate["required_level"],
                {"source": "run", "task_id": task["id"],
                 "task_instance": task.get("instance_id"),
                 "task_created_at": task.get("created_at")},
                resume=("python %s run %s --approval <approval_id>"
                        % (Path(__file__).name, task["id"])),
                preflight=preflight,
                status="pending" if allow_override else "denied_injection")
            announce_approval(root, rec)
            log_event(root, "preflight_denied", task_id=task["id"], approval_id=rec["id"],
                      verdict=preflight.get("verdict"), reason=preflight.get("reason"),
                      tags=preflight.get("tags"), confidence=preflight.get("confidence"))
            emit_event(root, {"type": "preflight_denied", "task_id": task["id"],
                              "approval_id": rec["id"],
                              "verdict": preflight.get("verdict"),
                              "reason": preflight.get("reason"),
                              "tags": preflight.get("tags"),
                              "executed": False})
            return {"error": "denied_injection", "task_id": task["id"],
                    "level": gate["required_level"], "approval_id": rec["id"],
                    "override_allowed": allow_override,
                    "preflight": preflight, "executed": False}, EXIT_DENIED

        if not gate["allowed"]:
            payload = {"error": "approval_required" if gate["reason"] == "approval_required" else gate["reason"],
                       "task_id": task["id"], "level": gate["required_level"],
                       "effective_level": gate["effective_level"],
                       "actor_level": gate["actor_level"],
                       "approval_id": None, "approval_error": gate["approval_error"]}
            if preflight:
                payload["preflight"] = preflight
            if gate["reason"] == "approval_required":
                rec = create_approval(root, gate_text, gate["required_level"],
                                      {"source": "run", "task_id": task["id"],
                                       "task_instance": task.get("instance_id"),
                                       "task_created_at": task.get("created_at")},
                                      resume=("python %s run %s --approval <approval_id>"
                                              % (Path(__file__).name, task["id"])),
                                      preflight=preflight)
                announce_approval(root, rec)
                payload["approval_id"] = rec["id"]
                payload["approval"] = rec
                payload["resume"] = rec["resume"]
                log_event(root, "task_blocked", task_id=task["id"], reason="approval_required",
                          approval_id=rec["id"])
            else:
                log_event(root, "task_blocked", task_id=task["id"], reason=gate["reason"])
            return payload, EXIT_DENIED

        if preflight:
            log_event(root, "preflight_completed", task_id=task["id"],
                      verdict=preflight.get("verdict"), status=preflight.get("status"),
                      tags=preflight.get("tags"), confidence=preflight.get("confidence"),
                      duration_ms=preflight.get("duration_ms"))

        (tdir / "stdout.log").parent.mkdir(parents=True, exist_ok=True)
        (tdir / "stdout.log").write_text("", encoding="utf-8")
        (tdir / "stderr.log").write_text("", encoding="utf-8")
        for stale_file in ("exit_code", "pid"):
            try:
                (tdir / stale_file).unlink()
            except OSError:
                pass

        effective_approval = (approval
                              or (gate.get("lease") or {}).get("approval_id")
                              or (gate.get("approval_id") if gate.get("auto_approved") else None))
        task.update({
            "status": "starting",
            "started_at": now_ts(),
            "started_iso": iso(),
            "ended_at": None,
            "exit_code": None,
            "finalized": False,
            "approval_id": effective_approval,
            "pid": None,
            "supervisor_pid": None,
        })
        save_task(root, task)
    finally:
        release_run_lock(lock_path)
    emit_event(root, {"type": "task_start", "task_id": task["id"],
                      "level": task.get("level"), "command": task["command"],
                      "approval_id": effective_approval, "gate": gate["reason"]})
    log_event(root, "task_start", task_id=task["id"], level=task.get("level"),
              gate=gate["reason"], approval_id=effective_approval)

    if wait:
        _supervise(root, task["id"])
        task = load_task(root, task["id"])
        return {"task": task, "waited": True}, EXIT_OK

    log_path = tdir / "supervisor.log"
    # 必须显式带 --root：子进程 cwd 也是 root，缺省会把 ATS_ROOT 解析成 <root>/.ats
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
            # 独立会话/进程组：停止时 killpg 不会波及 ATS CLI 自己所在的那一组
            proc = subprocess.Popen(argv, cwd=str(root), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=fh, close_fds=True,
                                    start_new_session=True)
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
    env["ATS_SPAWN_CMD"] = subprocess.list2cmdline([str(x) for x in argv])
    env["ATS_SPAWN_CWD"] = str(cwd)
    script = (
        "$ErrorActionPreference='Stop';"
        "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine=$env:ATS_SPAWN_CMD;CurrentDirectory=$env:ATS_SPAWN_CWD};"
        "if($r.ReturnValue -eq 0){Write-Output ('ATS_PID=' + $r.ProcessId)}"
        "else{Write-Output ('ATS_RC=' + $r.ReturnValue)}"
    )
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=30, env=env,
                              creationflags=0x08000000)   # CREATE_NO_WINDOW
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r"ATS_PID=(\d+)", done.stdout or "")
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
    # 任务自己也起成独立会话/进程组：stop 的 killpg 才不会打到调用者那一组
    spawn_kw = {"start_new_session": True} if os.name != "nt" else {}
    with (tdir / "stdout.log").open("a", encoding="utf-8", errors="replace") as out, \
            (tdir / "stderr.log").open("a", encoding="utf-8", errors="replace") as err:
        if argv:
            # 原生 argv 任务（download）：不经过 shell，避免 Windows 路径引号问题
            proc = subprocess.Popen([str(x) for x in argv], cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    **spawn_kw)
        else:
            proc = subprocess.Popen(command, cwd=cwd, env=env, shell=shell,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    **spawn_kw)
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
DOWNLOAD_MAX_MANIFESTS = 64
MANIFEST_GLOB_CHARS = ("*", "?", "[")


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


def _expand_manifest_args(values):
    """把 `--file` 的通配展开成具体清单路径；返回 (paths, error)。

    `--file` 只用于**读**清单，读它不会写任何文件（真正的写入目标是 `--out`），
    所以这里不跳过符号链接——与 `@enc` 不同：那边的展开结果会被就地转码，
    跳过链接是为了不顺着链接改到 cwd 之外；这里读一个链接只是读它指向的清单。

    只展开含 `* ? [` 的值，字面路径原样保留（仍由 `_load_manifest` 报
    `manifest_not_found`，行为与旧版一致）；0 匹配报 `manifest_no_match`，
    绝不把字面量 `*.txt` 当成一个真实文件名；匹配数超过上限（默认 64，
    `ATS_DOWNLOAD_MANIFEST_MAX` 可调）报 `manifest_too_many`。
    """
    try:
        limit = int(os.environ.get("ATS_DOWNLOAD_MANIFEST_MAX") or DOWNLOAD_MAX_MANIFESTS)
    except (TypeError, ValueError):
        limit = DOWNLOAD_MAX_MANIFESTS
    if limit <= 0:
        limit = DOWNLOAD_MAX_MANIFESTS
    paths = []
    for value in values or []:
        raw = str(value)
        if not any(ch in raw for ch in MANIFEST_GLOB_CHARS):
            paths.append(raw)
            continue
        matches = sorted(glob.iglob(raw, recursive=True))
        if not matches:
            return None, "manifest_no_match:%s" % raw
        if len(matches) > limit:
            return None, "manifest_too_many:%s" % raw
        paths.extend(matches)
    return paths, None


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
    manifests, err = _expand_manifest_args(args.file)
    if err:
        return None, err
    manifest_outputs = None
    for path in manifests:
        m_outputs, m_items, err = _load_manifest(path)
        if err:
            return None, "%s:%s" % (err, path)
        if m_outputs and not args.out:
            resolved = str(Path(m_outputs).expanduser().resolve())
            if manifest_outputs is None:
                manifest_outputs = resolved
            elif resolved != manifest_outputs:
                return None, "manifest_outputs_conflict:%s" % path
        for row in m_items:
            if not isinstance(row, dict) or not row.get("url"):
                return None, "item_missing_url:%s" % path
            item = {"url": str(row["url"]), "save_as": row.get("save_as") or None}
            for key in ("sha256", "size", "headers"):
                if row.get(key):
                    item[key] = row[key]
            items.append(item)
    outputs = outputs or manifest_outputs
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
    if manifests:
        spec["manifests"] = manifests
    return spec, None


def cmd_download(root, args):
    """创建下载任务并立刻交给 run 链路（权限/审批/状态/日志全部复用）。"""
    spec, err = _build_download_spec(args)
    if err:
        return print_json({"error": err,
                           "hint": "download 参数不合法：对照 ats.py download --help "
                                   "检查 --url / --file / --out 的必填关系"},
                          EXIT_USAGE)

    task_id = args.id or ("dl-" + uuid.uuid4().hex[:6])
    tdir = task_dir(root, task_id)
    if (tdir / "task.json").exists():
        return print_json({"error": "task_exists", "task_id": task_id,
                           "hint": "换一个 --id，或先 ats.py show <task_id> 确认是不是要复用的任务"},
                          EXIT_CONFLICT)
    tdir.mkdir(parents=True, exist_ok=True)
    spec_path = tdir / "download.json"
    jwrite(spec_path, spec)

    level = str(args.level or DEFAULT_TASK_LEVEL).upper()
    command = "download %d item(s) -> %s" % (len(spec["items"]), spec["outputs"])
    if spec.get("manifests"):
        command = "download %d item(s) from %d manifest(s) -> %s" % (
            len(spec["items"]), len(spec["manifests"]), spec["outputs"])
    now = now_ts()
    task = {
        "id": task_id,
        "instance_id": uuid.uuid4().hex[:12],
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
                 str(Path(__file__).resolve().parent / "ats_download.py"),
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
                           "manifests": len(spec.get("manifests") or []),
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
        from ats_torrent import find_aria2c
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
            return print_json({"error": "torrent_not_found", "path": str(torrent),
                               "hint": "--torrent 要指向存在的 .torrent 文件；磁力链改用 --magnet"},
                              EXIT_USAGE)
        source = {"type": "torrent", "path": str(torrent)}

    aria2 = _resolve_aria2()
    if not aria2:
        return print_json({"error": "aria2_not_found",
                           "hint": "winget install --id aria2.aria2 -e，"
                                   "或设置 ATS_ARIA2 指向 aria2c.exe"}, EXIT_USAGE)

    task_id = args.id or ("tor-" + uuid.uuid4().hex[:6])
    tdir = task_dir(root, task_id)
    if (tdir / "task.json").exists():
        return print_json({"error": "task_exists", "task_id": task_id,
                           "hint": "换一个 --id，或先 ats.py show <task_id> 确认是不是要复用的任务"},
                          EXIT_CONFLICT)
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
        "instance_id": uuid.uuid4().hex[:12],
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
                 str(Path(__file__).resolve().parent / "ats_torrent.py"),
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


def purge_task_approvals(root, task_id):
    """任务被删除后清理指向它的待决审批。

    留着就是悬空引用：任务没了但待办还在，`approve --all` 会把它一并批准，
    而这条审批的 command 只是「delete <id>」，一旦 id 被回收复用就会指向新任务。
    已决议记录保留，作为历史留痕。
    """
    removed = []
    for p in sorted(approvals_dir(root).glob("ap-*.json")):
        rec = jread(p, {})
        if not isinstance(rec, dict):
            continue
        if rec.get("task_id") != task_id or rec.get("status") != "pending":
            continue
        try:
            p.unlink()
            removed.append(rec.get("id"))
        except OSError:
            pass
    if removed:
        log_event(root, "approval_purged", task_id=task_id, approvals=removed)
    return removed


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
    gate = gate_operation(root, operation_level(policy(root), "stop", "L2"), label,
                          args.approval,
                          {"task_id": task["id"], "source": "stop",
                           "task_instance": task.get("instance_id"),
                           "task_created_at": task.get("created_at"),
                           "resume": "python %s stop %s --approval <approval_id>"
                                     % (Path(__file__).name, task["id"])})
    if not gate["allowed"]:
        return print_json(gate, EXIT_DENIED)

    leftovers, checked = stop_process_tree(task)
    if leftovers:
        # 还有进程活着：不要谎报 killed，任务留在 running 让调用方重试或改配置
        log_event(root, "task_stop", task_id=task["id"], gate=gate.get("reason"),
                  leftovers=leftovers)
        return print_json({"task": task, "stopped": False,
                           "checked": checked, "leftovers": leftovers}, EXIT_CONFLICT)
    finalize_task(root, task, "killed", task.get("exit_code"), note="stopped_by_user")
    log_event(root, "task_stop", task_id=task["id"], gate=gate.get("reason"))
    return print_json({"task": task, "stopped": True,
                       "checked": checked, "leftovers": []})


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
    gate = gate_operation(root, operation_level(policy(root), "delete", "L3"), label,
                          args.approval,
                          {"task_id": task["id"], "source": "delete",
                           "task_instance": task.get("instance_id"),
                           "task_created_at": task.get("created_at"),
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
    purged = purge_task_approvals(root, task["id"])
    log_event(root, "task_deleted", task_id=task["id"], gate=gate.get("reason"))
    return print_json({"task_id": task["id"], "deleted": True,
                       "purged_approvals": purged})


def cmd_approve(root, args):
    auth = require_passphrase(root)
    if not auth.get("ok"):
        return print_json({"error": "passphrase_required", "detail": auth,
                           "hint": "批准/拒绝审批需要人工口令：用 --passphrase-stdin "
                                   "从标准输入提供一行"}, EXIT_DENIED)
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

        pf = record.get("preflight") or {}
        if pf.get("verdict") == "deny_injection" and not args.reject:
            if not bool(pol.get("denied_injection_allow_override")):
                results.append({"approval_id": approval_id, "ok": False,
                                "error": "injection_denied",
                                "note": "denied_injection 已按策略禁止人工放行"})
                continue
            if not getattr(args, "override", False):
                results.append({"approval_id": approval_id, "ok": False,
                                "error": "override_required",
                                "note": "denied_injection 放行需要 --override 与 --note"})
                continue
            if not (args.note or "").strip():
                results.append({"approval_id": approval_id, "ok": False,
                                "error": "note_required",
                                "note": "强制放行必须填写备注"})
                continue

        status = "rejected" if args.reject else "approved"
        record["status"] = status
        record["override"] = bool(pf.get("verdict") == "deny_injection" and not args.reject)
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
            "override": record.get("override"),
            "resume": record.get("resume"),
        }
        sid = record.get("schedule_id")
        target = schedule_event_stream(root, sid) if sid else event_stream(root)
        append_event_line(target, event)
        events.append(event)
        log_event(root, "approval_resolved", approval_id=approval_id, status=status,
                  required_level=record.get("required_level"),
                  schedule_id=record.get("schedule_id"), task_id=record.get("task_id"),
                  resolved_by=record["resolved_by"], override=record.get("override"))
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
    changes = dict(args.set or [])
    restricted = sorted(k for k in changes if k in RESTRICTED_POLICY_KEYS)
    if changes:
        # 任何 --set 都要口令：写盘后必须重封签，而重封签需要口令。
        # 先把「会放宽执行许可」的键点出来，让人知道这次改动的风险面。
        auth = require_passphrase(root)
        if not auth.get("ok"):
            return print_json({"error": "passphrase_required", "restricted": restricted,
                               "detail": auth,
                               "hint": "改策略会重建信任基线（重封签），必须持口令："
                                       "用 --passphrase-stdin 从标准输入提供一行"},
                              EXIT_DENIED)
        unknown = sorted(k for k in changes if k not in POLICY_DEFAULTS)
        if unknown:
            return print_json({"error": "unknown_keys", "keys": unknown,
                               "allowed": sorted(POLICY_DEFAULTS),
                               "hint": "只接受 allowed 里的键：ats.py policy --set <KEY> <VALUE>；"
                                       "未知键不会写进 policy.json"},
                              EXIT_USAGE)
        path = Path(root) / "policy.json"
        stored = jread(path, {})
        if not isinstance(stored, dict):
            stored = {}
        for key, raw in changes.items():
            if key in BOOL_POLICY_KEYS:
                stored[key] = as_bool(raw)
            elif key in INT_POLICY_KEYS:
                try:
                    stored[key] = int(raw)
                except (TypeError, ValueError):
                    return print_json({"error": "bad_value", "key": key,
                                       "value": raw,
                                       "hint": "该键需要整数：ats.py policy --set <KEY> <整数>"},
                                      EXIT_USAGE)
            elif key == "approval_mode_by_level":
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    return print_json({"error": "bad_value", "key": key,
                                       "value": raw,
                                       "hint": "该键要 JSON 对象，如 "
                                               'ats.py policy --set approval_mode_by_level '
                                               '{"L2": "ask"}'},
                                      EXIT_USAGE)
                if not isinstance(parsed, dict):
                    return print_json({"error": "bad_value", "key": key,
                                       "value": raw,
                                       "hint": "该键要 JSON 对象（形如 {\"L2\": \"ask\"}），"
                                               "不能是字符串或数组"},
                                      EXIT_USAGE)
                stored[key] = parsed
            else:
                stored[key] = raw
        jwrite(path, stored)
        log_event(root, "policy_updated", keys=sorted(changes),
                  restricted=restricted or None)
        # 写完立刻重封签，否则下次启动会因 policy.json 摘要不符被看门狗拦住
        rc, sealed = _run_guard(["seal", "--confirm"], root, GUARD_PASSPHRASE)
        if rc != EXIT_OK or not sealed.get("ok"):
            return print_json({"error": "reseal_failed", "detail": sealed,
                               "hint": "policy.json 已写入但重封签失败，下次启动会被看门狗"
                                       "拒绝；请人工执行 ats_guard.py seal --confirm"},
                              EXIT_DENIED)
        _freeze_policy(root, force=True)
    pol = policy(root)
    record, state = lease_state(root, pol)
    pol["lease"] = {
        "path": str(lease_path(root, pol)),
        "state": state,
        "token": (record or {}).get("token"),
        "pid": (record or {}).get("pid"),
        "heartbeat_at": (record or {}).get("heartbeat_at"),
        "scope": (record or {}).get("scope"),
        "ats_autonomy": (record or {}).get("ats_autonomy"),
    }
    if args.json:
        return print_json(pol)
    print(json.dumps(pol, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_extend(root, args):
    """扩展插件入口：list / help / check 只读，run 走普通 Task 链路真执行。"""
    if ats_extend is None:
        return print_json({"error": "extend_module_missing",
                           "hint": "scripts/ats_extend.py 缺失或无法导入，扩展发现层不可用"},
                          EXIT_USAGE)
    if args.action == "list":
        data = ats_extend.scan(root)
        if args.json:
            return print_json(data)
        for line in ats_extend.render_table(data["extends"]):
            print(line)
        if not data["extends"]:
            print("no extends in %s" % data["extend_dir"])
        if data["skipped_symlinks"]:
            print("skipped %d symlinked director%s"
                  % (data["skipped_symlinks"],
                     "y" if data["skipped_symlinks"] == 1 else "ies"))
        return EXIT_OK
    if args.action == "check":
        return cmd_extend_check(root, args)
    if args.action == "run":
        return cmd_extend_run(root, args)
    item = ats_extend.describe(root, args.extend_id)
    if item is None:
        return print_json({"error": "not_found", "extend_id": args.extend_id,
                           "hint": "先运行 extend list 查看可用插件"},
                          EXIT_CONFLICT)
    if args.json:
        return print_json({"extend": item})
    for line in ats_extend.render_help(item):
        print(line)
    return EXIT_OK


def cmd_extend_check(root, args):
    """扩展执行的纯预演：算等级、过权限门、跑静态预检，但绝不执行插件。

    这是「执行前先过审」的准备层：第 3 批接执行时直接复用这里算出的
    gate_text 与 required_level，保证审批绑定与预演结果同源。

    三个刻意的边界：
      * 权限门用 gate_preview 而不是 permission_gate —— 后者命中租约会写审批；
      * 预检用 static_review 而不是 review —— 后者在 preflight_dry_run 开启时
        会在沙箱里真跑命令，那就是执行插件代码；
      * 输出里恒有 executed=false，别让调用方把 would=allow 当成已跑过。
    """
    item = ats_extend.describe(root, args.extend_id)
    if item is None:
        return print_json({"error": "not_found", "extend_id": args.extend_id,
                           "hint": "先运行 extend list 查看可用插件"},
                          EXIT_CONFLICT)
    action, argv, bad = ats_extend.check_args(args.action_name, args.arg)
    if bad:
        return print_json({"error": bad["code"], "extend_id": args.extend_id,
                           "detail": bad["detail"], "executed": False,
                           "hint": "按 detail 修参数后重试；参数不能含双引号或换行、"
                                   "单条不超过 1024 字符，指令名只允许字母数字_-"},
                          EXIT_USAGE)
    if item["state"] != "active":
        return print_json({
            "extend_id": args.extend_id,
            "state": item["state"],
            "reason": item["reason"],
            "detail": item["detail"],
            "problems": item["problems"],
            "would": "deny",
            "error": "inactive_extend",
            "executed": False,
            "hint": "非 active 插件不参与执行；按 problems 修好后再 check",
        }, EXIT_DENIED)
    commands = item.get("commands") or {}
    if action is not None and action not in commands:
        return print_json({
            "error": "unknown_action",
            "extend_id": args.extend_id,
            "action": action,
            "known": sorted(commands),
            "executed": False,
            "hint": "指令名取 instruction.json 的 commands 键",
        }, EXIT_USAGE)
    text = ats_extend.gate_text(item, action, argv)
    level, level_note = ats_extend.task_level(item)
    gate = gate_preview(root, text, level, {"whitelist_text": text})
    preflight = None
    if ats_preflight is not None:
        try:
            preflight = ats_preflight.static_review(text, {"cwd": item["dir"]})
        except Exception as exc:         # noqa: BLE001
            preflight = {"error": "%s: %s" % (type(exc).__name__, exc),
                         "tags": [], "confidence": "none"}
    payload = {
        "extend_id": args.extend_id,
        "dis_name": item.get("dis_name"),
        "state": item["state"],
        "executed": False,
        "action": action,
        "args": argv,
        "gate_text": text,
        "level": level,
        "level_note": level_note,
        "gate": gate,
        "preflight": preflight,
        "warnings": item.get("warnings") or [],
    }
    if args.json:
        return print_json(payload)
    for line in ats_extend.render_check(payload):
        print(line)
    return EXIT_OK


def cmd_extend_run(root, args):
    """按插件声明的入口建 Task 并启动（argv 模式，不经 shell）。

    与 extend check 只差最后一步：check 算完就返回，run 把同一份
    gate_text / argv / level 交给 _start_task，走既有的权限门、预检、
    审批、supervisor、日志链路。扩展层不自建第二套执行器 —— 否则审批绑定、
    编码解码、失败收敛这些既有行为都要重新实现一遍，且必然与主体漂移。

    三个必须守住的点：
      * command 必须等于 gate_text：_start_task 用 task["command"] 做审批绑定
        与预检，换成任何别的写法都会让「预演看到的」和「执行的」不一致；
      * shell=False + argv：参数不经 shell 解释，空格与 `&`、`;` 只是普通字符；
      * cwd 设为插件目录：插件按相对路径读自己的资源。它也能用 `../..` 读到
        root，这属于执行期的沙箱边界，静态预检只能看常量路径，看不到运行时拼接。
    """
    item = ats_extend.describe(root, args.extend_id)
    if item is None:
        return print_json({"error": "not_found", "extend_id": args.extend_id,
                           "hint": "先运行 extend list 查看可用插件"}, EXIT_CONFLICT)
    action, argv_args, bad = ats_extend.check_args(args.action_name, args.arg)
    if bad:
        return print_json({"error": bad["code"], "extend_id": args.extend_id,
                           "detail": bad["detail"], "executed": False,
                           "hint": "按 detail 修参数后重试；参数不能含双引号或换行、"
                                   "单条不超过 1024 字符，指令名只允许字母数字_-"},
                          EXIT_USAGE)
    if item["state"] != "active":
        return print_json({
            "extend_id": args.extend_id, "state": item["state"],
            "reason": item["reason"], "detail": item["detail"],
            "problems": item["problems"], "error": "inactive_extend",
            "executed": False,
            "hint": "非 active 插件不参与执行；按 problems 修好后再 run",
        }, EXIT_DENIED)
    commands = item.get("commands") or {}
    if action is not None and action not in commands:
        return print_json({"error": "unknown_action", "extend_id": args.extend_id,
                           "action": action, "known": sorted(commands),
                           "executed": False,
                           "hint": "用 ats.py extend help <extend_id> 看可用指令，"
                                   "known 是当前清单"},
                          EXIT_USAGE)
    argv = ats_extend.extend_argv(item, action, argv_args)
    text = ats_extend.gate_text(item, action, argv_args)
    if argv is None or text is None:
        return print_json({"error": "missing_entry_point", "extend_id": args.extend_id,
                           "executed": False,
                           "hint": "入口缺失或越界：跑 ats.py extend check <extend_id> 看 problems"},
                          EXIT_CONFLICT)
    level, level_note = ats_extend.task_level(item)

    task_id = args.id or new_task_id()
    tdir = task_dir(root, task_id)
    if (tdir / "task.json").exists() and not args.force:
        return print_json({"error": "task_exists", "task_id": task_id,
                           "hint": "换一个 --id，或对已存在的任务用 ats.py run <task_id>；"
                                   "确认要覆盖加 --force"},
                          EXIT_CONFLICT)

    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT
    try:
        timeout = int(timeout or 0)
    except (TypeError, ValueError):
        return print_json({"error": "bad_timeout",
                           "hint": "超时必须是整数秒：--timeout 600（0 为不限）"},
                          EXIT_USAGE)

    now = now_ts()
    task = {
        "id": task_id,
        "instance_id": uuid.uuid4().hex[:12],
        "command": text,
        "argv": argv,
        "cwd": item["dir"],
        "level": level,
        "timeout": timeout,
        "env": {},
        "shell": False,
        "description": "extend %s%s" % (item["extend_id"],
                                        "" if not action else ":" + action),
        "tags": ["extend", item["extend_id"]],
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
        "extend": {"extend_id": item["extend_id"], "action": action,
                   "level": level, "level_note": level_note},
    }
    tdir.mkdir(parents=True, exist_ok=True)
    save_task(root, task)
    emit_event(root, {"type": "task_created", "task_id": task_id, "level": level,
                      "command": text, "kind": "extend",
                      "extend_id": item["extend_id"]})
    log_event(root, "task_created", task_id=task_id, level=level,
              command=text, kind="extend", extend_id=item["extend_id"])

    payload, code = _start_task(root, task_id, args.approval, args.wait, False)
    payload["extend"] = {"extend_id": item["extend_id"], "action": action,
                         "level": level, "level_note": level_note,
                         "gate_text": text}
    return print_json(payload, code)


# ---------------------------------------------------------------- 看门狗准入

def _sha256_file(path):
    """流式计算 sha256；文件不存在或读失败返回 None（与 ats_guard.py 同语义）。"""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        digest = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _guard_manifest_path(root):
    """清单位置与 ats_guard.py 保持一致：ATS_GUARD_MANIFEST 优先，否则 <root>/清单名。"""
    env = os.environ.get(GUARD_MANIFEST_ENV)
    if env:
        return Path(env)
    return Path(root) / GUARD_MANIFEST


def _read_guard_manifest(root):
    try:
        data = json.loads(_guard_manifest_path(root).read_text(encoding="utf-8"))
    except Exception:                    # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _extract_guard_flags(argv):
    """剥离看门狗全局参数，返回 (skip_guard, guard_approval, visa_id, passphrase, 剩余 argv)。

    必须在 argparse 之前调用：--skip-guard / --guard-approval / --visa-id /
    --passphrase-stdin 是跨子命令的全局开关，交给 argparse 会被子解析器当成未知参数。
    口令只从 stdin 读，绝不进 argv（argv 会留在进程列表与 shell 历史里）。
    """
    skip_guard = False
    guard_approval = None
    visa_id = None
    passphrase_stdin = False
    rest = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--skip-guard":
            skip_guard = True
            i += 1
            continue
        if token == "--guard-approval":
            if i + 1 < len(argv):
                guard_approval = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if token.startswith("--guard-approval="):
            guard_approval = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--visa-id":
            if i + 1 < len(argv):
                visa_id = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if token.startswith("--visa-id="):
            visa_id = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--passphrase-stdin":
            passphrase_stdin = True
            i += 1
            continue
        rest.append(token)
        i += 1
    if visa_id is None:
        visa_id = (os.environ.get(GUARD_VISA_ENV) or "").strip() or None
    passphrase = ""
    if passphrase_stdin:
        try:
            passphrase = (sys.stdin.readline() or "").rstrip("\r\n")
        except Exception:                # noqa: BLE001
            passphrase = ""
    return skip_guard, guard_approval, visa_id, passphrase, rest


def _pre_scan_root(argv):
    """在 argparse 之前裸扫 --root：握手要用 root，而解析失败不能先炸掉准入。"""
    for i, token in enumerate(argv):
        if token == "--root" and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith("--root="):
            return token.split("=", 1)[1]
    return None


def _run_guard(args, root, passphrase=None):
    """跑一次 ats_guard.py 子命令，返回 (退出码, 解析后的 JSON)。输出不可解析一律算失败。"""
    script = Path(__file__).resolve().parent / GUARD_FILE
    cmd = [sys.executable, str(script), "--root", str(root)] + [str(a) for a in args]
    stdin = None
    if passphrase is not None:
        cmd.append("--passphrase-stdin")
        stdin = passphrase + "\n"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return EXIT_DENIED, {"ok": False, "error": "guard_spawn_failed",
                             "detail": "%s: %s" % (type(exc).__name__, exc)}
    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return EXIT_DENIED, {"ok": False, "error": "guard_output_invalid",
                             "exit_code": proc.returncode,
                             "stdout": (proc.stdout or "")[:400],
                             "stderr": (proc.stderr or "")[:400]}
    return proc.returncode, payload if isinstance(payload, dict) else {
        "ok": False, "error": "guard_output_invalid"}


def require_passphrase(root):
    """校验启动时从 stdin 读到的口令；缺失/错误都返回 fail-closed 的载荷。"""
    if not GUARD_PASSPHRASE:
        return {"ok": False, "error": "passphrase_missing",
                "hint": "口令不能为空：用 --passphrase-stdin 从标准输入提供一行，"
                        "或直接在终端里交互输入"}
    rc, payload = _run_guard(["auth"], root, GUARD_PASSPHRASE)
    return payload


def _hold_handles():
    """运行期持有核心脚本的只读句柄。

    Windows 上 Python 的 open() 不带 FILE_SHARE_DELETE，因此持有句柄期间
    删除与 rename 替换这些文件会失败（实测 PermissionError），读取不受影响。
    但**原地覆写内容不被阻止** —— 共享写是允许的；这条只能靠「后续新启动的
    进程都要过 verify」兜住，别把它当成运行期内容不可改。句柄由进程生命周期
    兜底释放。
    """
    if GUARD_HANDLES:
        return
    here = Path(__file__).resolve().parent
    for name in sorted(p.name for p in here.glob("*.py")):
        try:
            GUARD_HANDLES.append((here / name).open("rb"))
        except OSError:
            continue


def _freeze_policy(root, force=False):
    """握手成功后冻结策略快照：运行中原地改写 policy.json 不影响本次决策。"""
    global GUARD_POLICY_SNAPSHOT
    key = _policy_root_key(root)
    if not force and GUARD_POLICY_SNAPSHOT and GUARD_POLICY_SNAPSHOT.get("root") == key:
        return
    saved = GUARD_POLICY_SNAPSHOT
    GUARD_POLICY_SNAPSHOT = None
    try:
        data = policy(root)
    finally:
        GUARD_POLICY_SNAPSHOT = saved
    GUARD_POLICY_SNAPSHOT = {"root": key, "data": data}


def _guard_self_check(root):
    """先自行比对 ats_guard.py 的摘要，再决定要不要把信任交给它。

    清单缺失时不在这里判失败 —— 交给 verify 报 manifest_missing，避免两处口径不一。
    """
    manifest = _read_guard_manifest(root)
    if manifest is None:
        return None
    expected = (manifest.get("files") or {}).get(GUARD_FILE)
    if not expected:
        return None
    actual = _sha256_file(Path(__file__).resolve().parent / GUARD_FILE)
    if actual == expected:
        return None
    return {"code": EXIT_DENIED,
            "payload": {"error": "guard_check_failed",
                        "detail": {"stage": "guard_self_check",
                                   "file": GUARD_FILE,
                                   "expected": expected,
                                   "actual": actual,
                                   "manifest": str(_guard_manifest_path(root)),
                                   "hint": "看门狗自身与清单不符，它输出的「校验通过」"
                                           "不可信。请用 git 历史逐行比对 "
                                           "ats_guard.py，确认改动来源后由人执行 "
                                           "seal --confirm 重封签"}}}


def _guard_skip_gate(root, approval_id):
    """--skip-guard 的审批闸门：只认绑定 __ats_guard_skip__ 的已批准 L3 记录。"""
    level = normalize_floor_level(policy(root).get("guard_skip_level"), GUARD_SKIP_LEVEL)
    check = approval_check(root, approval_id, GUARD_SKIP_COMMAND)
    record = check["record"]
    if check["ok"] and level_rank(record.get("required_level")) >= level_rank(level):
        log_event(root, "guard_skip_approved", approval_id=record["id"],
                  required_level=record.get("required_level"), level=level)
        return None
    if check["ok"]:
        # 审批等级不足：低等级审批不能用来跳过看门狗
        check = {"ok": False, "error": "insufficient_level", "record": record}
    new_record = create_approval(
        root, GUARD_SKIP_COMMAND, level,
        context={"source": "guard_skip", "guard_skip": True},
        resume="python %s <command> --skip-guard --guard-approval <approval_id>"
               % Path(__file__).name)
    announce_approval(root, new_record)
    log_event(root, "guard_skip_blocked", approval_id=new_record["id"],
              approval_error=check["error"], required_level=level)
    return {"code": EXIT_DENIED,
            "payload": {"error": "guard_skip_approval_required",
                        "required_level": level,
                        "approval_id": new_record["id"],
                        "approval": new_record,
                        "approval_error": check["error"],
                        "resume": new_record.get("resume"),
                        "hint": "--skip-guard 只供人类紧急调试，必须持 L3 审批："
                                "先 approve 该 approval_id，再带 --guard-approval 重跑；"
                                "跳过看门狗不会降低任何命令的执行等级"}}


def _visa_scope(payload):
    """判定这次 verify 失败是否落在签证覆盖范围内，返回 (是否覆盖, 说明)。

    只认核心 .py 的 digest_mismatch：清单缺失、清单版本不符、空清单、
    policy.json / 密钥文件被动过、新增未封签文件，全都超出签证语义
    —— 签证买的是「代码改了还没重封签」，不是「整条信任链都可以不一致」。
    """
    if payload.get("error"):
        return False, "verify_error:%s" % payload.get("error")
    problems = payload.get("problems")
    if not isinstance(problems, list) or not problems:
        return False, "no_problems"
    files = []
    for item in problems:
        if not isinstance(item, dict):
            return False, "malformed_problem"
        if item.get("error") != "digest_mismatch":
            return False, "problem:%s" % item.get("error")
        name = item.get("file")
        if not isinstance(name, str) or not name.endswith(".py"):
            return False, "problem_file:%s" % name
        files.append(name)
    return True, files


def _guard_visa_gate(root, visa_id, verify_payload, files):
    """签证闸门：核心 .py 摘要不符时凭有效签证放行一次。

    放行不放权：签证只跳过完整性校验，审批门、命令等级、策略上限全都不受影响。
    扣减配额交给 ats_guard.py visa consume，那边带锁做读改写，扣到 0 当场删文件。
    调用方保证 visa_id 非空、且失败范围已由 _visa_scope 确认可覆盖。
    """
    rc, peek = _run_guard(["visa", "peek", "--id", visa_id], root)
    scope_type = peek.get("scope_type") if peek.get("ok") else None
    if scope_type in ("command", "plan"):
        # Task/Plan Visa 绑的是「某条命令 / 某个计划」，不覆盖核心脚本摘要不符。
        # 硬拿它过握手就是越权使用，一次配额都不扣，直接拒绝。
        log_event(root, "guard_visa_scope_refused", visa_id=visa_id,
                  scope_type=scope_type, files=files)
        return {"code": EXIT_DENIED,
                "payload": {"error": "visa_scope_not_applicable",
                            "detail": {"stage": "guard_visa", "visa_id": visa_id,
                                       "scope_type": scope_type, "files": files,
                                       "hint": "这张签证绑定的是具体任务/计划，不覆盖核心"
                                               "脚本摘要不符。要么 seal --confirm 重封签，"
                                               "要么由人签发一张 digest 签证"}}}
    rc, result = _run_guard(["visa", "consume", "--id", visa_id], root)
    if rc != EXIT_OK or not result.get("ok"):
        log_event(root, "guard_visa_rejected", visa_id=visa_id, exit_code=rc,
                  error=result.get("error"), reason=result.get("reason"))
        return {"code": EXIT_DENIED,
                "payload": {"error": "visa_invalid",
                            "detail": {"stage": "guard_visa",
                                       "visa_id": visa_id,
                                       "reason": result.get("reason"),
                                       "guard": result,
                                       "files": files,
                                       "verify": verify_payload,
                                       "hint": "签证不存在 / 已过期 / 配额耗尽，"
                                               "本次命令不执行。由人重新执行 "
                                               "ats_guard.py visa issue 签发新签证，"
                                               "或 seal --confirm 重封签"}}}
    if result.get("destroyed"):
        # 子进程的 stderr 被 _run_guard 捕获了，这里补一条到父进程控制台
        sys.stderr.write("[Visa] 配额已耗尽，签证已销毁，"
                         "后续执行请重新封签或申请新签证。\n")
    log_event(root, "guard_visa_used", visa_id=visa_id,
              remaining=result.get("remaining"), destroyed=result.get("destroyed"),
              files=files)
    return None


def _visa_task_gate(root, command, context, result):
    """approval_required 时的 Task / Plan Visa 闸门；不涉及签证一律返回 None。

    只接管 scope_type=command / plan 的签证。digest 签证是完整性校验专用，
    到这里原样交回人工审批 —— 老语义「签证不放宽审批门」不变。

    三道关：
      1) peek 只读预检：签证不存在/过期/是 digest → 交回原流程，不报错、不扣配额；
      2) consume 带作用域消费：命令哈希或 task/plan ID 对不上 → 直接 visa_mismatch
         拒绝，**不回落到审批**（跨任务使用一律不许）；
      3) 放行留痕：写 auto_approved 审批记录 + visa_auto_approved 事件。
    """
    visa_id = (context.get("visa_id") or GUARD_VISA_ID or "").strip()
    if not visa_id:
        return None
    rc, peek = _run_guard(["visa", "peek", "--id", visa_id], root)
    if rc != EXIT_OK or not peek.get("ok"):
        log_event(root, "visa_task_skipped", visa_id=visa_id,
                  reason=peek.get("reason") or peek.get("error"))
        return None
    if peek.get("scope_type") not in ("command", "plan"):
        return None
    scope = {"scope_hash": command_scope_hash(command),
             "task_id": str(context.get("task_id") or "").strip(),
             "plan_id": str(context.get("plan_id") or "").strip()}
    args = ["visa", "consume", "--id", visa_id, "--scope-hash", scope["scope_hash"]]
    if scope["task_id"]:
        args += ["--task-id", scope["task_id"]]
    if scope["plan_id"]:
        args += ["--plan-id", scope["plan_id"]]
    rc, consumed = _run_guard(args, root)
    if rc != EXIT_OK or not consumed.get("ok"):
        log_event(root, "visa_task_rejected", visa_id=visa_id, exit_code=rc,
                  error=consumed.get("error"), reason=consumed.get("reason"),
                  task_id=scope["task_id"], plan_id=scope["plan_id"],
                  scope_hash=scope["scope_hash"])
        denied = dict(result)
        denied.update(allowed=False, reason="visa_mismatch", visa=consumed,
                      visa_id=visa_id,
                      visa_scope_type=consumed.get("scope_type") or peek.get("scope_type"))
        return denied
    record = create_visa_approval(
        root, command, result.get("required_level"),
        {"visa_id": visa_id, "remaining": consumed.get("remaining")},
        {"scope_type": consumed.get("scope_type") or peek.get("scope_type"),
         "scope_hash": scope["scope_hash"]},
        context)
    result.update(allowed=True, reason="visa_auto", approval_id=record["id"],
                  approval=record, visa=consumed, auto_approved=True)
    return result


def guard_handshake(root, skip_guard=False, guard_approval=None, visa_id=None):
    """启动准入。返回 None 表示放行；否则返回 {"code", "payload"}。

    顺序：--skip-guard 先过 L3 审批闸门 → 自检看门狗摘要 → 跑 verify →
    （verify 仅因核心 .py 摘要不符失败时，凭签证放行一次并扣配额）→
    持只读句柄 → 冻结策略快照。任一步失败都 fail-closed。
    """
    global GUARD_SKIP_ACTIVE
    root = Path(root)
    if skip_guard:
        denied = _guard_skip_gate(root, guard_approval)
        if denied is not None:
            return denied
        GUARD_SKIP_ACTIVE = True
        log_event(root, "guard_skipped", level=GUARD_SKIP_LEVEL,
                  approval_id=guard_approval)
    else:
        denied = _guard_self_check(root)
        if denied is not None:
            log_event(root, "guard_self_check_failed", file=GUARD_FILE)
            return denied
        rc, payload = _run_guard(["verify"], root)
        if rc != EXIT_OK or not payload.get("ok"):
            log_event(root, "guard_verify_failed", exit_code=rc,
                      error=payload.get("error"))
            covered, scope = _visa_scope(payload)
            if covered and visa_id:
                denied = _guard_visa_gate(root, visa_id, payload, scope)
                if denied is None:
                    _hold_handles()
                    _freeze_policy(root)
                    return None
                return denied
            if covered:
                log_event(root, "guard_visa_absent", files=scope)
            else:
                log_event(root, "guard_visa_skipped", visa_id=visa_id, scope=scope)
            error = payload.get("error")
            if error == "manifest_missing" and payload.get("next_action") == "init":
                hint = ("本 root 尚未建立信任根，本次命令不执行。由人先执行 "
                        "ats_guard.py init --confirm 创建密钥，再执行 seal --confirm "
                        "建立基线；两步都要口令")
            elif error == "manifest_missing":
                hint = ("清单缺失但密钥仍在，本次命令不执行。由人执行 seal --confirm "
                        "重新封签，口令须与 init 时一致")
            elif payload.get("next_action") == "restore_key":
                hint = ("密钥文件丢失，本次命令不执行，seal 也修不好（seal 要口令校验，"
                        "口令校验要读密钥）。本仓的 ats_guard_key.json 与 "
                        "ats_guard_manifest.json 当前未被 git 跟踪，无法从 git 恢复；"
                        "确认要全新开始，须由人先手动删除这两个文件，"
                        "再重跑 init --confirm 与 seal --confirm")
            else:
                hint = ("核心脚本未通过看门狗校验，本次命令不执行。确认改动可信后由人执行 "
                        "seal --confirm 重封签")
            if covered:
                hint = (hint + "；若只是改了核心脚本、想先跑一轮测试，可带 --visa-id <id> "
                                "或设环境变量 ATS_VISA_ID 使用人类签发的签证，"
                                "每次握手消耗 1 次配额")
            return {"code": EXIT_DENIED,
                    "payload": {"error": "guard_check_failed",
                                "detail": {"stage": "guard_verify",
                                           "detail": payload,
                                           "manifest": str(_guard_manifest_path(root)),
                                           "next_action": payload.get("next_action"),
                                           "hint": hint}}}
    _hold_handles()
    _freeze_policy(root)
    return None


# ---------------------------------------------------------------- 输出与 CLI

def print_json(data, code=EXIT_OK):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return code


def build_parser():
    ap = argparse.ArgumentParser(
        prog="ats.py",
        description="agent-task-scheduler — Task 层执行器 (v3.3.0-R0)",
    )
    ap.add_argument("--root", default=None,
                    help="状态根目录，默认 <ats 本体目录>/.ats（即 OCSKIS/.ats）；"
                         "也可用环境变量 ATS_ROOT 指定")
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
    p.add_argument("--file", action="append", default=[],
                   help="清单文件（.json / .txt / .csv），可重复；支持通配（如 manifests/*.txt）")
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
    p.add_argument("--override", action="store_true",
                   help="denied_injection 强制放行（需策略允许 + --note）")

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

    p = sub.add_parser("policy", help="查看/修改有效策略")
    p.add_argument("--json", action="store_true")
    p.add_argument("--set", action="append", nargs=2, metavar=("KEY", "VALUE"),
                   default=[], help="写入 policy.json，可重复：--set approval_mode ask")

    p = sub.add_parser("extend", help="扩展插件（发现 / 预演 / 执行）")
    esub = p.add_subparsers(dest="action", required=True)

    e = esub.add_parser("list", help="列出插件与状态（active / inactive）")
    e.add_argument("--json", action="store_true")

    e = esub.add_parser("help", help="查看单个插件的说明与指令")
    e.add_argument("extend_id")
    e.add_argument("--json", action="store_true")

    e = esub.add_parser("check", help="预演插件执行的等级与审批（不执行）")
    e.add_argument("extend_id")
    e.add_argument("--action", dest="action_name", default=None,
                   help="插件指令名，取 instruction.json 的 commands 键")
    e.add_argument("--arg", action="append", default=[],
                   help="传给插件的参数，可重复")
    e.add_argument("--json", action="store_true")

    e = esub.add_parser("run", help="建 Task 并启动插件（走既有审批与预检）")
    e.add_argument("extend_id")
    e.add_argument("--action", dest="action_name", default=None,
                   help="插件指令名，取 instruction.json 的 commands 键")
    e.add_argument("--arg", action="append", default=[],
                   help="传给插件的参数，可重复")
    e.add_argument("--approval", default=None, help="复用已有审批 id")
    e.add_argument("--id", default=None, help="指定 task id")
    e.add_argument("--timeout", type=int, default=None, help="超时秒数，0 为不限")
    e.add_argument("--wait", action="store_true", help="同步等待执行结束")
    e.add_argument("--force", action="store_true", help="task id 已存在时覆盖")
    e.add_argument("--json", action="store_true")

    p = sub.add_parser("visa",
                       help="签证管理（issue / list / peek / revoke / consume）")
    p.add_argument("visa_action",
                   choices=["issue", "list", "peek", "revoke", "consume"])
    p.add_argument("visa_id", nargs="?", default=None, help="peek/revoke/consume 用")
    p.add_argument("--id", dest="visa_id_flag", default=None, help="等价于位置参数 visa_id")
    p.add_argument("--number", default=None,
                   help="配额次数；plan 场景可写 unlimited")
    p.add_argument("--time", type=int, default=None, help="有效期（分钟），默认 30")
    p.add_argument("--note", default=None)
    p.add_argument("--task", default=None,
                   help="签发 Task Visa：读 tasks/<id>/task.json 的命令原文并绑定哈希")
    p.add_argument("--plan", default=None,
                   help="签发 Plan Visa：读该 Plan 的 DAG 并绑定指纹")
    p.add_argument("--scope-type", dest="scope_type", default=None,
                   choices=["digest", "command", "plan"])
    p.add_argument("--scope-hash", dest="scope_hash", default=None)
    p.add_argument("--task-id", dest="task_id", default=None)
    p.add_argument("--plan-id", dest="plan_id", default=None)

    return ap


def _plan_module():
    """按需导入同目录 ats_plan.py。

    ats_plan 反向 import ats（core 调 extension），模块级 import 会成环，
    所以只在真正需要读 plan.json / 校验 DAG 时才延迟导入。
    """
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import ats_plan
    return ats_plan


def _visa_target_task(root, task_id):
    """Task Visa 的作用域：按 task.json 里的命令原文算哈希。

    防篡改靠哈希本身：签发这一刻的内容被写进签证，之后谁再改 task.json，
    消费时算出来的哈希就对不上，签证自动失效，不需要额外时间戳比对。
    """
    task = load_task(root, task_id)
    if not task:
        return None, {"error": "task_not_found", "task_id": task_id,
                      "hint": "先 create 建 Task，或 list 确认 task_id 拼写"}
    text = hook_gate_text(task.get("command") or "", task.get("post_hook"))
    if not text.strip():
        return None, {"error": "task_command_empty", "task_id": task_id,
                      "hint": "task.json 里没有命令原文，没有可绑定的对象"}
    return {"scope_type": "command", "scope_hash": command_scope_hash(text),
            "task_id": str(task.get("id") or task_id), "command": text}, None


def _visa_target_plan(root, plan_id):
    """Plan Visa 的作用域：先校验整条 DAG，合法才允许签发。"""
    planmod = _plan_module()
    sdir, pdir = planmod.resolve_plan(root, plan_id)
    if pdir is None:
        return None, {"error": "plan_not_found", "plan_id": plan_id,
                      "hint": "先 ats_plan.py list 确认 plan_id 拼写"}
    plan = planmod.load_plan(pdir) or {}
    errors = planmod.validate_spec(plan)
    if errors:
        return None, {"error": "plan_spec_invalid", "plan_id": plan_id,
                      "errors": errors,
                      "hint": "DAG 不合法不给签：Plan Visa 只对能跑完的计划有意义"}
    return {"scope_type": "plan", "scope_hash": planmod.plan_fingerprint(plan),
            "plan_id": str(plan.get("id") or plan_id),
            "schedule_id": Path(sdir).name}, None


def cmd_visa(root, args):
    """签证管理入口：issue 在这里补齐作用域，其余动作原样转交 ats_guard。"""
    action = args.visa_action
    visa_id = (args.visa_id_flag or args.visa_id or "").strip()
    if action != "issue":
        argv = ["visa", action]
        if visa_id:
            argv += ["--id", visa_id]
        if action == "consume":
            for flag, value in (("--scope-hash", args.scope_hash),
                                ("--task-id", args.task_id),
                                ("--plan-id", args.plan_id)):
                if value:
                    argv += [flag, value]
        rc, payload = _run_guard(argv, root, GUARD_PASSPHRASE)
        return print_json(payload, rc)

    if args.task and args.plan:
        return print_json({"error": "visa_scope_conflict",
                           "hint": "--task 与 --plan 只能二选一"}, EXIT_USAGE)
    scope_type, scope_hash = args.scope_type, args.scope_hash
    task_id, plan_id, resolved = args.task_id, args.plan_id, None
    if args.task:
        resolved, err = _visa_target_task(root, args.task)
        if err is not None:
            return print_json(err, EXIT_CONFLICT)
        scope_type, scope_hash = resolved["scope_type"], resolved["scope_hash"]
        task_id, plan_id = resolved["task_id"], None
    elif args.plan:
        resolved, err = _visa_target_plan(root, args.plan)
        if err is not None:
            return print_json(err, EXIT_CONFLICT)
        scope_type, scope_hash = resolved["scope_type"], resolved["scope_hash"]
        plan_id, task_id = resolved["plan_id"], None
    argv = ["visa", "issue"]
    if args.number is not None:
        argv += ["--number", str(args.number)]
    if args.time is not None:
        argv += ["--time", str(args.time)]
    if args.note:
        argv += ["--note", args.note]
    if scope_type:
        argv += ["--scope-type", scope_type]
    if scope_hash:
        argv += ["--scope-hash", scope_hash]
    if task_id:
        argv += ["--task-id", task_id]
    if plan_id:
        argv += ["--plan-id", plan_id]
    rc, payload = _run_guard(argv, root, GUARD_PASSPHRASE)
    if isinstance(payload, dict) and payload.get("ok") and resolved:
        payload["scope_target"] = {k: v for k, v in resolved.items() if k != "command"}
        if resolved.get("command"):
            payload["command"] = resolved["command"]
    return print_json(payload, rc)


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
    "extend": cmd_extend,
    "visa": cmd_visa,
}


def main(argv=None):
    global GUARD_PASSPHRASE, GUARD_VISA_ID
    argv = list(sys.argv[1:] if argv is None else argv)
    # 看门狗参数必须在 argparse 之前剥离：它们是跨子命令的全局开关
    skip_guard, guard_approval, visa_id, passphrase, argv = _extract_guard_flags(argv)
    GUARD_PASSPHRASE = passphrase or None
    GUARD_VISA_ID = visa_id or None
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command_name:
        parser.print_help()
        return EXIT_USAGE
    if args.command_name == "approve" and not args.all and not args.approval_id:
        print(json.dumps({"error": "missing_approval_id",
                          "hint": "使用 approve <id>... 或 --all"}, ensure_ascii=False))
        return EXIT_USAGE
    root = ats_root(_pre_scan_root(argv) or args.root)
    denied = guard_handshake(root, skip_guard=skip_guard, guard_approval=guard_approval,
                             visa_id=visa_id)
    if denied is not None:
        # 准入失败时只放行 approve 与 visa 的只读/签发动作：
        #   approve —— 否则「跳过看门狗」这类审批单永远批不了，自己锁死自己；
        #   visa issue/list/peek —— 核心脚本正被改动时，人要签发 Task/Plan 签证
        #     才能继续测试，签发本身仍受人类口令约束。
        # revoke / consume 不放行：它们不需要口令，没必要在准入失败时开口子。
        exempt = args.command_name == "approve" or (
            args.command_name == "visa" and args.visa_action in ("issue", "list", "peek"))
        if not exempt:
            return print_json(denied["payload"], denied["code"])
        log_event(root, "guard_bypassed_for_%s" % args.command_name,
                  action=getattr(args, "visa_action", None),
                  stage=(denied["payload"].get("detail") or {}).get("stage"))
    handler = HANDLERS[args.command_name]
    return handler(root, args) or EXIT_OK



if __name__ == "__main__":
    sys.exit(main())
