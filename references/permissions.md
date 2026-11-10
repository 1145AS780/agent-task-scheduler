# 权限与审批 — agent-task-scheduler v2.9.1

本文描述 L0-L3 分级、策略上限、白名单、审批闭环与存活租约免审。实现见 `scripts/ats.py` 的
`policy()`、`whitelist_match()`、`lease_state()`、`lease_autonomy()`、`permission_gate()`、
`gate_operation()`、`create_approval()`、`create_lease_approval()`、`approval_check()`、`cmd_approve()`。

---

## 1. 设计意图

调度器在无人值守环境里执行命令，最大风险是「Agent 自行决定跑了一条破坏性命令」。权限层要解决的问题是：

1. **分级**：不同破坏力的命令用不同门槛
2. **上限**：即使 Agent 自称高权限，也不能越过运维设定的硬上限
3. **留痕**：每次放行/拦截都有事件与日志
4. **需人点头**：高风险操作必须留下人类批准记录，且审批绑定具体命令
5. **不误伤**：已知安全的命令走白名单，不必反复审批

它**不**解决：

- 身份伪造（能改环境变量的人可以自称 L3）
- 审计防篡改（文件可被删改）
- 多租户隔离

这些需要外层系统提供。

---

## 2. 等级定义

| 等级 | 名称 | 判定标准 | 示例 |
| --- | --- | --- | --- |
| L0 | 只读 | 不改变任何持久状态 | `ls`、`cat`、`git status`、`grep` |
| L1 | 可恢复 | 改变工作区但可用 git/备份回滚 | `npm run build`、`pytest`、写临时文件、`pip install` 到虚拟环境 |
| L2 | 有外部影响 | 影响他人/共享资源 | 部署到测试环境、改 CI 配置、`git push`、删除非关键目录 |
| L3 | 不可逆 | 无法自动回滚 | 删库、`git push --force` 到主干、改生产配置、格式化磁盘 |

**定级原则**

1. 按「最坏后果」定，不按「通常后果」定
2. 涉及 `--force`、`--hard`、`rm -rf`、`drop`、`truncate` 一律上调
3. 涉及生产环境标识（`prod`、`live`、`release`）至少 L2
4. 不确定时上调一级

---

## 3. 判定流程

```
输入：command（命令原文）、required_level、approval_id、context

1. 白名单
   for rule in allow_rules:
       若 re.search(rule, command, IGNORECASE) → ALLOW(whitelist=rule)，结束

2. 策略上限
   若 rank(required_level) > rank(max_level) → DENY(denied_policy)，结束
   （审批与租约都不能绕过；这是硬上限）

3. 取审批模式
   mode = approval_mode_for(pol, required_level)
   取值优先级：approval_mode_by_level[level] > approval_mode > "ask"

4. 直接拒绝（deny-only 档）
   若 mode == "deny-only" → DENY(denied_mode)，结束
   （不创建审批记录，不产生可批准对象）

5. 身份直通（仅 auto 档）
   若 mode == "auto" 且 rank(required_level) <= rank(actor_level) → ALLOW(level)，结束
   （ask 档不看身份，即使 actor_level 更高也继续往下走审批）

6. 存活租约免审（详见 6.9）
   若 lease_autonomy.enabled 且租约存活 且 required_level ≤ min(max_level, 租约授予等级)
   且 command 命中 rules、不命中 deny_rules、未触发租约自带约束
   → ALLOW(lease_autonomy)，写一条 status=approved 的留痕审批，结束

7. 审批
   check = approval_check(approval_id, command)
   若 check.ok → ALLOW(approval)，结束
   否则 → DENY(approval_required)，并在调用方决定是否创建审批
```

`effective_level = min(required_level, actor_level, max_level)` 只用于**解释**拦截原因，不参与放行判定。

---

## 4. 策略来源与优先级

```
默认值  <  <root>/policy.json  <  环境变量
```

