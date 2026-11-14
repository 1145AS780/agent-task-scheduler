# API 指南 — agent-task-scheduler v3.3.0-R0

本文分两半：**前半是对外协议**（外部人类 / Agent 怎么调、怎么读 JSON、怎么处理退出码与审批），
**后半是内部扩展点**（新增策略键、审批模式、子命令、UI 动作时要同步改哪些地方）。

> 阅读方式：不需要从头读。调用方只看第 1–9 节；要改代码时直接跳第 10–12 节。
> 本文只写**已在代码中确认存在**的行为，未定案的内容集中放在第 13 节，不与现状混写。
>
> **版本锚点**：`240e366`（扩展插件接线 + 一键设置 + 看门狗准入）。核对日期 2026-11-14，
> 核对时工作树干净。代码变动后先比对 `git log --oneline -5` 与本节，再读第 13 节。

---

## 1. 调用约定

1. **stdout 只有 JSON**（`list` / `logs` 的人类可读模式除外），诊断信息一律走 stderr 或
   `<root>/logs/`。因此可以直接 `| ConvertFrom-Json` / `json.loads(stdout)`。
2. **一次调用一条命令**，一次只解析一个 JSON。不要在一次 `run` 的返回值里期待后续状态。
3. **命令原文就是主键**：`--command` 会原样写进 `task.json`、事件、以及审批绑定文本。
   多一个空格、尾随空格、变量展开后的差异，都会让审批 `mismatch`。
4. **状态目录三级定位**：`--root <dir>` > 环境变量 `ATS_ROOT` > `<ats 本体目录>/.ats`（`ats_ui.py` 例外，默认 `<cwd>/.ats`）。
   `--root` 是全局参数，必须写在子命令**之前**。

```powershell
python scripts/ats.py --root D:\run\state status build1     # 正确
python scripts/ats.py status build1 --root D:\run\state     # 报错：status 没有 --root
```

### 1.1 看门狗准入（所有命令的前置门）

**每一个子命令在执行前都要先过 `guard_handshake`**，顺序固定，任一步失败都 fail-closed：

```
--skip-guard 的 L3 审批闸门 → 自检 ats_guard.py 自身摘要 → ats_guard.py verify
→ 持只读句柄 → 冻结策略快照
```

失败时退出码 `4`，信封为 `{"error": "guard_check_failed", "detail": {"stage": ..., "hint": ...}}`。
**这不是可配置开关**：`policy.json` 里没有关闭它的键，全新 root 第一次调用就会撞上：

| `verify` 报的错 | 含义与下一步 |
| --- | --- |
| `manifest_missing` + `next_action=init` | 尚无信任根。由人先 `ats_guard.py init --confirm`，再 `seal --confirm`，两步都要口令 |
| `manifest_missing`（无 `init`） | 清单缺失但密钥还在。由人 `seal --confirm` 重封签，口令须与 init 时一致 |
| `next_action=restore_key` | 密钥文件丢失，seal 修不好（seal 要口令，口令要密钥）。从 git 恢复两个文件，或人工删掉后重来 |
| 其他 | 核心脚本摘要与清单不符。确认改动可信后由人 `seal --confirm` 重封签 |

**`--skip-guard` 不是免检开关**：它本身要求一条绑定 `__ats_guard_skip__` 的**已批准 L3**
记录，先 `approve` 再带 `--guard-approval <approval_id>` 重跑，否则返回
`guard_skip_approval_required` + `approval_id`（退出码 `4`）。而且它**不降低任何命令的执行
等级** —— 本次进程里所有等级下限抬到 `guard_skip_level`（默认 `L3`）。它只供人类紧急调试。

三个跨子命令的全局开关 `--skip-guard`、`--guard-approval <id>`、`--passphrase-stdin`
在 `argparse` **之前**就被剥离，所以**位置自由**（写在子命令前后都行，实测两种位置等价）。
但 `--root` 不是这样：它由 `argparse` 解析，**必须写在子命令之前**，写在后面对应子解析器
不认，直接 `unrecognized arguments`。


---

## 2. 退出码

