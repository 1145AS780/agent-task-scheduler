# 存储布局 — agent-task-scheduler v3.1.1

所有状态都在 `ATS_ROOT` 下（默认 `<cwd>/.ats`）。本文描述目录结构、文件语义、生命周期与清理策略。

---

## 1. 根路径解析

优先级从高到低：

1. 命令行 `--root <dir>`
2. 环境变量 `ATS_ROOT`
3. 当前工作目录下的 `.ats`

```bash
# 显式指定
python scripts/ats.py --root D:/state/ats list

# 环境变量（推荐长期使用）
export ATS_ROOT=/var/lib/ats        # Linux/macOS
$env:ATS_ROOT = "D:\state\ats"      # PowerShell
```

**为什么默认在 cwd 下**：每个项目独立一套调度状态，随项目一起被 `.gitignore` 或备份，避免全局污染。

**建议**：把 `.ats/` 加进 `.gitignore`（含运行日志与临时状态）。

---

## 2. 完整目录树

```
<root>/
├── policy.json                     # 可选：策略
├── events.jsonl                    # 全局事件流（Task 层 + 无 schedule 的审批）
├── events_state.json               # 全局事件游标
├── scheduler.log                   # 全局日志（JSONL）
├── scheduler.log.1                 # 轮转文件（存在时）
├── approvals/
│   ├── ap-3f2a91c4.json
│   └── ap-8e1d55f0.json
├── tasks/
│   ├── build1/
│   │   ├── task.json
│   │   ├── stdout.log
│   │   ├── stderr.log
│   │   ├── supervisor.log
│   │   ├── hook-enc.log             # 仅 @enc 钩子：转换器输出（主命令成功才生成）
│   │   ├── pid
│   │   ├── exit_code
│   │   └── run.lock                 # 运行锁，per-task；不同 task 互不阻塞
│   ├── t-9a3c1f/
│   │   └── ...
│   ├── dl-1a2b3c/                   # 下载任务（kind=download）
│   │   ├── task.json
│   │   ├── download.json            # worker spec（只读）
│   │   └── stdout.log               # JSONL：item_start/retry/done/error + summary
│   └── tor-1a2b3c/                  # 种子任务（kind=torrent）
│       ├── task.json
│       ├── torrent.json             # worker spec（只读）
│       ├── stdout.log               # JSONL：torrent_progress/done/error + summary
│       ├── aria2.log                # aria2 自身日志（--log）
│       └── aria2.out.log            # aria2 stdout/stderr 重定向
└── schedules/
    ├── ci1/                        # 一次性计划
    │   ├── schedule.json
    │   ├── events.jsonl
    │   ├── events_state.json
    │   ├── plans/
    │   │   └── ci1-a1/
    │   │       ├── plan.json
    │   │       ├── run.lock              # Plan 层互斥锁（执行期间存在）
    │   │       └── stages/
    │   │           ├── lint/
    │   │           │   ├── stage.json
    │   │           │   └── steps/
    │   │           │       └── eslint/
    │   │           │           ├── step.json
    │   │           │           ├── stdout.log
    │   │           │           └── stderr.log
    │   │           └── test/
    │   │               └── ...
    │   └── runs/
    │       └── ci1-a1/
    │           ├── plan.json
    │           ├── schedule.json
    │           ├── steps.jsonl
    │           └── summary.json
    └── patrol1/                    # 周期计划
        ├── schedule.json
        ├── events.jsonl
        ├── plans/
        │   ├── patrol1-a1/
        │   └── patrol1-a2/
        └── runs/
            ├── patrol1-a1/
            └── patrol1-a2/
```

---

## 3. 文件语义