| 键 | 默认 | 环境变量 | 说明 |
| --- | --- | --- | --- |
| `max_level` | `L3` | `ATS_MAX_LEVEL` | 硬上限 |
| `actor_level` | `L3` | `ATS_ACTOR_LEVEL` | 当前身份 |
| `allow_rules` | `[]` | `ATS_ALLOW_RULES` | 正则，`\|\|` 分隔 |
| `approval_ttl_seconds` | `86400` | `ATS_APPROVAL_TTL` | 审批有效期，`<=0` 不过期 |
| `keep_runs_per_plan` | `5` | `ATS_KEEP_RUNS` | 快照保留数，`<=0` 不清理 |
| `stall_seconds` | `300` | `ATS_STALL` | 停滞阈值，`<=0` 关闭 |
| `lease_autonomy` | 关闭 | `ATS_LEASE_AUTONOMY` | 租约免审配置（JSON 字符串），坏值回落关闭 |
| `lease_file` | `""` | `ATS_LEASE_FILE` | 租约路径，空 = `<root>/../temp/py_lease.json`，不存在则回落 `<root>/../.autopilot/temp/py_lease.json` |

`policy.json` 示例：

```json
{
  "max_level": "L2",
  "actor_level": "L1",
  "allow_rules": ["^git status", "^git diff", "^ls ", "^npm run (lint|test|build)"],
  "approval_ttl_seconds": 3600,
  "keep_runs_per_plan": 3,
  "stall_seconds": 120
}
```

查看生效值：

```bash
python scripts/ats.py policy --json
```

**注意**：环境变量优先于文件，便于临时提升/收紧权限。L2/L3 是否需要审批由 `approval_mode` / `approval_mode_by_level` 决定（默认 L2/L3 为 `ask`），与 `actor_level` 无关；`actor_level` 只在 `auto` 档（默认 L0/L1）参与「等级 ≤ 身份即直通」的判定。生产环境建议把 `actor_level` 设低（如 L1），并让受控等级保持 `ask`。

---

## 5. 白名单

### 5.1 语义

- 规则是**正则**，用 `re.search` 匹配命令原文（不区分大小写）
- 列表顺序即匹配顺序，第一条命中即返回
- 命中后**跳过等级检查与审批**，直接放行
- 白名单**不能**突破「不存在」之类的实体检查（只影响权限判定）

### 5.2 写法建议

```json
"allow_rules": [
  "^git status$",
  "^git (diff|log|show)",
  "^ls( -\\w+)? .*",
  "^npm run (lint|test|build|typecheck)$",
  "^pytest( .*)?$",
  "^python -m pytest( .*)?$",
  "^cargo (check|clippy|test)$"
  ]
```

推荐原则：

1. **锚定开头**（`^`）并尽量锚定结尾（`$`），避免 `.*` 兜底
2. 用精确子命令集合（`(lint|test|build)`），不要用 `npm run .*`
3. 不把带 `--force` / `--hard` / `rm` / `deploy` / `publish` 的命令放进白名单
4. 定期 review 白名单；一条 `.*` 等于把整套权限体系废掉

### 5.3 命中样例

| 命令 | 规则 | 结果 |
| --- | --- | --- |
| `git status` | `^git status$` | 放行 |
| `git status --short` | `^git status$` | 不命中（`$` 限制了） |
| `git status --short` | `^git status` | 放行 |
| `npm run deploy` | `^npm run (lint\|test)$` | 不命中 |
| `rm -rf build` | 任意常见白名单 | 不命中 → 走等级判定 |

---

## 6. 审批生命周期

```
                 ┌─────────── reject ───────────┐
                 │                               ▼
创建 ──► pending ── approve ──► approved ──►（使用）
   │        │                        │
   │        └── 到期 ──► expired      └── 到期 ──► expired（不可用）
   │
   └──（命令变更）──► mismatch（不可用）
```

### 6.1 状态

| 状态 | 可用 | 说明 |
| --- | --- | --- |
| `pending` | 否 | 等待决策 |
| `approved` | 是（且未过期） | 已批准 |
| `rejected` | 否 | 永久失效 |
| `expired` | 否 | 超过 `expires_at` |

### 6.2 字段

```json
{
  "id": "ap-3f2a91c4",
  "command": "npm run deploy",
  "required_level": "L2",
  "status": "pending",
  "created_at": 1793512103,
  "created_iso": "2026-11-01T13:48:23",
  "expires_at": 1793515703,
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
  "task_instance": "b8ef7c7837a8",
  "task_created_at": 1793512103,
  "resume": "python ats.py run deploy1 --approval <approval_id>"
}
```