| 码 | 含义 | 典型 `error` |
| --- | --- | --- |
| `0` | 成功 | — |
| `2` | 参数错误 / 缺少确认 | `missing_command`、`bad_level`、`bad_cwd`、`bad_timeout`、`unknown_keys` |
| `3` | 不存在 / 状态冲突 | `not_found`、`task_exists`、`state_conflict` |
| `4` | 权限不足 / 被闸门拦截 | `approval_required`、`denied_policy`、`denied_mode`、`denied_injection`、`insufficient_level` |

**判断成败优先看 `error` 字段与退出码，不要只看有没有输出。**
`approve --all` 是批量语义，恒返回 `0`，必须逐项检查 `approvals[].ok`。

---

## 3. 错误信封

CLI（`scripts/ats.py`、`scripts/ats_plan.py`）：

```json
{ "error": "approval_required", "task_id": "build1", "level": "L2",
  "effective_level": "L2", "actor_level": "L3",
  "approval_id": "ap-1a2b3c4d", "approval_error": "missing",
  "preflight": { "verdict": "...", "status": "..." },
  "resume": "python ats.py run build1 --approval <approval_id>" }
```

- 成功时不一定有 `ok` 字段，按命令文档读具体键。
- 失败时**一定有** `error`；`approval_required` 还会带 `approval_id` 与 `resume`。

HTTP 控制台（`scripts/ats_ui.py`）统一为 `{"ok": bool, ...}`，失败带 `error` 与 `message`：

```json
{ "ok": false, "error": "bad_action", "message": "unknown action: xxx" }
```

---

## 4. Task 层命令（`scripts/ats.py`）

| 命令 | 作用 | 主要返回键 | 备注 |
| --- | --- | --- | --- |
| `create <id>` | 登记任务，不执行 | `task`, `created` | 支持 `--spec`、`--force`、`--level`、`--env`、`--cwd`、`--timeout`、`--no-shell` |
| `pack` | 加载指令包（多条命令一次性登记） | `pack_id`, `count`, `executed=false` | `executed` 恒为 false，不执行 |
| `run <id>` | 启动任务 | `task` 或 `error` | 被闸门拦截时 `error=approval_required` + `approval_id` + `resume` |
| `download` | 创建并启动下载任务 | `task` | 以子进程方式调 `ats_download.py` |
| `torrent` | 创建并启动种子下载 | `task` | 依赖 `aria2c`，缺失时 `aria2_not_found` |
| `check [id]` | 同步运行中任务状态 | 任务状态 + `next_poll` | 无参时批量；`next_poll` 决定下次轮询间隔 |
| `list` | 列出任务 | 数组 / 人类可读 | `--status` 过滤 |
| `show <id>` | 任务详情 + 日志尾部 | `task`, `stdout`, `stderr` | `--lines N` |
| `status <id>` | 精简状态 | `task` | 最常用 |
| `stop <id>` | 停止任务 | `task` | 走 `gate_operation`，等级 L1 |
| `delete <id>` | 删除任务 | — | 走 `gate_operation`，等级 L2 |
| `approve <id>...` / `--all` | 批准或拒绝（要口令） | `approvals[]`, 每项含 `ok` | `--reject`、`--note`、`--passphrase-stdin` |
| `approval prune` | 清理已决议审批 | 计数 | `--dry-run`、`--older-than SEC` |
| `events` | 消费事件流 | 事件数组 | `--lines`、`--json` |
| `logs` | 调度器日志 | 文本 / JSON | `--plain`、`--json` |
| `policy` | 查看 / 修改有效策略 | 策略 dict | `--json`；`--set key value`（可重复） |
| `extend <sub>` | 扩展插件：`list` / `help` / `check`（预演）/ `run`（真执行） | `extends` / `gate_text` / `task` | 见 §4.1；`run` 复用普通 Task 全链路 |
| `supervise` | **内部命令**，由 `run` 派生 | — | 不要手动调用 |

任务记录（`tasks/<id>/task.json`）与本文协议相关的字段：`id`、`command`、`level`、`status`、
`created_at`、`instance_id`。其余字段以实际文件为准，本文不逐一承诺。

