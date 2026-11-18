#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ats_plugin.py — Python 插件层（plugins/<plugin_id>/plugin.json + Injection.py）

与 ats_extend.py 的分工（两者都叫「插件」，但执行模型相反）：

    extend  <ATS_ROOT>/extend/<id>/instruction.json
            只读清单，执行**永远**拉起子进程走 Task 链路；插件代码不进主进程，
            也因此要过权限门、审批、supervisor —— 它就是一条普通命令。

    plugin  <代码根>/plugins/<id>/plugin.json + Injection.py
            真的把 Injection.py 导入主进程，所以闸门顺序是硬的：
              静态校验（本模块，不导入）
                → 封签校验（ats_guard.py plugin verify，子进程、零依赖）
                  → 受控 exec
            「check 通过」严格等于「load 不会因为闸门被拒」：两者共用
            _prepare 这一条链，不允许出现第二条判定路径。

三条铁律（照搬 R1 的插件层，理由不变）：

  1. 不写 .pyc —— `sys.dont_write_bytecode` 在本模块顶层就设死，否则插件目录
     多出一个 __pycache__，封签当场失配，而且字节码还能绕过「源码即审查对象」；
  2. 先封签再 exec —— 没封签 / 封签与字节不符，一律拒绝导入，不回落到审批；
  3. 导入后清模块缓存 —— 从插件目录加载出来的模块全部摘掉，避免两个插件的
     同名 helper 互相顶替。

插件目录是**代码**，不属于任何 ATS_ROOT 的运行态；封签清单单独一份
（<root>/ats_guard_plugin_manifest.json），不混进核心基线，因此插件换版不需要
重封核心脚本。可用 ATS_PLUGINS_ROOT 覆盖插件目录（CI / 自测用）。

插件里唯一可用的 ATS 接口是 `import ats_api`（本模块注入 sys.modules 的窄接口）：
tr / command / log / state_dir。**没有**执行命令、读写任务、发审批、拿 policy 的入口。
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import json
import os
import re
import subprocess
import traceback
import types
from pathlib import Path

PLUGIN_DIR_NAME = "plugins"
PLUGIN_STATE_DIRNAME = "plugin_state"
MANIFEST_NAME = "plugin.json"
DEFAULT_INJECTION = "Injection.py"
LOCALIZATION_DIRNAME = "Localization"
STRINGS_NAME = "strings.json"
DEFAULT_LOCALE = "en"

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
LEVEL_ORDER = ("L0", "L1", "L2", "L3", "L4")
PLUGIN_LEVEL_FLOOR = "L2"
MAX_ENTRY_BYTES = 512 * 1024

API_VERSION = "1.0"
RESERVED_PREFIX = "ats."
COMMAND_PATTERN_MAX = 64

GUARD_FILE = "ats_guard.py"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_DENIED = 4

SEAL_HINT = ("python scripts/%s plugin seal --confirm --passphrase-stdin" % GUARD_FILE)

REQUIRED_FIELDS = ("plugin_id", "name", "version", "description", "level",
                   "allow_language")

FIELD_ALIASES = {
    "plugin_id": ("plugin_id", "id"),
    "name": ("name", "dis_name"),
    "version": ("version",),
    "description": ("description", "desc"),
    "level": ("level",),
    "allow_language": ("allow_language", "language"),
    "injection": ("injection", "entry_point"),
    "extend": ("extend",),
}

LANGUAGE_ALIASES = {"python": "python", "py": "python", "python3": "python"}


# ---------------------------------------------------------------- 路径与清单