| 路径 | 写者 | 读者 | 语义 |
| --- | --- | --- | --- |
| `policy.json` | 人 | 两个脚本 | 策略；可热改 |
| `events.jsonl` | ats.py（Task 事件）/ ats.py（审批） | `ats.py events`、外部 | 全局事件流 |
| `events_state.json` | `ats.py events` | 同上 | 游标（行号） |
| `scheduler.log` | 两个脚本 | `ats.py logs`、人 | 全局时间线 |
| `scheduler.log.1` | 轮转逻辑 | 人 | 上一代日志 |
| `approvals/<id>.json` | `create_approval` / `cmd_approve` | `approval_check` | 审批记录 |
| `tasks/<id>/task.json` | `create` / `pack` / `run` / `supervise` / `check` | 所有 Task 命令 | 任务状态 |
| `tasks/<id>/stdout.log` | 真实子进程 | `show --lines` | 标准输出 |
| `tasks/<id>/stderr.log` | 真实子进程 | `show --lines` | 标准错误 |
| `tasks/<id>/supervisor.log` | 监督进程 | 人（排查） | 监督进程自身错误输出 |
| `tasks/<id>/hook-enc.log` | supervisor（`run_post_hook`） | `show --lines`、人 | `@enc` 钩子输出；主命令失败/超时/被 stop 时不生成 |
| `tasks/<id>/pid` | `supervise` | `stop` 兜底 | 子进程 PID |
| `tasks/<id>/exit_code` | `finalize_task` | `check` | 退出码（跨进程兜底） |
| `tasks/<id>/download.json` | `ats.py download` | `ats_download.py`（只读） | 下载 spec；不在 `ATS_ROOT` 之外留副本 |
| `tasks/<id>/torrent.json` | `ats.py torrent` | `ats_torrent.py`（只读） | 种子 spec（source/outputs/seed_time/限速/aria2 路径） |
| `tasks/<id>/aria2.log` | aria2c | 人（排查） | aria2 自身日志（`--log`） |
| `tasks/<id>/aria2.out.log` | aria2c | 人（排查） | aria2 stdout/stderr 重定向 |
| `schedules/<sid>/schedule.json` | `submit_*` / `check_schedule` / `pause` / `cancel` | 所有 Plan 命令 | Schedule 状态 |
| `schedules/<sid>/events.jsonl` | `ats_plan.emit` | `check_schedule`、人 | 该 Schedule 的事件 |
| `schedules/<sid>/events_state.json` | 预留（当前未使用） | — | 局部游标（保留给外部消费者） |
| `plans/<pid>/plan.json` | `materialize` / `run_plan` | 执行器 | Plan 状态 |
| `plans/<pid>/run.lock` | `run_plan` / `rerun_plan` | 同上 | Plan 层互斥锁；仅执行期间存在 |
| `plans/<pid>/stages/<stid>/stage.json` | `run_stage` | 执行器 | Stage 状态 |
| `plans/<pid>/stages/<stid>/steps/<spid>/step.json` | `start_step` / `finish_step` / `skip_dependents` / `finish_batch_fail_fast` | 执行器 | Step 状态（即停终止时写 `aborted_by` / `aborted_at`） |
| `steps/<spid>/stdout.log` | 子进程 | 人 | Step 输出（追加，重试不清空） |
| `runs/<pid>/{plan,schedule,summary}.json` + `steps.jsonl` | `write_run_snapshot` | `history` | 不可变快照 |

**`run.lock` 的强杀恢复**：进程被 `taskkill /F` 时 `finally` 块不执行，锁文件会残留。
Task 层（`tasks/<task_id>/run.lock`）兜底是「持有者 pid 已死」或「锁龄 > 300s」时自动清理后重试一次，**最坏等 5 分钟**；
Plan 层（`plans/<pid>/run.lock`）**只看持有者 pid 是否已死**，不按锁龄抢锁（Plan 可能运行超过 5 分钟）。
急用可直接删除对应的 `run.lock`（该文件仅在执行期间有意义）。

---

## 4. 生命周期

### 4.1 Task

```
create → pending
run    → starting → running
结束    → succeeded / failed / timeout / killed
delete → 目录删除（running 时拒绝，先 stop）
```