### 4.1 扩展插件（`extend`）

插件是 `<ATS_ROOT>/extend/<extend_id>/` 下一个自包含目录，用 `instruction.json` 声明入口与指令。
**发现层只读**：`ats_extend.py` 只读 `instruction.json`、查文件是否齐全，从不导入插件代码；
执行不另开路径，`extend run` 把 `gate_text` 与 `argv` 交给普通 Task 链路。

| 子命令 | 是否执行 | 主要返回键 | 备注 |
| --- | --- | --- | --- |
| `list` | 否 | `extend_dir`、`extends[]`、`count`、`active`、`inactive`、`skipped_symlinks`、`level_floor` | `--json`；无插件时返回空数组而不是报错 |
| `help <id>` | 否 | `extend` | 人类可读，`--json` 给完整 item |
| `check <id>` | **否**（纯预演） | `gate_text`、`level`、`level_note`、`gate{}`、`preflight{}`、`executed=false` | 不写审批、不碰租约、不在沙箱真跑 |
| `run <id>` | **是** | `task` / `approval_required` + `extend{}` | 支持 `--action`、`--arg`（可重复）、`--wait`、`--id`、`--force`、`--timeout` |

`extends[]` 单项字段：`extend_id`、`folder`、`dir`、`state`、`reason`、`detail`、
`dis_name`、`desc`、`version`、`language`、`entry_point`、`entry_path`、`declared_level`、
`level`、`level_note`、`runtime_library`、`commands{}`、`problems[]`、`warnings[]`。

`check` 的 `gate{}` 是**预演结果**，字段与 `permission_gate` 同形：`required_level`、
`actor_level`、`policy_max`、`effective_level`、`hard_floor`、`approval_mode`、`whitelist`、
`would`、`reason`、`lease_checked`、`approval_checked`。`preflight{}` 里 `sources[]` 每项是
`{file, ok}` —— 入口绝对路径进了 `gate_text`，所以 python 入口源码会被静态读到。

**四条不可动摇的约定**：

1. **`gate_text` 同源**。`run` 建出的 `task.command` 与 `check` 报出的 `gate_text` 逐字相同，
   两者都由 `extend_argv()` 加引号投影而来。审批按文本绑定，改一处就会 `mismatch`。
2. **argv 不经 shell**。`task` 里同时有 `argv`（列表）与 `shell: false`，supervisor 用
   `Popen([...argv])`。`--arg "a & b; c"` 到达插件时仍是这一整串，不会变成第二条命令。
3. **等级不由插件决定**。`EXTEND_LEVEL_FLOOR = "L2"`：声明低于 L2 一律抬到 L2，
   缺失或非法按 `L3`；策略 `max_level` 低于 L2 时直接 `denied_policy`。这是常量，不是策略键。

4. **审批会钉住入口文件**。`run` 被拦时返回的 `approval.artifacts[]` 带入口文件的
   `{path, sha256}` —— 审批绑定的是 `gate_text`，同时留一份入口摘要。审批通过后
   `resume` 走的是**普通** `run <task_id> --approval <id>`，不是 `extend run`；
   之后 `show` / `stop` / `delete` 也都按普通 Task 命令操作。

```json
{ "error": "approval_required", "task_id": "t-5a9267", "level": "L3",
  "approval_id": "ap-63454ff0", "approval_error": "missing",
  "resume": "python ats.py run t-5a9267 --approval <approval_id>",
  "extend": { "extend_id": "hello", "action": "run", "level": "L3",
              "level_note": "missing_or_invalid_level",
              "gate_text": "<python 解释器绝对路径> <入口绝对路径> --action run" },
  "approval": { "command": "…与 extend.gate_text 逐字相同…",
                "source": "run",
                "artifacts": [{ "path": "…/extend/hello/__init__.py", "sha256": "1ba8b113…" }] } }
```