def plugins_root():
    """插件目录（代码根，不是 ATS_ROOT）。ATS_PLUGINS_ROOT 仅供 CI / 自测隔离。"""
    env = os.environ.get("ATS_PLUGINS_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / PLUGIN_DIR_NAME


def state_root(root):
    """插件可写目录：放在 <root> 下，插件写盘不会破坏自己的封签。"""
    return Path(root) / PLUGIN_STATE_DIRNAME


def _read_manifest_file(path):
    """读 plugin.json。返回 (dict, 错误说明)；BOM / 解析失败都按不可用处理。"""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        data = json.loads(text)
    except Exception as exc:             # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)
    if not isinstance(data, dict):
        return None, "plugin.json 顶层必须是对象"
    return data, None


def _field(data, name):
    """按别名取字段：插件作者写 id / desc 这类简写也能认，不必背字段表。"""
    for key in FIELD_ALIASES.get(name, (name,)):
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_level(declared, level_floor=PLUGIN_LEVEL_FLOOR):
    """把声明的等级归一化，并抬到插件下限：插件不能自己给自己定权限。"""
    text = str(declared or "").strip().upper()
    if text not in LEVEL_ORDER:
        return level_floor, "declared level %r 非法，按插件下限 %s" % (declared, level_floor)
    if LEVEL_ORDER.index(text) < LEVEL_ORDER.index(level_floor):
        return level_floor, "插件不能低于 %s，已抬到 %s" % (level_floor, level_floor)
    return text, ""


def _check_entry(plugin_dir, name):
    """校验入口：必须是 .py、必须落在插件目录内、必须真实存在。

    防穿越是硬要求：入口路径来自 plugin.json（第三方可写），`../../scripts/ats.py`
    这种写法如果被放过，插件就等于拿到了核心脚本的执行权。
    """
    text = str(name or "").strip()
    if not text:
        return None, "missing_injection", "injection 字段为空"
    if not text.lower().endswith(".py"):
        return None, "unsupported_language", "R0 插件只接受 Python 入口（.py）: %r" % text
    base = Path(plugin_dir).resolve()
    candidate = (base / text)
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return None, "bad_injection_path", "入口路径无法解析: %s" % exc
    if resolved != base and base not in resolved.parents:
        return None, "bad_injection_path", "入口必须落在插件目录内: %r" % text
    if not resolved.is_file():
        return None, "missing_injection", "入口文件不存在: %s" % text
    return resolved, None, None


def localization_locales(plugin_dir):
    """可用语言：Localization/<locale>/strings.json 存在的目录才算数。"""
    base = Path(plugin_dir) / LOCALIZATION_DIRNAME
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        if (entry / STRINGS_NAME).is_file():
            out.append(entry.name)
    return out


def load_strings(plugin_dir, locale):
    path = Path(plugin_dir) / LOCALIZATION_DIRNAME / str(locale) / STRINGS_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:                    # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def resolve_string(plugin_dir, locale, key, kwargs=None):
    """回退链：请求语言 → en → 插件里任意一个语言；都没有返回 None。"""
    chain = []
    for name in (locale, DEFAULT_LOCALE):
        if name and name not in chain:
            chain.append(name)
    chain.extend(x for x in localization_locales(plugin_dir) if x not in chain)
    for name in chain:
        strings = load_strings(plugin_dir, name)
        if key in strings:
            text = strings[key]
            if kwargs:
                try:
                    return text.format(**kwargs)
                except Exception:        # noqa: BLE001
                    return text
            return text
    return None


# ---------------------------------------------------------------- 静态校验（不导入）

def inspect_plugin(plugin_dir, folder, level_floor=PLUGIN_LEVEL_FLOOR):
    """只读 plugin.json 与文件系统，不导入任何插件代码。

    返回的 state 只有 active / inactive 两种；inactive 的 reason 取第一条 problem，
    便于调用方直接用错误码给出下一步。
    """
    item = {
        "plugin_id": str(folder),
        "dir": str(plugin_dir),
        "state": "inactive",
        "name": str(folder),
        "dis_name": str(folder),
        "desc": "",
        "version": "",
        "level": None,
        "level_note": "",
        "allow_language": [],
        "injection": DEFAULT_INJECTION,
        "extend": None,
        "entry_path": None,
        "extend_path": None,
        "localization": [],
        "problems": [],
        "warnings": [],
        "reason": None,
        "detail": None,
    }

    data, err = _read_manifest_file(Path(plugin_dir) / MANIFEST_NAME)
    if data is None:
        item["reason"] = "missing_or_invalid_manifest"
        item["detail"] = err
        item["problems"].append({"code": "missing_or_invalid_manifest", "detail": err})
        return item

    missing = [f for f in REQUIRED_FIELDS if _field(data, f) in (None, "")]
    if missing:
        item["reason"] = "missing_or_invalid_manifest"
        item["detail"] = "缺少必填字段: %s" % ", ".join(missing)
        item["problems"].append({"code": "missing_or_invalid_manifest",
                                 "detail": item["detail"]})
        return item

    item["name"] = str(_field(data, "name"))
    item["dis_name"] = item["name"]
    item["desc"] = str(_field(data, "description") or "")
    item["version"] = str(_field(data, "version") or "")
    item["injection"] = str(_field(data, "injection") or DEFAULT_INJECTION)

    declared_id = str(_field(data, "plugin_id")).strip()
    if declared_id != folder:
        item["problems"].append({
            "code": "plugin_id_mismatch",
            "detail": "目录名 %r 与 plugin.json 的 plugin_id %r 不一致"
                      % (folder, declared_id),
        })

    languages = _field(data, "allow_language")
    if isinstance(languages, str):
        languages = [languages]
    if not isinstance(languages, (list, tuple)) or not languages:
        item["problems"].append({
            "code": "missing_or_invalid_manifest",
            "detail": "allow_language 必须是非空数组（R0 插件只支持 python）",
        })
        languages = []
    normalized = []
    for raw in languages:
        alias = LANGUAGE_ALIASES.get(str(raw).strip().lower())
        if alias is None:
            item["problems"].append({
                "code": "unsupported_language",
                "detail": "allow_language %r 不受支持，R0 插件只接受 python" % raw,
            })
            continue
        if alias not in normalized:
            normalized.append(alias)
    item["allow_language"] = normalized

    item["declared_level"] = str(_field(data, "level")).strip()
    level, note = _normalize_level(item["declared_level"], level_floor)
    item["level"] = level
    item["level_note"] = note

    entry_path, code, detail = _check_entry(plugin_dir, item["injection"])
    if code:
        item["problems"].append({"code": code, "detail": detail})
    else:
        item["entry_path"] = str(entry_path)
        try:
            size = entry_path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_ENTRY_BYTES:
            item["problems"].append({
                "code": "entry_too_large",
                "detail": "入口 %d 字节超过静态审查上限 %d，源码不会被人工看完"
                          % (size, MAX_ENTRY_BYTES),
            })

    extend_name = str(_field(data, "extend") or "").strip()
    if extend_name:
        extend_path, code, detail = _check_entry(plugin_dir, extend_name)
        if code:
            item["problems"].append({"code": code, "detail": "extend: %s" % detail})
        else:
            item["extend"] = extend_name
            item["extend_path"] = str(extend_path)

    item["localization"] = localization_locales(plugin_dir)

    if (Path(plugin_dir) / "ats_api.py").is_file():
        item["warnings"].append({
            "code": "shadow_api_module",
            "detail": "插件目录里有 ats_api.py：它不会生效（加载器先占住 sys.modules），"
                      "删掉以免误导",
        })

    if item["problems"]:
        item["reason"] = item["problems"][0]["code"]
        item["detail"] = item["problems"][0]["detail"]
    else:
        item["state"] = "active"
    return item


def _plugin_dirs():
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


def scan(level_floor=PLUGIN_LEVEL_FLOOR):
    """扫描整个 plugins 目录；目录不存在时返回空结果而不是报错。"""
    base = plugins_root()
    result = {
        "plugin_dir": str(base),
        "plugins": [],
        "count": 0,
        "active": 0,
        "inactive": 0,
        "level_floor": level_floor,
    }
    for entry in _plugin_dirs():
        result["plugins"].append(inspect_plugin(entry, entry.name, level_floor))
    result["count"] = len(result["plugins"])
    result["active"] = sum(1 for x in result["plugins"] if x["state"] == "active")
    result["inactive"] = result["count"] - result["active"]
    return result


def describe(plugin_id, level_floor=PLUGIN_LEVEL_FLOOR):
    """按 id 取单个插件；id 本身也校验，不能借它跳出 plugins 目录。"""
    text = str(plugin_id or "").strip()
    if not SAFE_ID_PATTERN.match(text):
        return None
    plugin_dir = plugins_root() / text
    if plugin_dir.is_symlink() or not plugin_dir.is_dir():
        return None
    return inspect_plugin(plugin_dir, text, level_floor)


def render_table(items):
    if not items:
        return []
    lines = ["%-24s %-9s %-6s %-8s %s"
             % ("ID", "STATE", "LEVEL", "LANG", "DESC")]
    for it in items:
        desc = (it.get("desc") or "").replace("\n", " ")
        if len(desc) > 50:
            desc = desc[:47] + "..."
        lines.append("%-24s %-9s %-6s %-8s %s"
                     % (it["plugin_id"], it["state"], it.get("level") or "-",
                        "/".join(it.get("allow_language") or []) or "-", desc))
    return lines


def render_help(item, seal=None):
    lines = []
    lines.append("%s  (%s)" % (item.get("name") or item["plugin_id"], item["plugin_id"]))
    lines.append("version : %s" % (item.get("version") or "-"))
    lines.append("state   : %s%s" % (item["state"],
                                     "" if not item.get("reason") else
                                     "  (%s)" % item["reason"]))
    lines.append("level   : %s%s" % (item.get("level") or "-",
                                     "" if not item.get("level_note") else
                                     "  (%s)" % item["level_note"]))
    lines.append("lang    : %s" % ("/".join(item.get("allow_language") or []) or "-"))
    lines.append("entry   : %s" % (item.get("injection") or "-"))
    lines.append("extend  : %s" % (item.get("extend") or "-"))
    lines.append("locale  : %s" % (", ".join(item.get("localization") or []) or "-"))
    if seal is not None:
        lines.append("seal    : %s" % seal.get("state"))
    lines.append("")
    lines.append(item.get("desc") or "(无说明)")
    if item.get("state") == "active":
        lines.append("")
        lines.append("看它能登记什么命令 : ats plugin load %s" % item["plugin_id"])
        lines.append("跑它的一条命令     : ats plugin run %s --command <命令名>"
                     % item["plugin_id"])
    if item.get("detail"):
        lines.append("")
        lines.append("blocked: %s" % item["detail"])
    for w in item.get("warnings") or []:
        lines.append("warning: %s" % w.get("detail"))
    return lines


# ---------------------------------------------------------------- 封签校验

def verify_seal(root):
    """跑 ats_guard.py plugin verify（子进程、零依赖），返回它的 JSON 载荷。

    不在这里自己算哈希：封签口径只允许有一处实现，否则「插件层认为已封签、
    看门狗认为没封签」这种分歧迟早出现。
    """
    script = Path(__file__).resolve().parent / GUARD_FILE
    cmd = [sys.executable, str(script), "--root", str(root), "plugin", "verify"]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": "guard_spawn_failed",
                "detail": "%s: %s" % (type(exc).__name__, exc)}
    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return {"ok": False, "error": "guard_output_invalid",
                "exit_code": proc.returncode,
                "stdout": (proc.stdout or "")[:400],
                "stderr": (proc.stderr or "")[:400]}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "guard_output_invalid"}
    return payload