- `source`：`run`（任务）、`plan`（计划步骤）、`operation`（stop/delete/pause/cancel/rerun）
- 定位字段用于回查：`schedule_id/plan_id/stage_id/step_id/task_id`
- `task_instance`（12 位 hex）：**任务实例唯一标识**，每次创建任务时生成
- `task_created_at`：任务创建时间戳，与 `task_instance` 一起作为**双因子绑定**
- `resume` 是给 Agent / 人复制粘贴的完整续跑命令

#### 6.2.1 实例绑定（防悬空引用）

删除 / 停止这类操作的审批 command 是 `delete <id>`、`stop <id>`，而 `<id>` 可以被回收复用。若只校验 command 字符串，旧审批就能作用于同名新任务——**悬空引用**漏洞。

防御链：

1. 创建任务时写入 `instance_id`（12 位 hex，来自 `uuid4().hex[:12]`），随任务 JSON 一起落盘
2. 创建审批时把 `task_instance` + `task_created_at` 一起写入审批记录
3. 执行时 `approval_instance_match()` 按优先级比对：
   - 两边都有 `task_instance` → 必须完全相同
   - 仅有 `task_created_at`（老任务）→ 时间戳必须一致
   - 任一缺字段 → `fail-closed`，拒绝放行

配套保障：

- `delete` 成功时调用 `purge_task_approvals()` 清理所有指向该任务的 **pending** 审批，已决议记录保留作历史
- `create_approval()` 内部的 `find_pending_approval()` 同样受实例绑定约束，杜绝跨实例复用待办

租约免审生成的记录（见 6.9）在上述字段之外多带五个：

```json
{
  "status": "approved",
  "auto_approved": true,
  "resolved_by": "py_lease:a01907f0bc0d",
  "note": "lease_autonomy: ^jar-deploy",
  "lease_token": "a01907f0bc0d",
  "lease_pid": 22188,
  "lease_started_at": 1793512103,
  "lease_heartbeat_at": 1793512133
}
```

- `auto_approved: true` 是唯一可用来区分「机器放行」与「人工批准」的标记
- 这类记录**创建时即为 `approved`**，不经过 `pending`，也不投递 `approval_created`

### 6.3 有效期

- 默认 86400 秒（24 小时）
- `approval_ttl_seconds <= 0`：不设 `expires_at`，永不过期（不推荐）
- 过期判定在**读取时**计算：`now >= expires_at`
- `approve` 遇到已过期：把状态永久置为 `expired`，返回 `error=expired`
- 执行遇到已过期：`approval_check` 返回 `expired` → 被拦

**为什么要有效期**：审批是对「此刻这条命令」的授权，不是永久通行证。24 小时后环境可能已变。

### 6.4 命令绑定

`approval_check` 比较 `record.command == command`（严格字符串相等）：

- 改了空格、改了参数、改了路径 → `mismatch`
- 因此**不要在审批后动态拼接命令**；审批生成时就应当把最终命令确定下来
- **指令包（`pack`）**：审批绑定的是**条目自身的 `command`**（切分后 `strip()` 的原文），不是整包文本；`pack` 本身不产生审批，也不豁免任何条目
- **`@enc` 钩子**：创建时钩子已从命令文本剥离，审批与白名单匹配的都是**主命令**；钩子不单独过权限门，随任务等级一起执行

### 6.5 拒绝

`approve <id> --reject --note "..."`：

- `status=rejected`，`resolved_by=actor:<level>`
- 同一审批 ID 永久不可用（再次 `approve` 返回 `already_rejected`）
- 需要重新执行 → 触发一次新的审批创建

### 6.6 批量

```bash
python scripts/ats.py approve --all              # 批准所有 pending 且未过期
python scripts/ats.py approve --all --reject     # 全部拒绝（慎用）
python scripts/ats.py approve a1 a2 a3          # 指定多个
```

`--all` 会跳过已过期记录（顺带把它们置为 `expired`），返回实际处理结果。

单条批量返回结构：

```json
{
  "approvals": [
    {"approval_id": "ap-1", "ok": true, "status": "approved", "approval": {...}, "event": {...}},
    {"approval_id": "ap-2", "ok": false, "error": "expired"}
  ],
  "count": 2,
  "events": [...],
  "actor_level": "L3"
}
```