**Windows 陷阱：`instruction.json` 不能带 BOM**。用 `Set-Content -Encoding UTF8` 写会带上
BOM，而 `ats_extend._read_instruction` 按无 BOM 解析，结果是
`state=inactive`、`reason=missing_or_invalid_instruction`、`detail` 含
`Unexpected UTF-8 BOM`，插件静默不执行。写文件时用无 BOM 的 UTF-8：
`[IO.File]::WriteAllText($p, $json, (New-Object Text.UTF8Encoding $false))`。

**状态与 fail-closed**：`state` 只有 `active` / `inactive`。目录名与 `extend_id` 不一致、
入口缺失、入口落在插件目录之外、id 不满足 `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`，
任一命中即 `inactive`，而 **inactive 插件不建 Task、不执行**。符号链接目录一律跳过并计入
`skipped_symlinks`，不跟进去。

**参数校验**（`check` / `run` 共用 `check_args`）：指令名须匹配 `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`；
参数**不能含双引号、`\r` 或 `\n`**（会让门禁文本无法确定，而门禁文本又是审批绑定依据），
单参数上限 1024 字符。违规直接拒绝，不做转义尝试。

**参数里的危险命令抬的是门禁层，不是插件层**。`--arg "rm -rf /tmp/x"` 会让 `gate.hard_floor`
与 `gate.required_level` 抬到 `L3`，但 `extend.level` 仍是插件自己声明的 `L2` —— 这两个数
**不一样，且本来就该不一样**：`level` 描述插件身份，`gate.*` 描述这一次调用的风险。
看到 `level=L2` 就以为不用审批，是读错字段。

**`extend` 专属错误码**：

| `error` | 退出码 | 含义 |
| --- | --- | --- |
| `extend_module_missing` | `2` | `ats_extend.py` 缺失或无法导入 |
| `bad_action` / `bad_extend_arg` / `bad_timeout` | `2` | 指令名非法 / 参数含引号或换行或超长 / 超时非整数 |
| `not_found` | `3` | 该 `extend_id` 不存在 |
| `unknown_action` | `2` | 指令名不在 `instruction.json` 的 `commands` 里 |
| `missing_entry_point` | `3` | 入口读不到。**通常轮不到它**：入口缺失时扫描阶段就判 `inactive`，先返回 `inactive_extend`；这条只在「扫描后文件被删」的竞态里出现 |
| `task_exists` | `3` | `--id` 指定的任务已存在且未带 `--force` |
| `inactive_extend` | `4` | 插件非 active；带 `problems` 与 `would=deny` |

`check` 的输出里恒有 `executed=false` —— **不要**把 `gate.would=allow` 当成已经跑过。
`run` 成功后返回的是普通 Task 信封（`task` / `next_poll`）外加一个 `extend{}`；
注意落盘的 `task.json` 里 `extend` **不含** `gate_text`，`gate_text` 存在 `command` 字段里，
返回信封才额外带一份。

预检覆盖面有个边界：`gate_text` 带入口绝对路径，所以 `ats_preflight` 能静态读到 **python**
入口源码；node / binary 插件在这段文本里没有可分析的源码，预检对它们只剩 shell 文本层。

---

## 5. Plan 层命令（`scripts/ats_plan.py`）

| 命令 | 作用 |
| --- | --- |
| `submit_plan` | 提交一次性计划（stages → steps） |
| `submit_schedule` | 提交周期计划 |
| `run_plan <ident>` | 执行计划；被拦时返回 `status=paused` + `approval` |
| `resume <ident>` | 从暂停处继续；同时接受 schedule_id 与 plan_id |
| `check_schedule <sid>` | 驱动周期计划（按 `next_run` 到期） |
| `pause` / `cancel` | 暂停 / 取消计划或周期 |
| `resume_schedule` | 恢复已暂停的周期计划，保留 `next_run` |
| `rerun_task` | 重跑某一步并重新武装下游 |
| `rerun_plan` | 整个 Plan 重置为 pending |
| `list` / `show` | 查看计划 |
| `delete` | 删除计划 / 周期 |
| `history` / `prune` | 运行快照与清理 |

Step 的 `level` 默认 `L1`；`level` 必须是 `L0`–`L3`。Step 权限判定统一走
`ats.permission_gate`（`ats_plan.ensure_step_permission`），与 Task 层同一套闸门。

---