def seal_entry(seal, plugin_id):
    plugins = (seal or {}).get("plugins")
    if not isinstance(plugins, dict):
        return None
    entry = plugins.get(str(plugin_id))
    return entry if isinstance(entry, dict) else None


def seal_allows(seal, plugin_id):
    """只看本插件那一行：一个新插件没封签，不该把已封好的插件一起拖下水。"""
    entry = seal_entry(seal, plugin_id)
    if entry is None:
        reason = str((seal or {}).get("error") or "plugin_unsealed")
        return False, None, reason
    if entry.get("state") != "active":
        return False, entry, "plugin_" + str(entry.get("state") or "unsealed")
    return True, entry, None


# ---------------------------------------------------------------- 插件 API（窄接口）

class PluginError(Exception):
    """插件主动抛出的可读错误；加载器会把它转成结构化失败，不当作崩溃。"""


_current = None


def current():
    """当前正在被导入的插件 API 对象（模块顶层登记命令时用它）。"""
    if _current is None:
        raise PluginError("ats_api 未绑定插件（只允许在 Injection.py 导入期间调用）")
    return _current


class PluginCommand:
    __slots__ = ("name", "handler", "help", "level")

    def __init__(self, name, handler, help_text="", level=None):
        self.name = name
        self.handler = handler
        self.help = help_text
        self.level = level

    def as_dict(self):
        return {"name": self.name, "help": self.help, "level": self.level}