- `task.json` 常驻，直到 `delete`
- `pack` 登记的条目在 `run` 之前**只有** `task.json`：没有 `pid` / `exit_code` / `stdout.log` / `stderr.log`
- `stdout.log`/`stderr.log` 每次 `run` 重置为空
- `supervisor.log` 追加，不重置
- `task.json` 的 `detach_mode` 记录 supervisor 的启动方式：`breakaway`（宿主 Job 允许脱离）/ `wmi`（Job 拒绝 breakaway，改由 WMI 创建进程）/ `detached`（两种都失败时的兜底）
- 无自动清理：Task 数量多时需自行 `delete` 或外部脚本按 `created_at` 清理

### 4.2 审批

```
create → pending
approve → approved / rejected
过期    → expired（在批准或执行时落定）
```

- 审批文件**不会**自动删除
- 手动清理：`python scripts/ats.py approval prune [--dry-run] [--older-than 秒]`
  只删 `approved`/`rejected`/`expired`；**`pending` 永不删除**，未知状态保留
- 例：清理 30 天前的已决议记录 `python scripts/ats.py approval prune --older-than 2592000 --dry-run`
  （`--older-than 0` = 不限时间 = 删除**所有**已决议记录，`pending` 仍保留）
- 已被引用的审批即使删除也只会导致 `approval_check` 返回 `missing`

### 4.3 Plan

```
submit → pending
run    → running
完成    → done / failed
暂停    → paused →（resume）running
取消    → cancelled
```

- Plan 目录长期保留（历史证据）
- `rerun_task` 复用同一 Plan 目录，状态回退到 pending（单步）
- `rerun_plan` 把整个 Plan 的所有 Stage/Step 重置为 pending，`waves` 归零
- 删除单个 Plan：`delete <plan_id>`（L2）
- 同一 Schedule 的第 N 次尝试是**另一个** Plan 目录（`-aN`）

### 4.4 Schedule

```
submit → active（`next_run` = `start_at`，缺省 `now + every`）
到期    → 触发一次 Plan，`next_run` 重写为 `now + every`（`start_at` 不再参与）
达到 max_runs → done，`next_run = null`
pause  → paused → resume_schedule → active（保留 `next_run`）
cancel → cancelled，`next_run = null`
delete → 整个目录删除
```

### 4.5 快照

- 每次 Plan 收尾（done/failed）写一份
- 目录名 = `<plan_id>`（已含 `-a<attempt>`）
- 内容为**副本**，之后 Plan 继续运行不会改快照
- 自动裁剪：按 `keep_runs_per_plan`（默认 5）

### 4.6 MCP 端点（控制台进程内，不落盘）

`ats_ui.py` 托管的 MCP 端点（`ats_mcp_http.py`）**不向 `ATS_ROOT` 写任何文件**：

- 会话（`Mcp-Session-Id`）与客户端计数只存在控制台进程内存里；`DELETE /mcp` 立即移除，空闲 30 分钟自动剔除
- 端点默认关闭；开启后才绑定 `127.0.0.1` 上的独立端口（默认控制台端口 + 1，可用 `--mcp-port` 覆盖）
- 关闭端点或退出控制台会停止监听并释放端口；没有残留文件、没有守护进程
- 开关状态不跨进程保留：刷新页面后看到的是进程内真实监听状态，而不是持久化配置
- stdio 版 `ats_mcp.py` 由 MCP 客户端拉起，同样不写状态文件，只在被调用时以子进程读写既有 Task / Plan 文件

---

## 5. 清理策略

### 5.1 自动

| 对象 | 时机 | 规则 |
| --- | --- | --- |
| 快照 | Plan 收尾 | 每个 plan 保留最近 N 份（`keep_runs_per_plan`） |
| `scheduler.log` | 每次写前 | 超过 2 MB 轮转为 `.1`（只保留一代） |

### 5.2 手动

```bash
# 预览
python scripts/ats_plan.py prune --keep 3 --dry-run
# 执行
python scripts/ats_plan.py prune --keep 3
# 用策略值
python scripts/ats_plan.py prune

# 审批记录：清理 30 天前的已决议记录（pending 永不删）
python scripts/ats.py approval prune --older-than 2592000 --dry-run
python scripts/ats.py approval prune --older-than 2592000
```