`ok=false` 的 `error` 取值：`missing`、`expired`、`already_approved`、`already_rejected`、
`already_expired`、`insufficient_level`。

### 6.7 批准者等级

批准者身份为当前 `actor_level`；要求 `rank(actor) >= rank(required_level)`，否则：

```json
{"approval_id": "ap-1", "ok": false, "error": "insufficient_level",
 "required_level": "L3", "actor_level": "L1"}
```

这条规则防止「低权限进程自己批自己的请求」。

**退出码**：单条 `approve <id>` 失败时给出可判别退出码——`insufficient_level` → `4`（EXIT_DENIED），`missing` / `expired` / `already_*` → `3`（EXIT_CONFLICT）。`approve --all` 是批量语义，无论部分成败一律返回 `0`，逐项结果看 `approvals[]` 里的 `ok` / `error`。

### 6.8 Plan 层自动重发

在执行 Plan 的 Step 时：

```
check = approval_check(state.approval_id, command)
若 check.error ∈ {missing, expired} 且 rank(actor) >= rank(required_level):
    创建新审批 → 写入 state.approval_id / approval_reissued_at
    投递 approval_created（带 reissued 标记）
    返回 paused
```

要点：

- `rejected` **不会**自动重发（尊重拒绝决定）
- `pending` **不会**重发（避免刷屏），只等待
- `mismatch` **不会**重发（命令已变，需要人重新确认）
- 身份不足时仍会创建一次审批（用于交给更高权限的人批），但不会被视为「自动重发」

### 6.9 存活租约免审（`lease_autonomy`）

外部授权窗口（`.trae/scripts/py_consent.py`）存活期间，可让命中白名单的高危指令跳过逐条审批。它是**第 4 条放行路径**，位于身份判定之后、审批之前。

五道关（任一不满足即退回普通审批）：

| 关 | 判据 | 失败表现 |
| --- | --- | --- |
| 1 总闸 | `lease_autonomy.enabled` 为真且 `rules` 非空 | 权限门不读租约，`reason` 仍是 `approval_required` |
| 2 等级 | `rank(required_level) <= rank(min(max_level, 租约 max_level))` | 退回审批；若超 `policy.max_level` 则早在第 2 步 `denied_policy` |
| 3 存活 | `granted`、心跳未过期、未到期限、宿主 PID 存活 | 退回审批；`lease_state()` 给出判定码 |
| 4 命令 | 命中 `rules` 且不命中 `deny_rules` | 退回审批 |
| 5 边界 | 租约 `extra_constraints`（如「额外禁止删除」）未命中动作 | 退回审批，日志 `lease_autonomy_blocked` |

`lease_state()` 判定码：`lease_absent` / `lease_unreadable` / `lease_invalid` / `lease_heartbeat_broken` / `lease_stale` / `lease_expired` / `lease_expiry_broken` / `lease_host_dead` / `lease_alive`。**除 `lease_alive` 外一律判无效**。

放行留痕：写一条 `status=approved` 的审批记录，`auto_approved=true`、`resolved_by="py_lease:<令牌>"`、`note="lease_autonomy: <规则>"`，并带 `lease_token` / `lease_pid` / `lease_started_at` / `lease_heartbeat_at`；**不写 pending、不投递 `approval_created`**，因此待办列表不被污染，`approve --all` 也不会把它当待决议项。

```json
"lease_autonomy": {
  "enabled": true,
  "max_level": "L2",
  "rules": ["^jar-deploy", "^gradlew build"],
  "deny_rules": ["--force", "^rm "]
}
```

约束与边界：

- **默认关闭**，且任何非法输入（JSON 坏、类型错、`max_level` 不在 L0-L3）都回落为关闭
- **不突破 `max_level`**：租约只放宽「审批」这一关，不放宽等级
- **授予由用户勾选**：`py_consent.py` 第 4 题「否 / 仅 L2 / 含 L3」，自定义文本不构成授予
- **随租约消亡**：窗口关闭 / 心跳过期后，下一次权限门立即回到审批，无需清理配置
- **只读**：ATS 不写、不删租约，也不杀进程；撤销仍由用户关窗完成
- 与 `actor_level` 同为**声明式**：能伪造租约文件或改 `policy.json` 的进程即可自证，不是敌对环境下的安全边界

