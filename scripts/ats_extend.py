#!/usr/bin/env python3
"""扩展插件只读发现层：扫描 <ATS_ROOT>/extend/ 并给出状态。

这一层只做三件事：读 instruction.json、查文件是否齐全、报状态。
它从不导入、从不执行插件代码，哪怕状态是 active——执行层是后续批次的事。

状态只有两种，非 active 一律不执行（fail-closed）：

    active    描述文件合法、入口存在、依赖可满足
    inactive  任一校验不过，reason 指出是哪一条

目录约定：<ATS_ROOT>/extend/<extend_id>/instruction.json
目录名必须与 instruction.json 里的 extend_id 相同：执行层要靠 extend_id 定位目录，
两个名字不一致迟早指错文件，所以直接判 inactive。

等级不信任声明值：插件最低 L2，声明 L0/L1 一律抬到 L2，缺失或非法按 L3。
理由是权限门读的是 task 里的 level，而插件调用文本（--ext id --action x）里
没有任何硬危险特征可供推导，声明值若被当真就等于让被审对象自己定权限。
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

EXTEND_DIRNAME = "extend"
INSTRUCTION_NAME = "instruction.json"

LEVELS = ("L0", "L1", "L2", "L3")
LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
EXTEND_LEVEL_FLOOR = "L2"          # 插件声明等级的下限，低于此值一律抬上来
EXTEND_LEVEL_INVALID = "L3"        # 缺失或非法时的覆写值

# 只有执行真正依赖的字段才判 inactive；展示字段缺失只填默认值。
# 理由是「缺 desc 的插件照样能跑」，把它判死只会逼作者编一段假说明。
REQUIRED_FIELDS = ("extend_id", "language", "entry_point", "commands")
DISPLAY_DEFAULTS = {"dis_name": None, "desc": "", "version": "unknown"}

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

LANGUAGE_ALIASES = {
    "python": "python", "py": "python",
    "node": "node", "nodejs": "node", "js": "node",
    "binary": "binary", "exe": "binary",
    "go": "binary", "rust": "binary", "c": "binary", "cpp": "binary",
}

# 与 ats_preflight.MAX_SOURCE_BYTES 对齐：超过这个大小的 python 入口不会被
# 静态审查读到源码，等于执行前少了一道检查，因此判 inactive 而不是 warning。
MAX_ENTRY_BYTES = 512 * 1024
MAX_ARG_LEN = 1024

# 门禁文本的解释器前缀；binary 直接跑入口，没有前缀。
# python 用当前解释器而不是字面量 "python"：_python_deps_missing 是用
# importlib 在本进程里查依赖的，若执行时换 PATH 上的另一个 python，
# 「校验过的依赖」和「真正跑起来的解释器」就不是同一个环境。
INTERPRETER_PREFIX = {"python": sys.executable or "python",
                      "node": "node", "binary": None}

# 含双引号或换行的参数会让门禁文本无法确定，直接拒掉而不是转义
FORBIDDEN_ARG_PATTERN = re.compile(r'["\r\n]')


def extend_dir(root) -> Path:
    return Path(root) / EXTEND_DIRNAME


def _read_instruction(path: Path):
    """返回 (数据, 错误说明)；解析失败不抛异常。"""
    if not path.is_file():
        return None, "%s 不存在" % path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:            # noqa: BLE001
        return None, "%s 解析失败: %s: %s" % (path.name, type(exc).__name__, exc)
    if not isinstance(data, dict):
        return None, "%s 顶层必须是 JSON 对象" % path.name
    return data, None


def normalize_level(declared, floor=EXTEND_LEVEL_FLOOR):
    """把声明等级归一化：非法→L3，低于下限→抬到下限。

    返回 (等级, 说明)。说明为 None 表示声明值被原样采用。
    """
    raw = str(declared or "").strip().upper()
    if raw not in LEVELS:
        return EXTEND_LEVEL_INVALID, "missing_or_invalid_level"
    if LEVEL_RANK[raw] < LEVEL_RANK[floor]:
        return floor, "raised_to_floor"
    return raw, None


def _python_deps_missing(names):
    """只查顶层模块名，不导入：find_spec 不执行模块代码。"""
    missing = []
    for name in names:
        top = name.split(".")[0].strip()
        if not top:
            continue
        try:
            spec = importlib.util.find_spec(top)
        except (ImportError, AttributeError, ValueError, ModuleNotFoundError):
            spec = None
        if spec is None:
            missing.append(name)
    return missing


def _split_deps(raw):
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [x.strip() for x in re.split(r"[,;\s]+", str(raw)) if x.strip()]


def _check_entry(plugin_dir: Path, entry_point: str):
    """入口必须落在插件目录内且真实存在，防目录穿越。"""
    if not entry_point:
        return None, "missing_entry_point", "entry_point 为空"
    if Path(entry_point).is_absolute():
        return None, "entry_point_escapes", "entry_point 不能是绝对路径: %s" % entry_point
    target = (plugin_dir / entry_point)
    try:
        resolved = target.resolve()
    except OSError as exc:
        return None, "missing_entry_point", "入口无法解析: %s" % exc
    if plugin_dir.resolve() not in resolved.parents and resolved != plugin_dir.resolve():
        return None, "entry_point_escapes", "entry_point 越出插件目录: %s" % entry_point
    if not resolved.is_file():
        return None, "missing_entry_point", "入口文件不存在: %s" % entry_point
    return resolved, None, None


def _check_runtime(language, entry_path, deps):
    """依赖与解释器检查。返回 (problems, warnings)。"""
    problems = []
    warnings = []
    if language == "python":
        missing = _python_deps_missing(deps)
        if missing:
            problems.append({"code": "missing_runtime_library",
                             "detail": "缺少 Python 依赖: %s" % ", ".join(missing)})
    elif language == "node":
        if not shutil.which("node"):
            problems.append({"code": "missing_interpreter",
                             "detail": "PATH 上找不到 node"})
        if deps:
            # 本层禁止执行代码，node 依赖无法在不跑 node 的前提下验证
            warnings.append({"code": "runtime_library_unverified",
                             "detail": "node 依赖未校验: %s（需执行才能确认，本层不做）"
                                       % ", ".join(deps)})
    elif language == "binary":
        if entry_path is not None and not str(entry_path).lower().endswith(
                (".exe", ".bat", ".cmd", ".com")):
            warnings.append({"code": "binary_entry_without_exe_suffix",
                             "detail": "入口没有可执行后缀，Windows 下可能拉不起来: %s"
                                       % Path(entry_path).name})
    return problems, warnings


def inspect_plugin(plugin_dir, folder=None, level_floor=EXTEND_LEVEL_FLOOR):
    """检查一个插件目录，返回状态字典。只读，不导入不执行。"""
    plugin_dir = Path(plugin_dir)
    folder = folder or plugin_dir.name
    item = {
        "extend_id": folder,
        "folder": folder,
        "dir": str(plugin_dir),
        "state": "inactive",
        "reason": None,
        "detail": None,
        "dis_name": None,
        "desc": None,
        "version": None,
        "language": None,
        "entry_point": None,
        "entry_path": None,
        "declared_level": None,
        "level": None,
        "level_note": None,
        "runtime_library": "",
        "commands": {},
        "problems": [],
        "warnings": [],
    }

    if not SAFE_ID_PATTERN.match(folder):
        item["reason"] = "bad_extend_id"
        item["detail"] = "目录名只允许字母、数字、下划线、短横线，且不超过 64 字符"
        item["problems"].append({"code": "bad_extend_id", "detail": item["detail"]})
        return item

    data, err = _read_instruction(plugin_dir / INSTRUCTION_NAME)
    if data is None:
        item["reason"] = "missing_or_invalid_instruction"
        item["detail"] = err
        item["problems"].append({"code": "missing_or_invalid_instruction", "detail": err})
        return item

    missing = [f for f in REQUIRED_FIELDS if data.get(f) in (None, "")]
    if missing:
        item["reason"] = "missing_or_invalid_instruction"
        item["detail"] = "缺少必填字段: %s" % ", ".join(missing)
        item["problems"].append({"code": "missing_or_invalid_instruction",
                                 "detail": item["detail"]})
        return item

    commands = data.get("commands")
    if not isinstance(commands, dict) or not commands:
        item["reason"] = "missing_or_invalid_instruction"
        item["detail"] = "commands 必须是非空对象（指令名 -> 用法说明）"
        item["problems"].append({"code": "missing_or_invalid_instruction",
                                 "detail": item["detail"]})
        return item

    item["dis_name"] = str(data.get("dis_name") or folder)
    item["desc"] = str(data.get("desc") or DISPLAY_DEFAULTS["desc"])
    item["version"] = str(data.get("version") or DISPLAY_DEFAULTS["version"])
    item["entry_point"] = str(data.get("entry_point"))
    item["commands"] = {str(k): str(v) for k, v in commands.items()}
    item["runtime_library"] = data.get("runtime_library") or ""

    declared_id = str(data.get("extend_id")).strip()
    item["declared_id"] = declared_id
    if declared_id != folder:
        item["problems"].append({
            "code": "extend_id_mismatch",
            "detail": "目录名 %r 与 instruction.json 的 extend_id %r 不一致"
                      % (folder, declared_id),
        })

    language_raw = str(data.get("language")).strip().lower()
    language = LANGUAGE_ALIASES.get(language_raw)
    if language is None:
        item["problems"].append({
            "code": "unsupported_language",
            "detail": "language %r 不受支持，可用: %s"
                      % (language_raw, ", ".join(sorted(LANGUAGE_ALIASES))),
        })
    item["language"] = language or language_raw

    item["declared_level"] = str(data.get("level")).strip()
    level, note = normalize_level(item["declared_level"], level_floor)
    item["level"] = level
    item["level_note"] = note

    entry_path = None
    if language is not None:
        entry_path, code, detail = _check_entry(plugin_dir, item["entry_point"])
        if code:
            item["problems"].append({"code": code, "detail": detail})
        elif entry_path is not None:
            item["entry_path"] = str(entry_path)
            # 只有 python 会被 preflight 读源码；超限时它静默跳过，
            # 「执行前必然静态审过」的承诺就破了，所以在这里判 inactive。
            if language == "python":
                try:
                    size = entry_path.stat().st_size
                except OSError:
                    size = 0
                if size > MAX_ENTRY_BYTES:
                    item["problems"].append({
                        "code": "entry_too_large",
                        "detail": "入口 %d 字节超过静态审查上限 %d，源码不会被扫描"
                                  % (size, MAX_ENTRY_BYTES),
                    })

    if language is not None:
        dep_problems, warnings = _check_runtime(
            language, entry_path, _split_deps(item["runtime_library"]))
        item["problems"].extend(dep_problems)
        item["warnings"].extend(warnings)

    if item["problems"]:
        item["reason"] = item["problems"][0]["code"]
        item["detail"] = item["problems"][0]["detail"]
    else:
        item["state"] = "active"
    return item


def scan(root, level_floor=EXTEND_LEVEL_FLOOR):
    """扫描整个 extend 目录，返回汇总。目录不存在时返回空结果而不是报错。"""
    base = extend_dir(root)
    result = {
        "extend_dir": str(base),
        "extends": [],
        "count": 0,
        "active": 0,
        "inactive": 0,
        "skipped_symlinks": 0,
        "level_floor": level_floor,
    }
    if not base.is_dir():
        return result
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_symlink():
            # 与 @enc 通配一致：符号链接一律跳过并计数，不跟进去
            result["skipped_symlinks"] += 1
            continue
        if not entry.is_dir():
            continue
        result["extends"].append(inspect_plugin(entry, entry.name, level_floor))
    result["count"] = len(result["extends"])
    result["active"] = sum(1 for x in result["extends"] if x["state"] == "active")
    result["inactive"] = result["count"] - result["active"]
    return result


def describe(root, extend_id, level_floor=EXTEND_LEVEL_FLOOR):
    """按 id 取单个插件；id 本身也做安全校验，不能借它跳出 extend 目录。"""
    extend_id = str(extend_id or "").strip()
    if not SAFE_ID_PATTERN.match(extend_id):
        return None
    plugin_dir = extend_dir(root) / extend_id
    if plugin_dir.is_symlink() or not plugin_dir.is_dir():
        return None
    return inspect_plugin(plugin_dir, extend_id, level_floor)


def render_table(items):
    """列表的人读形式；没有插件时返回空列表由调用方打印提示。"""
    if not items:
        return []
    lines = ["%-24s %-9s %-6s %-8s %s"
             % ("ID", "STATE", "LEVEL", "LANG", "DESC")]
    for it in items:
        desc = (it.get("desc") or "").replace("\n", " ")
        if len(desc) > 50:
            desc = desc[:47] + "..."
        lines.append("%-24s %-9s %-6s %-8s %s"
                     % (it["extend_id"], it["state"], it.get("level") or "-",
                        it.get("language") or "-", desc))
    return lines


def render_help(item):
    """单个插件的说明文本：desc + commands + 状态原因。"""
    lines = []
    lines.append("%s  (%s)" % (item.get("dis_name") or item["extend_id"],
                               item["extend_id"]))
    lines.append("version : %s" % (item.get("version") or "-"))
    lines.append("state   : %s%s" % (item["state"],
                                     "" if not item.get("reason") else
                                     "  (%s)" % item["reason"]))
    lines.append("level   : %s%s" % (item.get("level") or "-",
                                     "" if not item.get("level_note") else
                                     "  (%s)" % item["level_note"]))
    lines.append("language: %s" % (item.get("language") or "-"))
    lines.append("entry   : %s" % (item.get("entry_point") or "-"))
    lines.append("")
    lines.append(item.get("desc") or "(无说明)")
    if item.get("commands"):
        lines.append("")
        lines.append("commands:")
        for name, usage in item["commands"].items():
            lines.append("  %-12s %s" % (name, usage))
    if item.get("state") == "active" and item.get("commands"):
        lines.append("")
        lines.append("预演审批（不执行）: ats extend check %s --action <指令名>"
                     % item["extend_id"])
    if item.get("detail"):
        lines.append("")
        lines.append("blocked: %s" % item["detail"])
    for w in item.get("warnings") or []:
        lines.append("warning: %s" % w.get("detail"))
    return lines


def check_args(action, args):
    """校验指令名与参数。返回 (action, args, error)；error 非空时前两项为 None。

    纯文本检查，不执行也不落盘。含双引号或换行的参数会让门禁文本无法确定，
    而门禁文本同时是审批绑定的比对依据，所以直接拒掉而不是尝试转义。
    """
    if action is not None:
        action = str(action)
        if not SAFE_ID_PATTERN.match(action):
            return None, None, {
                "code": "bad_action",
                "detail": "指令名只允许字母、数字、下划线、短横线: %r" % action}
    out = []
    for raw in args or []:
        text = str(raw)
        if FORBIDDEN_ARG_PATTERN.search(text):
            return None, None, {
                "code": "bad_extend_arg",
                "detail": "参数不能含双引号或换行: %r" % text}
        if len(text) > MAX_ARG_LEN:
            return None, None, {
                "code": "bad_extend_arg",
                "detail": "参数超过 %d 字符: %r..." % (MAX_ARG_LEN, text[:32])}
        out.append(text)
    return action, out, None


def _quote_arg(text):
    """门禁文本里的参数引用：含空白就加双引号。

    不做 shell 转义：本层不执行，文本只用于正则匹配与审批绑定。
    含双引号/换行的参数在 check_args 阶段已被拒，这里不会出现嵌套引号。
    """
    if text == "":
        return '""'
    if re.search(r"[ \t]", text):
        return '"%s"' % text
    return text


def extend_argv(item, action=None, args=None):
    """插件执行的 argv 列表（不经 shell）。返回 list 或 None（入口缺失）。

    与 gate_text 同源：gate_text 由本函数的每一项加引号拼成，所以「审批绑定的
    文本」与「真正执行的 argv」不可能指向不同内容 —— 这是第 3 批接线的前提。
    列表模式不经过 shell，参数里的空格、`&`、`;` 都只是普通字符。
    """
    entry_path = item.get("entry_path")
    if not entry_path:
        return None
    argv = []
    prefix = INTERPRETER_PREFIX.get(item.get("language"))
    if prefix:
        argv.append(prefix)
    argv.append(str(entry_path))
    if action:
        argv.append("--action")
        argv.append(str(action))
    for text in args or []:
        argv.append(str(text))
    return argv


def gate_text(item, action=None, args=None):
    """插件执行的门禁文本，三处共用：等级推导、权限门命令文本、预检源码定位。

    文本里带入口的绝对路径，因此 ats_preflight._python_sources 会直接读到
    插件源码 —— 「把插件源码注入预检」不需要改 preflight，靠这段文本就成立。
    执行与预演都从 extend_argv 派生，文本只是它的加引号投影，不存在第二条路径。

    注意覆盖面：只有 python 入口能被静态读到源码；node / binary 插件在这段
    文本里没有可分析的源码，预检对它们只剩 shell 文本层，意义有限。
    """
    argv = extend_argv(item, action, args)
    if argv is None:
        return None
    return " ".join(_quote_arg(part) for part in argv)


def task_level(item, level_floor=EXTEND_LEVEL_FLOOR):
    """插件任务的等级与来源说明。返回 (等级, 说明)。

    用 declared_level 重算而不是直接取 item["level"]：两者应当一致，
    但如果将来有人改了 inspect 的归一化逻辑，这里仍会独立抬到下限，
    不会因为一处改动就漏掉「插件不能自己定权限」这条。
    """
    if item is None:
        return None, "extend_not_found"
    level, note = normalize_level(item.get("declared_level"), level_floor)
    return level, note


def render_check(data):
    """extend check 的人读输出。"""
    lines = []
    lines.append("%s  (%s)" % (data.get("dis_name") or data["extend_id"],
                               data["extend_id"]))
    lines.append("state    : %s" % data["state"])
    lines.append("action   : %s" % (data.get("action") or "-"))
    note = data.get("level_note")
    lines.append("level    : %s%s" % (data.get("level") or "-",
                                      "" if not note else "  (%s)" % note))
    lines.append("gate     : %s" % (data.get("gate_text") or "-"))
    gate = data.get("gate") or {}
    lines.append("required : %s  (hard_floor: %s)"
                 % (gate.get("required_level") or "-", gate.get("hard_floor") or "-"))
    lines.append("actor    : %s  policy_max: %s"
                 % (gate.get("actor_level") or "-", gate.get("policy_max") or "-"))
    lines.append("mode     : %s" % (gate.get("approval_mode") or "-"))
    lines.append("would    : %s%s" % (gate.get("would") or "-",
                                      "" if not gate.get("reason")
                                      else "  (%s)" % gate["reason"]))
    pf = data.get("preflight")
    if pf:
        lines.append("preflight: confidence=%s tags=%s"
                     % (pf.get("confidence") or "-",
                        ", ".join(pf.get("tags") or []) or "-"))
    lines.append("executed : no")
    for w in data.get("warnings") or []:
        lines.append("warning  : %s" % w.get("detail"))
    return lines