class PluginAPI:
    """一个插件实例对应一个 API 对象；所有状态都在实例上，不落全局。"""

    def __init__(self, plugin_id, plugin_dir, manifest, locale=None, logger=None,
                 root=None):
        self._plugin_id = str(plugin_id)
        self._plugin_dir = Path(plugin_dir)
        self._manifest = dict(manifest or {})
        self._locale = str(locale or DEFAULT_LOCALE)
        self._logger = logger
        self._root = root
        self._commands = {}

    @property
    def plugin_id(self):
        return self._plugin_id

    @property
    def plugin_dir(self):
        return str(self._plugin_dir)

    @property
    def version(self):
        return self._manifest.get("version") or "unknown"

    @property
    def name(self):
        return self._manifest.get("name") or self._plugin_id

    @property
    def level(self):
        return self._manifest.get("level") or PLUGIN_LEVEL_FLOOR

    @property
    def locale(self):
        return self._locale

    @property
    def allow_language(self):
        return list(self._manifest.get("allow_language") or [])

    def tr(self, key, **kwargs):
        """取一条本地化文案；缺 key 时返回 key 本身，让漏配一眼可见。"""
        value = resolve_string(self._plugin_dir, self._locale, key, kwargs)
        return value if value is not None else str(key)

    def command(self, name, handler, help_text="", level=None):
        """登记命令。命名形如 `<plugin_id>.xxx`，前缀不得是保留的 `ats.`。

        handler 的调用约定固定为 `handler(api, argv)`，返回可 JSON 序列化的结果。
        """
        text = str(name or "").strip()
        if not text or len(text) > COMMAND_PATTERN_MAX:
            raise PluginError("命令名长度非法: %r" % name)
        if text.lower().startswith(RESERVED_PREFIX):
            raise PluginError("命令名不得占用保留前缀 %s: %s" % (RESERVED_PREFIX, text))
        if "." not in text:
            raise PluginError("命令名必须带插件前缀（如 %s.hello）: %s"
                              % (self._plugin_id, text))
        if not callable(handler):
            raise PluginError("命令 handler 不可调用: %s" % text)
        if text in self._commands:
            raise PluginError("命令重复登记: %s" % text)
        self._commands[text] = PluginCommand(text, handler, str(help_text or ""), level)
        return self._commands[text]

    def log(self, message):
        text = str(message or "")
        if self._logger is not None:
            self._logger("[plugin:%s] %s" % (self._plugin_id, text))
        return text

    def state_dir(self):
        """插件可写目录（懒创建）。放在 plugins/ 外，否则每次写盘都会让封签失配。"""
        if self._root is None:
            raise PluginError("未提供 root，插件不可写盘")
        path = state_root(self._root) / self._plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def registered(self):
        return {name: cmd.as_dict() for name, cmd in self._commands.items()}

    def handler(self, name):
        cmd = self._commands.get(str(name or "").strip())
        return cmd.handler if cmd is not None else None

    def __repr__(self):
        return "<PluginAPI %s v%s level=%s>" % (self._plugin_id, self.version, self.level)