**两条 prune 的 `0` 语义相反**：`ats_plan.py prune --keep 0` = 不清理（保留全部快照）；
`ats.py approval prune --older-than 0` = 不限时间 = 删除所有已决议记录（`pending` 仍保留）。

### 5.3 彻底清理

```bash
# 删除单个任务/计划/周期（受 L2 权限约束）
python scripts/ats.py delete build1
python scripts/ats_plan.py delete ci1-a1
python scripts/ats_plan.py delete ci1

# 整库重置（危险）
rm -rf <root>
```

**整库重置前**：确认没有 `running` 的任务或计划，否则会留下孤儿进程。先 `check` + `stop`。

---

## 6. 备份与迁移

### 6.1 备份

```bash
# 只备份"配置 + 计划定义 + 快照"，跳过输出日志
tar czf ats-backup.tgz \
    --exclude='*/stdout.log' --exclude='*/stderr.log' \
    --exclude='*/supervisor.log' \
    policy.json schedules/*/schedule.json schedules/*/plans \
    schedules/*/runs approvals
```

### 6.2 迁移到另一台机器

1. 停止所有运行中任务
2. 复制整个 `<root>`（或按上面精简）
3. 路径注意：`task.json` / `step.json` 里**没有**绝对路径依赖（除 `plan_spec` 已内嵌）
4. `cwd` 字段若指向旧机器路径，需要改写

### 6.3 从 v2.0 升级

- 直接替换脚本即可；旧状态文件缺字段时按默认值处理
- 建议先跑 `ats.py check` 和 `ats_plan.py list` 确认状态可读
- 旧 Plan 没有 `runs/` 快照，`history` 返回空列表属正常

---

## 7. 权限与安全

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 状态文件可被任意进程改写 | 无签名/校验 | 目录权限收紧（`chmod 700`） |
| 审批文件可被伪造 | 无签名 | 信任边界放在外层 |
| 日志可被删改 | 纯文本 | 外部集中日志（如果需要） |
| 命令含敏感信息（token） | `command` 明文入盘 | 用环境变量 + `env` 字段，命令里只写变量名 |
| `stdout.log` 泄露数据 | 明文 | 清理策略 + 目录权限 |
| MCP 端点暴露命令执行 | 开启后本机任意进程都可连上并调用工具 | 只绑 `127.0.0.1`；校验 Host / Origin；开启必须显式 `confirm` |

**目录权限建议**：

```bash
mkdir -p <root> && chmod 700 <root>
```

Windows：把 `<root>` 放在用户私有目录，用 NTFS ACL 限制其他账户。

---

## 8. 已实现 / 未实现

| 项 | 状态 |
| --- | --- |
| 快照自动保留 | ✅ v2.1 |
| 手动 prune / dry-run | ✅ v2.1 |
| 日志轮转 | ✅ v2.1 |
| 全局事件游标 | ✅ v2.1 |
| 状态原子写 | ✅ |
| 跨进程互斥锁（Task 层 `run`） | ✅ `tasks/<id>/run.lock`：原子建锁（`O_CREAT`+`O_EXCL`），持有者 pid 已死或锁龄 > 300s 时清理后重试一次 |
| 跨进程互斥锁（Plan 层 `run_plan`/`rerun_plan`） | ✅ `plans/<pid>/run.lock`：原子建锁（`O_CREAT`+`O_EXCL`），仅当持有者 pid 已死时清理残留并重试一次（不按锁龄抢锁）；并发调用返回 `run_lock_busy`（exit 3） |
| 快照压缩 | ❌ 纯文本 |
| 自动清理旧 Task | ❌ 需手动 |
| 审批记录清理 / 归档 | ✅ 清理用 `approval prune`；归档需外部备份 |
| 状态加密 | ❌ |
| MCP 接入 | ✅ stdio `ats_mcp.py`（16 工具）；HTTP `ats_mcp_http.py` 由控制台内启停，默认关闭、不落盘 |