---

## 7. 操作型权限门

`gate_operation` 用于不执行命令、但会改变调度状态的动作：

| 操作 | 需要等级 | label 示例 |
| --- | --- | --- |
| `stop` | L1 | `stop t1` |
| `delete` task | L2 | `delete t1` |
| `pause` schedule | L1 | `pause p1` |
| `cancel` schedule | L2 | `cancel p1` |
| `rerun_task` | L1 | `rerun p1/s1/step` |
| `rerun_plan` | L1 | `rerun_plan p1-a1` |
| `resume_schedule` | L1 | `resume_schedule p1` |
| `delete` plan/schedule | L2 | `delete p1-a1` |

与 `permission_gate` 的差异：

- 审批绑定的文本是 **label**（不是命令），例如 `stop t1`
- 失败时**自动创建审批**并投递事件，返回 `approval_id` 与 `resume`
- 返回结构直接带 `reason`，便于 Agent 判断是 `denied_policy` 还是 `approval_required`
- 同样支持 6.9 的存活租约免审：命中 `lease_autonomy.rules` 的 label 在租约存活时直接放行

---

## 8. 事件与日志

| 动作 | 事件 | 日志 msg |
| --- | --- | --- |
| 创建审批 | `approval_created` | `approval_created` |
| 批准 | `approval_resolved`（`status=approved`） | `approval_resolved` |
| 拒绝 | `approval_resolved`（`status=rejected`） | `approval_resolved` |
| 过期（批准时发现） | — | `approval_expired` |
| 租约免审放行 | `approval_lease_auto` | — |
| 租约约束拦下免审 | `lease_autonomy_blocked` | — |
| 执行被拦 | — | `task_blocked`（Task 层） |
| Plan 请求/重发审批 | `approval_created` + `plan_paused` | `approval_requested` / `approval_reissued` / `plan_paused` |

投递位置：

- 记录带 `schedule_id` → `schedules/<sid>/events.jsonl`
- 否则 → `<root>/events.jsonl`

---

## 9. 未实现

| 项 | 说明 | 替代方案 |
| --- | --- | --- |
| 身份鉴权 | `actor_level` 可被任何进程声明 | 外层 CI token / 操作系统账户 / 人工二次确认 |
| 租约伪造 | 租约文件是普通 JSON，能写盘就能自证 | 外层账户隔离；`lease_autonomy` 默认关闭，按需开启 |
| 黑名单硬拒绝 | 没有全局「这些命令永不批准」的规则（`lease_autonomy.deny_rules` 只作用于租约路径） | 用 `max_level` 限制 + 白名单只放安全命令 |
| 审批双人复核 | 单人即可批准 | 外部流程保证（如审批人必须是另一账号） |
| 审批委托/转交 | 无 | 人为在群里转达 |
| 审批记录清理 / 归档 | 只有 JSON 文件与日志 | 清理用 `ats.py approval prune`（只删 `approved`/`rejected`/`expired`，`pending` 永不删）；归档仍需外部定期备份 `approvals/` 与 `scheduler.log` |

---

## 10. 反模式

| 反模式 | 后果 | 正确做法 |
| --- | --- | --- |
| `allow_rules: [".*"]` | 权限体系形同虚设 | 只放精确安全命令 |
| `actor_level: "L3"`（默认值）长期不变 | `auto` 档（默认 L0/L1）不再构成身份约束；`ask` 档（默认 L2/L3）本就由审批控制 | 需收紧 `auto` 档时临时降低；受控等级改 `approval_mode` |
| 审批后拼接变量再执行 | `mismatch` 永远失败 | 审批前确定最终命令 |
| TTL 设为 0（永久） | 过期授权被长期复用 | 按任务时长设 1-24 小时 |
| 把 `--force` 命令放白名单 | 不可逆操作无门槛 | 显式走审批 |
| 用 `approve --all` 当日常操作 | 一次误批全部高危命令 | 逐条批准，或至少人工 review 列表 |
| 拒绝后复用同一审批 ID | 永远失败 | 重新执行以生成新审批 |