def _module_command(name, handler, help_text="", level=None):
    return current().command(name, handler, help_text=help_text, level=level)


def _module_tr(key, **kwargs):
    return current().tr(key, **kwargs)


def _module_log(message):
    return current().log(message)


def _module_state_dir():
    return current().state_dir()


def api_module():
    """构造 `ats_api` 模块对象：插件 `import ats_api` 命中的就是它。

    加载器在 exec 前把它塞进 sys.modules，所以插件目录里即使放一个同名
    ats_api.py 也劫持不到（导入命中缓存，不走文件系统）。
    """
    module = types.ModuleType("ats_api")
    module.API_VERSION = API_VERSION
    module.PluginError = PluginError
    module.PluginAPI = PluginAPI
    module.current = current
    module.command = _module_command
    module.tr = _module_tr
    module.log = _module_log
    module.state_dir = _module_state_dir
    return module


# ---------------------------------------------------------------- 受控导入

def _purge_plugin_modules(plugin_dir, keep):
    """清掉从插件目录加载出来的模块：避免两个插件的同名 helper 互相顶替。"""
    base = str(Path(plugin_dir).resolve())
    for name, module in list(sys.modules.items()):
        if name == keep or module is None:
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        try:
            if str(Path(origin).resolve()).startswith(base):
                sys.modules.pop(name, None)
        except OSError:
            continue