## 6. 审批协议

标准三段式，不要跳步：

```powershell
$run = python scripts/ats.py run deploy1 | ConvertFrom-Json
if ($run.error -eq "approval_required") { $apId = $run.approval_id }

python scripts/ats.py approve $apId

python scripts/ats.py run deploy1 --approval $apId --wait
```

### 6.1 审批记录字段（`approvals/<id>.json`）

```json
{
  "id": "ap-1a2b3c4d",
  "command": "npm run deploy",
  "required_level": "L2",
  "status": "pending",
  "created_at": 1760000000,
  "created_iso": "2026-11-09T10:00:00",
  "expires_at": 1760086400,
  "resolved_at": null,
  "resolved_iso": null,
  "resolved_by": null,
  "note": null,
  "source": "run",
  "schedule_id": null,
  "plan_id": null,
  "stage_id": null,
  "step_id": null,
  "task_id": "deploy1",
  "task_instance": "…",
  "task_created_at": 1760000000,
  "resume": "python ats.py run deploy1 --approval <approval_id>",
  "preflight": { "verdict": "…", "status": "…" }
}
```

`status` 取值：`pending` / `approved` / `rejected` / `expired` / `denied_injection`。
`expires_at=null` 表示不过期（`approval_ttl_seconds <= 0`）。

### 6.2 `approval_check` 的错误枚举

`missing` / `pending` / `rejected` / `expired` / `mismatch` / `denied_injection`。
其中 `mismatch` 表示**审批存在且已批准，但命令原文不一致**——不要重试，先核对字符串。

### 6.3 批准者身份

`approve` 时校验 `rank(actor_level) >= rank(required_level)`，不足返回
`ok=false` + `insufficient_level` + 退出码 `4`。此时不要重试同一条命令，先提升
`ATS_ACTOR_LEVEL`。

口令关在身份关之前：没有正确口令直接返回 `passphrase_required`（退出码 `4`），
不会进入身份比较。口令只从 `--passphrase-stdin` 或终端交互读，不读 argv。

---

## 7. 权限门现状

### 7.1 `permission_gate`（Task / Step 路径）

判定顺序（`scripts/ats.py`）：

```
0. 硬下限        hard_floor_level 命中 → required 至少抬到 L3/L2（压过传入等级）
1. 白名单        allow_rules 命中且未触发硬下限 → allowed, reason="whitelist"
2. 策略上限      rank(required) > rank(max_level) → denied, reason="denied_policy"
3. 取审批模式    approval_mode_for(pol, required)
                 （approval_mode_by_level[level] > approval_mode > "ask"）
4. deny-only     mode == "deny-only" → denied, reason="denied_mode"（不产生可批准记录）
5. auto 直通     mode == "auto" 且 rank(required) <= rank(actor) → allowed, reason="level"
6. 存活租约      lease_autonomy 命中 → allowed, reason="lease_autonomy"（写留痕审批）
7. 审批检查      approval_check 通过 → allowed, reason="approval"
8. 兜底          denied, reason="approval_required"（调用方据此创建审批）
```

**关键点**：`actor_level` 只在第 6 步生效。`approval_mode=ask` 的等级**不看身份**，
即使 `actor_level=L3` 也要审批。

### 7.2 `gate_operation`（操作型命令路径）

`stop` / `delete` / `pause` / `cancel` / `rerun_task` / `prune` / `resume_schedule` 走这一条：

```
1. 白名单（仅 auto 档）→ 2. 策略上限 → 3. 等级 <= actor_level 直接放行（仅 auto 档）
→ 4. 存活租约 → 5/6. 审批
```

**它读取 `approval_mode`**：`auto` 档才允许白名单与身份直通；`ask` / `deny-only` 档一律走审批或拒绝。

### 7.3 默认值

