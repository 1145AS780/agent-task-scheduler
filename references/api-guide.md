# API 指南 — agent-task-scheduler

本文分两半：**前半是对外协议**（外部人类 / Agent 怎么调、怎么读 JSON、怎么处理退出码与审批），
**后半是内部扩展点**（新增策略键、审批模式、子命令、UI 动作时要同步改哪些地方）。

> 阅读方式：不需要从头读。调用方只看第 1–9 节；要改代码时直接跳第 10–12 节。
> 本文只写**已在代码中确认存在**的行为，未定案的内容集中放在第 13 节，不与现状混写。
>
> **版本锚点**：`540b004`（刀2 + 刀3）＋工作区未提交改动（`hard_floor` 硬下限 / 白名单例外 /
> `whitelist_ignored`）。核对日期 2026-11-11。代码变动后先比对 `git log --oneline -5` 与本节，再读第 13 节。

---

## 1. 调用约定

1. **stdout 只有 JSON**（`list` / `logs` 的人类可读模式除外），诊断信息一律走 stderr 或
   `<root>/logs/`。因此可以直接 `| ConvertFrom-Json` / `json.loads(stdout)`。
2. **一次调用一条命令**，一次只解析一个 JSON。不要在一次 `run` 的返回值里期待后续状态。
3. **命令原文就是主键**：`--command` 会原样写进 `task.json`、事件、以及审批绑定文本。
   多一个空格、尾随空格、变量展开后的差异，都会让审批 `mismatch`。
4. **状态目录三级定位**：`--root <dir>` > 环境变量 `ATS_ROOT` > `<当前工作目录>/.ats`。
   `--root` 是全局参数，必须写在子命令**之前**。

```powershell
python scripts/ats.py --root D:\run\state status build1     # 正确
python scripts/ats.py status build1 --root D:\run\state     # 报错：status 没有 --root
```

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
| `approve <id>...` / `--all` | 批准或拒绝 | `approvals[]`, 每项含 `ok` | `--reject`、`--note` |
| `approval prune` | 清理已决议审批 | 计数 | `--dry-run`、`--older-than SEC` |
| `events` | 消费事件流 | 事件数组 | `--lines`、`--json` |
| `logs` | 调度器日志 | 文本 / JSON | `--plain`、`--json` |
| `policy` | 查看 / 修改有效策略 | 策略 dict | `--json`；`--set key value`（可重复） |
| `supervise` | **内部命令**，由 `run` 派生 | — | 不要手动调用 |

任务记录（`tasks/<id>/task.json`）与本文协议相关的字段：`id`、`command`、`level`、`status`、
`created_at`、`instance_id`。其余字段以实际文件为准，本文不逐一承诺。

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

---

## 10. 模块地图

```
ats.py                 Task 层 CLI + 策略 + 权限门 + 审批 + 事件
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