def _exec_file(entry, mod_name, api):
    """exec 单个入口文件并调用它的 register(api)（若有）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(mod_name, str(entry))
    if spec is None or spec.loader is None:
        return None, "plugin_spec_failed", "无法为 %s 构造 import spec" % Path(entry).name
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except PluginError as exc:
        sys.modules.pop(mod_name, None)
        return None, "plugin_error", str(exc)
    except SystemExit as exc:
        sys.modules.pop(mod_name, None)
        return None, "plugin_system_exit", "插件在导入期调用了 sys.exit(%s)" % exc.code
    except Exception:                    # noqa: BLE001
        sys.modules.pop(mod_name, None)
        return None, "plugin_import_failed", traceback.format_exc(limit=6).strip()

    register = getattr(module, "register", None)
    if callable(register):
        try:
            register(api)
        except PluginError as exc:
            return None, "plugin_error", str(exc)
        except Exception:                # noqa: BLE001
            sys.modules.pop(mod_name, None)
            return None, "plugin_register_failed", traceback.format_exc(limit=6).strip()
    return module, None, None


def _exec_plugin(item, locale, root, logger):
    """在隔离命名空间里 exec Injection.py 与可选的 extend 入口，返回 (api, 错误码, 说明)。

    两个入口共用同一个 API 对象，所以命令都汇总到一处；第二个入口失败即整插件
    失败 —— 半加载的插件比没加载更难排查。
    """
    global _current
    entries = [(Path(item["entry_path"]), "ats_plugin_" + item["plugin_id"])]
    if item.get("extend_path"):
        entries.append((Path(item["extend_path"]),
                        "ats_plugin_" + item["plugin_id"] + "_extend"))

    plugin_dir = str(Path(item["dir"]).resolve())
    api = PluginAPI(item["plugin_id"], item["dir"], item,
                    locale=locale, logger=logger, root=root)

    previous = _current
    _current = api
    sys.modules["ats_api"] = api_module()
    sys.path.append(plugin_dir)
    try:
        for entry, mod_name in entries:
            _module, code, detail = _exec_file(entry, mod_name, api)
            if code:
                return None, code, detail
        return api, None, None
    finally:
        _current = previous
        try:
            sys.path.remove(plugin_dir)
        except ValueError:
            pass
        _purge_plugin_modules(plugin_dir, keep=entries[0][1])


def _prepare(root, plugin_id, locale=None, verify=True, logger=None, run_exec=True):
    """静态校验 → 封签校验 → exec。返回 (item, api, seal, 错误载荷, 退出码)。

    load / run / check 共用这一条链，所以「check 通过」严格等于「load 不会被闸门拒绝」：
    check 只是把最后一步 exec 关掉（run_exec=False），判定逻辑不允许有第二份。
    """
    sys.dont_write_bytecode = True

    item = describe(plugin_id)
    if item is None:
        return None, None, None, {
            "ok": False, "error": "plugin_not_found", "plugin_id": str(plugin_id),
            "detail": "plugins/%s 不存在或 id 非法" % plugin_id}, EXIT_CONFLICT

    if item["state"] != "active":
        return item, None, None, {
            "ok": False,
            "error": item["reason"] or "plugin_inactive",
            "detail": item["detail"] or "",
            "plugin_id": item["plugin_id"],
            "plugin": item}, EXIT_DENIED

    seal = None
    if verify:
        seal = verify_seal(root)
        allowed, entry, reason = seal_allows(seal, item["plugin_id"])
        if not allowed:
            payload = {"ok": False,
                       "error": str(reason or "plugin_seal_invalid"),
                       "plugin_id": item["plugin_id"],
                       "plugin": item,
                       "seal": seal,
                       "seal_plugin": entry,
                       "executed": False}
            if entry is None:
                payload["detail"] = ("该插件不在插件封签清单里"
                                     "（新加的插件必须先由人 plugin seal）")
            else:
                payload["detail"] = "封签与当前字节不符（state=%s）" % entry.get("state")
            payload["hint"] = SEAL_HINT
            return item, None, seal, payload, EXIT_DENIED

    if not run_exec:
        return item, None, seal, None, EXIT_OK

    api, code, detail = _exec_plugin(item, locale, root, logger)
    if code:
        payload = {"ok": False, "error": code, "detail": detail,
                   "plugin_id": item["plugin_id"], "plugin": item}
        if seal is not None:
            payload["seal"] = seal
        return item, None, seal, payload, EXIT_DENIED

    return item, api, seal, None, EXIT_OK


def check(root, plugin_id, locale=None, verify=True, logger=None):
    """预演：静态校验 + 封签校验，到 exec 前停下。返回 (载荷, 退出码)。

    不导入插件代码，所以插件里的顶层副作用一条都不会发生；能查出的问题就是
    真正会挡住 load 的那些问题。
    """
    item, _api, seal, error, code = _prepare(root, plugin_id, locale, verify, logger,
                                            run_exec=False)
    if error is not None:
        return error, code

    entry = seal_entry(seal, item["plugin_id"]) if seal is not None else None
    payload = {"ok": True,
               "plugin_id": item["plugin_id"],
               "plugin": item,
               "executed": False,
               "would_load": True,
               "api_version": API_VERSION,
               "sealed": seal is not None,
               "seal_plugin": entry,
               "hint": "静态与封签都过了；真正导入用 ats plugin load %s"
                       % item["plugin_id"]}
    if seal is not None:
        payload["seal"] = seal
    return payload, EXIT_OK


def load(root, plugin_id, locale=None, verify=True, logger=None):
    """导入一个插件并收集它登记的命令。返回 (载荷, 退出码)。"""
    item, api, seal, error, code = _prepare(root, plugin_id, locale, verify, logger)
    if error is not None:
        return error, code

    commands = api.registered()
    payload = {"ok": True,
               "plugin_id": item["plugin_id"],
               "plugin": item,
               "api_version": API_VERSION,
               "locale": api.locale,
               "commands": commands,
               "no_bytecode": bool(sys.dont_write_bytecode),
               "sealed": seal is not None,
               "warnings": ([{"code": "no_registrations",
                              "detail": "插件没有登记任何命令（只作为库存在？）"}]
                            if not commands else [])}
    if seal is not None:
        payload["seal"] = seal
    return payload, EXIT_OK


def run(root, plugin_id, command, argv=None, locale=None, verify=True, logger=None):
    """导入插件并只执行它登记的那一条命令。返回 (载荷, 退出码)。

    执行发生在主进程内 —— 插件代码本来就在主进程里，包一层子进程只会制造
    「以为隔离了」的错觉。插件要跑外部命令得自己 subprocess，那条路照样受
    ATS 的任务与审批链路管辖。
    """
    item, api, seal, error, code = _prepare(root, plugin_id, locale, verify, logger)
    if error is not None:
        return error, code

    handler = api.handler(command)
    if handler is None:
        return ({"ok": False, "error": "command_not_found",
                 "plugin_id": item["plugin_id"], "command": str(command),
                 "available": sorted(api.registered()),
                 "detail": "插件没有登记这条命令",
                 "hint": "先 plugin load 看它到底登记了什么"}, EXIT_USAGE)

    argv = [str(a) for a in (argv or [])]
    try:
        result = handler(api, argv)
    except PluginError as exc:
        return ({"ok": False, "error": "plugin_error", "detail": str(exc),
                 "plugin_id": item["plugin_id"], "command": str(command)}, EXIT_DENIED)
    except SystemExit as exc:
        return ({"ok": False, "error": "plugin_system_exit",
                 "detail": "插件命令调用了 sys.exit(%s)" % exc.code,
                 "plugin_id": item["plugin_id"], "command": str(command)}, EXIT_DENIED)
    except Exception:                    # noqa: BLE001
        return ({"ok": False, "error": "plugin_command_failed",
                 "detail": traceback.format_exc(limit=6).strip(),
                 "plugin_id": item["plugin_id"], "command": str(command)}, EXIT_DENIED)

    return ({"ok": True, "plugin_id": item["plugin_id"], "command": str(command),
             "argv": argv, "locale": api.locale, "result": result,
             "level": item.get("level"),
             "no_bytecode": bool(sys.dont_write_bytecode)}, EXIT_OK)


# ---------------------------------------------------------------- 本地化与骨架

def lang_payload(plugin_id, locale=None, key=None):
    """`plugin lang` 的载荷：可用语言、回退链、某个 key 或整份文案。"""
    item = describe(plugin_id)
    if item is None:
        return None, {"error": "not_found", "plugin_id": str(plugin_id)}, EXIT_CONFLICT
    loc = str(locale or DEFAULT_LOCALE)
    if key:
        value = resolve_string(item["dir"], loc, key)
        return item, {"plugin_id": item["plugin_id"], "locale": loc,
                      "key": key, "value": value}, (
            EXIT_OK if value is not None else EXIT_CONFLICT)
    strings = load_strings(item["dir"], loc)
    if not strings:
        strings = load_strings(item["dir"], DEFAULT_LOCALE)
    return item, {"plugin_id": item["plugin_id"], "locale": loc,
                  "available": localization_locales(item["dir"]),
                  "strings": strings}, EXIT_OK


def render_lang(payload):
    lines = ["locale : %s" % payload.get("locale"),
             "avail  : %s" % (", ".join(payload.get("available") or []) or "(none)")]
    for key, value in sorted((payload.get("strings") or {}).items()):
        lines.append("  %-32s %s" % (key, value))
    if not payload.get("strings"):
        lines.append("  (该语言没有 strings.json)")
    return lines


_PLUGIN_JSON_TEMPLATE = """{
  "plugin_id": "{pid}",
  "name": "{name}",
  "version": "0.1.0",
  "description": "{name} plugin",
  "level": "L2",
  "allow_language": ["python"],
  "runtime_library": [],
  "injection": "Injection.py",
  "extend": ""
}
"""

_INJECTION_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Injection.py — {pid} 的固定导入入口。

本文件在封签通过后由 ATS 主进程 exec（不是子进程、没有沙箱），所以：

  * 顶层只做登记，不做耗时 / 危险动作 —— 导入期抛异常等于整个插件不可用；
  * 需要 ATS 能力只用 `import ats_api`（ATS 预注入的窄接口），不要 import scripts.*；
  * 任何外部命令都自己 subprocess 起来，那条路照样受 ATS 的任务与审批管辖。

两种登记写法，二选一即可：
    register(api)           ATS 在 exec 完会调用它
    ats_api.current()       想在模块顶层登记时用它
"""
import ats_api