| 常量 / 键 | 默认值 | 位置 |
| --- | --- | --- |
| `DEFAULT_MAX_LEVEL` | `L3` | `ats.py` 常量区 |
| `DEFAULT_ACTOR_LEVEL` | `L3` | `ats.py` 常量区 |
| `DEFAULT_TASK_LEVEL` | `L1` | `ats.py` 常量区 |
| `approval_mode` | `ask` | `POLICY_DEFAULTS` |
| `approval_mode_by_level` | `{L0: auto, L1: auto, L2: ask, L3: ask}` | `POLICY_DEFAULTS` |
| `lease_autonomy.enabled` | `false` | `DEFAULT_LEASE_AUTONOMY` |
| `approval_ttl_seconds` | `86400` | `POLICY_DEFAULTS` |

策略来源优先级：**默认值 < `<root>/policy.json` < 环境变量**。

---

## 8. 事件与日志

| 落地位置 | 内容 |
| --- | --- |
| `<root>/events.jsonl` | Task 层事件（`task_created` / `task_start` / `approval_created` / `task_blocked` / `preflight_completed` …） |
| `<root>/schedules/<sid>/events.jsonl` | Plan 层事件 |
| `<root>/tasks/<id>/stdout.log` / `stderr.log` | 子进程输出 |
| `<root>/logs/` | 调度器自身日志 |
| `<root>/approvals/` | 审批记录 |

事件是**追加写**，没有删除接口；需要归档时自行截断或迁移。

---

## 9. HTTP 控制台（`scripts/ats_ui.py`）

启动参数：`--root`、`--host`（默认 `127.0.0.1`）、`--port`（默认 `8765`）、`--mcp-port`、
`--open`、`--ensure`、`--status`、`--stop`。

### 9.1 GET

| 路径 | 返回 |
| --- | --- |
| `/api/ping` | 服务信息（`server`、`version`、`pid`、`root`、`port`、`started_at`） |
| `/api/state` | 面板总状态（任务 / 计划 / 审批 / 策略） |
| `/api/task?id=` | 单任务详情 |
| `/api/plan?ident=` | 计划详情 |
| `/api/log?task=&file=&lines=` | 日志尾部 |
| `/api/events` | 最近 80 条事件 |
| `/api/policy` | 有效策略 |

### 9.2 POST `/api/action`

请求体为 `{"action": "...", ...}`。已注册动作：

`mcp.start` / `mcp.stop` / `task.run` / `task.retry` / `task.stop` / `task.delete` /
`task.create` / `pack.create` / `approval.resolve` / `policy.set` / `plan.submit` /
`plan.run` / `plan.resume` / `plan.pause` / `plan.cancel` / `plan.unpause` /
`plan.rerun` / `plan.delete` / `schedule.tick`

未知 action 返回 `{"ok": false, "error": "bad_action"}`，HTTP 400。

`approval.resolve` 与 `policy.set` 额外要求请求体带 `passphrase`：缺失时控制台直接返回
`{"ok": false, "exit_code": 4, "result": {"error": "passphrase_required"}}`，不启动子进程；
带值时控制台只把它写进子进程 stdin 并附 `--passphrase-stdin`，**绝不拼进 argv**，
请求体也不写日志、不落盘。口令错误由 `ats.py` 判为 `passphrase_mismatch`，控制台原样回传。

---

## 10. 模块地图

```
ats.py                 Task 层 CLI + 策略 + 权限门 + 审批 + 事件
  ├─ ats_extend.py    扩展插件发现层（只读 instruction.json；执行交给 Task 链路）
  ├─ ats_download.py   下载 worker（由 run 派生的子进程，非 import）
  ├─ ats_torrent.py    种子下载（aria2c 探测与调用）
  └─ ats_preflight.py  预演模块（可选导入；缺失时退化为纯权限门）

ats_plan.py            Plan / Schedule 层 CLI，复用 ats.py 的 permission_gate
ats_ui.py              本地 HTTP 控制台 + 静态页面 + 动作分发
ats_mcp.py             本地 MCP stdio 端点（task_* / plan_* / schedule_* / approval_resolve）
ats_mcp_http.py        可启停的 MCP HTTP 端点
```

数据流（一次 `run`）：

```
policy() → permission_gate() → [denied | approved | approval_required]
                              ↓ approval_required
                        create_approval() → announce_approval() → events.jsonl
                              ↓ 人类 approve
                        approval_check() → 通过 → 启动子进程
```

---

## 11. 扩展操作清单

> 这是本文的核心。**新增任何能力，按对应清单逐项打勾**，漏一步就会出现「代码生效但文档/UI/回归没跟上」。

### A. 新增一个策略键

1. `POLICY_DEFAULTS` 加键与默认值（`ats.py` 常量区）
2. `POLICY_ENV` 加环境变量映射
3. `INT_POLICY_KEYS` / `BOOL_POLICY_KEYS` 归类（决定 `policy --set` 的转换方式）
4. `policy()` 里补归一逻辑（字符串、列表、dict 都要能容错）
5. `cmd_policy` 的 `--set` 分支（若需要特殊解析，如 `approval_mode_by_level` 的 JSON）
6. `SKILL.md` 策略键表
7. `references/data-model.md` 策略字段表
8. `references/permissions.md` 策略来源表
9. UI：`renderPolicyControls()` + `policy.set` 动作 + 页面对应控件
10. 回归：`tests/selftest.py` 加一条「默认值 + 覆盖值」用例

**漏掉的后果**：`--set` 返回 `unknown_keys`；或键能写进去但读不出来；或 UI 保存后被覆盖。

### B. 新增一种审批模式

1. `APPROVAL_MODES` 加枚举
2. `normalize_approval_mode()` 的容错（非法值回落）
3. `permission_gate()` 加分支（注意：必须放在 `auto 直通` 之前或之后，顺序即语义）
4. `reason` 取值表：`permission_gate` docstring + `SKILL.md` + `error-taxonomy.md`
5. 调用方（`ats.py` 的 `run`、`ats_plan.py` 的 `ensure_step_permission`）对新增 `reason` 的处理
6. 决定是否同步 `gate_operation`（**当前它不读模式**，不同步就必须在文档写明）
7. UI 模式下拉框（全局 + 每等级）
8. 回归：三档模式各一条用例

### C. 新增一个 Task 子命令

1. `build_parser()` 加 `add_parser`（**必须写 `help=`**）
2. 实现 `cmd_xxx(root, args)`
3. `main()` 分发分支
4. 退出码：参数错 `2`、不存在 `3`、权限拒绝 `4`
5. 若涉及破坏性操作 → 决定走 `permission_gate` 还是 `gate_operation`，并写明等级
6. 事件：成功/失败各发一条
7. `SKILL.md` 命令表 + `references/command-guide.md`
8. 回归用例

### D. 新增一个 Plan 子命令

同上，另加：`ats_plan.py` 的 `build_parser()`、`SKILL.md` Plan 段、
`references/state-machine.md`（若引入新状态迁移）。

### E. 新增一个操作型命令（走 `gate_operation`）

1. 定级（L1/L2/L3）并写进 `error-taxonomy.md` 命令等级表
2. `gate_operation(root, level, label, ...)`，`label` 必须**稳定可复现**（它就是审批绑定文本）
3. 被拦时返回 `approval_required` + `approval_id`，不要自造错误码
4. 无需额外处理模式：`gate_operation` 已读 `approval_mode`，只有 `auto` 档才允许白名单与身份直通

### F. 新增一个 UI 动作

1. `dispatch()` 加 `action` 分支
2. 前端按钮 + `fetch('/api/action', ...)`
3. 返回统一 `{"ok": bool, ...}`
4. 若动作会改状态，页面刷新走 `/api/state`

### G. 新增一个事件类型

1. `log_event(root, "xxx", **fields)`
2. `references/data-model.md` 事件表
3. `ats_ui.py` 的事件渲染（若需人类可读）

### H. 新增一个错误码 / 状态

1. 常量区或字符串字面量统一定义
2. `references/error-taxonomy.md`（含 `approval_error` 枚举）
3. 退出码映射
4. `SKILL.md` 排障段

### I. 新增一个 MCP 工具

1. `ats_mcp.py` 工具表加 `name` + `inputSchema`
2. 映射到既有 CLI 逻辑，不要复制第二套权限判断
3. `ats_mcp_http.py` 确认协议层同步

### J. 修改扩展插件层（`ats_extend.py`）