def register(api):
    api.command("{pid}.hello", cmd_hello, help_text="说一句 hello", level="L2")


def cmd_hello(api, argv):
    name = api.tr("hello.target", name=(argv[0] if argv else "world"))
    api.log("hello %s" % name)
    return {"ok": True, "plugin": api.plugin_id, "said": name}
'''

_STRINGS_EN = """{
  "hello.target": "Hello, {name}!"
}
"""

_STRINGS_ZH = """{
  "hello.target": "你好，{name}！"
}
"""


def _plugin_name(plugin_id):
    words = re.split(r"[-_]+", str(plugin_id))
    return " ".join(w.capitalize() for w in words if w) or str(plugin_id)


def scaffold(plugin_id, force=False):
    """生成最小插件骨架（plugins/<id>/）。返回 (载荷, 退出码)。

    生成物就是一份「封签之后拿来就能跑」的插件：plugin.json + Injection.py +
    Localization/{en,zh}/strings.json。**不生成 __pycache__**，本层全链路禁字节码。
    """
    text = str(plugin_id or "").strip()
    if not SAFE_ID_PATTERN.match(text):
        return ({"error": "bad_plugin_id", "plugin_id": text,
                 "hint": "插件 id 只允许字母、数字、下划线、短横线，长度 1-64"},
                EXIT_USAGE)
    target = plugins_root() / text
    if target.exists() and not force:
        return ({"error": "plugin_exists", "plugin_id": text,
                 "dir": str(target),
                 "hint": "目录已存在；确认要覆盖骨架加 --force（只覆盖模板文件）"},
                EXIT_CONFLICT)

    name = _plugin_name(text)
    files = {
        target / MANIFEST_NAME: _PLUGIN_JSON_TEMPLATE.replace("{pid}", text)
                                                  .replace("{name}", name),
        target / DEFAULT_INJECTION: _INJECTION_TEMPLATE.replace("{pid}", text),
        target / LOCALIZATION_DIRNAME / "en" / STRINGS_NAME: _STRINGS_EN,
        target / LOCALIZATION_DIRNAME / "zh" / STRINGS_NAME: _STRINGS_ZH,
    }
    written = []
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            written.append(str(path))
    except OSError as exc:
        return ({"error": "scaffold_write_failed", "plugin_id": text,
                 "detail": "%s: %s" % (type(exc).__name__, exc),
                 "written": written}, EXIT_USAGE)

    return ({"ok": True, "plugin_id": text, "dir": str(target),
             "files": written,
             "hint": "骨架建好后由人封签：python scripts/ats_guard.py plugin seal "
                     "--confirm --passphrase-stdin",
             "next": ["ats plugin check %s" % text,
                      "ats plugin load %s" % text,
                      "ats plugin run %s --command %s.hello" % (text, text)]},
            EXIT_OK)