> 与 A–I 不同，这一层**不改调度器本体**：插件由使用者自己写，ATS 只负责发现与执行。
> 但只要动了这一层，就同时牵动「审批绑定文本」，所以比普通改动更容易出事。

1. **`instruction.json` 契约变更** → `inspect_plugin()` 的校验与 `problems` 文案
2. **入口解析变更** → `_check_entry()` 的越界判断（入口必须落在插件目录内，否则 `inactive`）
3. **argv 组成变更** → 只改 `extend_argv()`，**不要手拼 `gate_text`**：后者必须是前者的加引号投影，
   否则 `check` 报的文本与 `run` 实际执行的文本分叉，审批必 `mismatch`
4. **等级规则变更** → `EXTEND_LEVEL_FLOOR` / `EXTEND_LEVEL_INVALID` / `normalize_level()`
   （三者是常量，不是策略键；若要改成可配，按清单 A 走一遍）
5. **新增子命令** → `build_parser()` 的 extend 段 + `cmd_extend()` 分发 + 退出码（`2` 参数 / `3` 不存在 / `4` 拒绝）
6. **文档四处同步**：`SKILL.md` 插件段、`references/command-guide.md` §11.13、
   `references/storage-layout.md` 的 `extend/<id>/instruction.json` 行、**本文 §4.1**
7. **回归**：`tests/selftest.py` 的 `extend_*` 用例族 —— `check` 不执行、`run` 真执行且
   `task.command` 等于 `gate_text`、参数不进 shell、非 active 一律拒绝

**漏掉的后果**：改了拼文本的方式却没同步 `extend_argv` → 预演与执行文本分叉；
改了校验却没更新 `problems` → `inactive` 的原因和实际不符；
只改代码不改本文 §4.1 → 下一批改动的人照旧文档接线，重复踩坑。

---

## 12. 改完必须跑的回归

```powershell
python tests/selftest.py          # 核心行为
python tests/readme_parity.py     # README.md 与 README.zh-CN.md 的结构/技术内容对齐
```

注意：`readme_parity.py` 只做**双语 README 对齐**（标题层级、代码块、命令行、链接目标），
**不校验默认值是否与代码一致**。所以改默认值时它会保持绿色，文档漂移不会被它发现——
这类检查目前没有自动化，只能人工对照 `POLICY_DEFAULTS` 与常量区。

---

## 13. 未定案 / 待补

以下均为**已确认的现状与文档不一致**，尚未决定修哪一侧，故只记录、不声称已解决：

1. **`approval_mode` / `approval_mode_by_level` / `preflight_*` 等新策略键尚未补进**
   `SKILL.md` 策略键表与 `references/data-model.md` 字段表。
   **触发**：两处补齐后删本条。
2. **`denied_mode` / `denied_injection` / `override_required` / `note_required` /
   `injection_denied` / `already_denied_injection` 尚未补进** `references/error-taxonomy.md`。
   **触发**：错误码表补齐后删本条。
3. **`--level` 的 argparse 没有 `help` 文本**，`create --help` 看不到默认 L1 与审批语义。
4. **`ats_preflight.py` 目前无自动化测试覆盖**（原 `tests/test_preflight.py` 已备份后移除）。

---

## 附录：其余脚本速查

| 脚本 | 用途 | 调用方式 |
| --- | --- | --- |
| `ats_download.py` | 下载 worker（断点续传、限速、校验） | 由 `ats.py download` 作为子进程拉起 |
| `ats_torrent.py` | 种子下载，探测 `aria2c` | 由 `ats.py torrent` 调用 |
| `ats_preflight.py` | 静态审查 + 诱饵试跑 + 决策 | 被 `ats.py` 可选导入，缺失即退化 |
| `ats_mcp.py` | MCP stdio 端点 | 直接运行，按 MCP 协议通信 |
| `ats_mcp_http.py` | MCP HTTP 端点 | 直接运行，可启停 |
| `ats_ui.py` | 本地控制台 | 见第 9 节 |

以上脚本的完整参数以各自 `--help` 为准，本文只保证「谁调谁、大方向是什么」。