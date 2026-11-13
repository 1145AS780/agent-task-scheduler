# 数据模型 — agent-task-scheduler v3.1.1

本文列出所有落盘 JSON 结构的字段、类型与语义。字段以实际脚本为准。

**实现状态**：v2.8 全部字段生效（含 `waves` 跨 `pause → resume` 累加、`kind`/`argv`/`download`/`torrent`/`pack` 与 `download.json`/`torrent.json`、即停相关 `fail_on_critical`/`critical`/`aborted_by`/`aborted_at`、命令成功钩子 `post_hook`、存活租约免审的 `lease_autonomy`/`lease_file` 与审批记录 `auto_approved`/`lease_token`/`lease_pid`/`lease_started_at`/`lease_heartbeat_at`）。

---

## 1. 实体关系

```
policy.json（单例）
approvals/<approval_id>.json
tasks/<task_id>/task.json
schedules/<schedule_id>/schedule.json
schedules/<schedule_id>/plans/<plan_id>/plan.json
schedules/<schedule_id>/plans/<plan_id>/stages/<stage_id>/stage.json
schedules/<schedule_id>/plans/<plan_id>/stages/<stage_id>/steps/<step_id>/step.json
schedules/<schedule_id>/runs/<plan_id>/summary.json
events.jsonl（全局）
schedules/<schedule_id>/events.jsonl（局部）
scheduler.log（全局）
```

---

## 2. `policy.json`

可选。缺失时用默认值。

| 字段 | 类型 | 默认 | 语义 |
| --- | --- | --- | --- |
| `max_level` | string | `"L3"` | 权限硬上限，审批不能绕过 |
| `actor_level` | string | `"L3"` | 当前身份等级（声明式） |
| `allow_rules` | string[] | `[]` | 白名单正则；`re.search` 不区分大小写 |
| `approval_ttl_seconds` | int | `86400` | 审批有效期；`<=0` 不过期 |
| `keep_runs_per_plan` | int | `5` | 每个 plan 保留快照份数；`<=0` 不清理 |
| `stall_seconds` | int | `300` | 停滞阈值；`<=0` 关闭检测 |
| `lease_autonomy` | object \| string | 关闭 | 存活租约免审：`enabled` / `max_level` / `rules` / `deny_rules`；字符串按 JSON 解析 |
| `lease_file` | string | `""` | 租约路径；空 = `<root>/../temp/py_lease.json`（不存在则回落 `<root>/../.autopilot/temp/py_lease.json`） |

非法值会被回退到默认值（`max_level`/`actor_level` 不在 L0-L3 时；整数字段非整数时；
`lease_autonomy` 任何解析失败一律回落为 `enabled=false`）。

---

## 3. Task

### 3.1 `tasks/<task_id>/task.json`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 任务 ID，`t-xxxxxx` / `dl-xxxxxx` / `tor-xxxxxx` 或用户指定 |
| `kind` | string \| 缺省 | 任务种类；缺省（普通命令）时无此字段，下载任务为 `"download"`，种子任务为 `"torrent"`，指令包条目为 `"pack"` |
| `command` | string | 命令原文（审批与重跑都以它为准）；下载任务是 `download N item(s) -> <out>`，种子任务是 `torrent <btih 前 16 位或文件名> -> <out>`，指令包条目是切分后 `strip()` 的单条命令；带 `@enc` 钩子时这里只存**主命令**，而审批 / 预检 / 等级判定绑定的是「主命令 + `@enc` 参数」的拼合文本 |
| `argv` | string[] \| null | 原生 argv（不经 shell）；下载任务直启 `ats_download.py`，种子任务直启 `ats_torrent.py` |
| `download` | object \| null | 下载元数据：`spec` / `outputs` / `items` / `concurrency` / `rate_limit` / `retry` |
| `torrent` | object \| null | 种子元数据：`spec` / `source`（`"magnet"` 或 `"torrent"`）/ `outputs` / `seed_time` / `max_download` / `max_upload` / `aria2` |
| `pack` | object \| null | 指令包元数据：`pack_id` / `index`（1 起）/ `total`；仅 `kind="pack"` 出现 |
| `post_hook` | object \| null | 命令成功后钩子；仅命令文本带 `@enc` 时出现。登记时：`type`（目前仅 `"enc"`）/ `args`（原始参数串，执行时按空白分词、引号包裹段保持完整，再以 argv 列表交给转换器，不经 shell）；执行后追加 `status`（`ok`/`failed`）/ `exit_code` / `log` / `error_type`（`encoder_not_found`/`hook_timeout`/`hook_spawn_failed`/`hook_glob_no_match`/`hook_glob_too_many`/`hook_glob_ambiguous`）/ `started_at` / `ended_at`；参数含通配路径时另有 `glob`（通配原文）/ `expanded`（展开个数）/ `skipped_symlinks`（跳过的符号链接数）/ `runs`（展开多个时逐条的 `path` + `exit_code`）/ `failed_on`（第一个失败的路径） |
| `cwd` | string \| null | 工作目录 |
| `level` | string | L0-L3 |
| `timeout` | int | 秒；0 = 不限 |
| `env` | object | 追加环境变量（字符串键值） |
| `shell` | bool | 是否用系统 shell 解释 |
| `description` | string \| null | 备注 |
| `tags` | string[] | 标签；指令包条目为 `["pack", "<pack_id>"]`，其余预留 |
| `status` | string | `pending`/`starting`/`running`/`succeeded`/`failed`/`timeout`/`killed` |
| `created_at` | int | Unix 秒 |
| `created_iso` | string | 本地时间 ISO |
| `updated_at` | int | 每次写入刷新 |
| `started_at` | int \| null | 启动时间 |
| `started_iso` | string \| null | 运行后出现 |
| `ended_at` | int \| null | 结束时间 |
| `ended_iso` | string \| null | 运行后出现 |
| `pid` | int \| null | 真实命令进程 PID |
| `supervisor_pid` | int \| null | 监督进程 PID |
| `exit_code` | int \| null | 退出码；超时为 -1 |
| `approval_id` | string \| null | 本次运行所用审批 |
| `finalized` | bool | 终态是否已由 supervise/check 落盘 |
| `timed_out` | bool | 运行后出现；仅超时时为 true |
| `note` | string | 运行后出现；例如 `supervisor_lost`、`stopped_by_user` |

### 3.2 其它文件

| 文件 | 内容 |
| --- | --- |
| `stdout.log` | 命令标准输出（追加） |
| `stderr.log` | 命令标准错误（追加） |
| `supervisor.log` | 监督进程自身的 stderr（排查启动失败用） |
| `pid` | 真实命令 PID 文本 |
| `exit_code` | 退出码文本（供 `check` 兜底读取） |
| `download.json` | 仅下载任务：worker spec（见 3.3） |
| `torrent.json` | 仅种子任务：worker spec（见 3.4） |
| `aria2.log` | 仅种子任务：aria2 自身日志 |
| `aria2.out.log` | 仅种子任务：aria2 stdout/stderr 重定向 |
| `hook-enc.log` | 仅带 `@enc` 钩子且主命令成功：转换器输出（成功/失败都写；主命令失败时不生成） |

### 3.3 `tasks/<task_id>/download.json`（`kind="download"`）

由 `ats.py download` 写出，`ats_download.py` 只读。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `outputs` | string | 当前目录 | 输出目录（绝对路径） |
| `items` | object[] | — | 每条：`url` / `save_as` / `sha256` / `size` / `headers` |
| `deduped` | int | 0 | 被合并掉的重复条目数 |
| `manifests` | string[] | 缺省不写 | 实际读入的清单路径（`--file` 通配展开后的结果，按字典序）；无 `--file` 时该键不出现 |
| `concurrency` | int | 2 | 线程池大小 |
| `rate_limit` | int | 0 | 全局字节/秒，0 不限 |
| `retry` | int | 2 | 每项重试次数（总尝试 = retry + 1） |
| `retry_backoff` | int | 3 | 线性退避基数（秒） |
| `io_timeout` | int | 30 | 单次网络读超时 |
| `resume` / `overwrite` | bool | true / false | 是否续传 `.part` / 是否覆盖已有目标 |
| `insecure` | bool | false | 跳过 HTTPS 证书校验 |
| `verify_sha256` / `verify_size` | bool | true | 是否做对应校验 |
| `headers` | object | {} | 全局请求头（条目级 `headers` 优先） |
| `proxy` | string \| null | null | http/https 代理 |

### 3.4 `tasks/<task_id>/torrent.json`（`kind="torrent"`）

由 `ats.py torrent` 写出，`ats_torrent.py` 只读。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `source` | object | — | `{"type": "magnet", "value": "magnet:..."}` 或 `{"type": "torrent", "path": "<绝对路径>"}` |
| `outputs` | string | 当前目录 | 下载根目录（绝对路径） |
| `seed_time` | number | 0 | 做种分钟数；`0` = 下完即停 |
| `max_download` / `max_upload` | int | 0 | 全局上下行限速（字节/秒），0 不限 |
| `log` | string | — | aria2 自身日志路径（`tasks/<id>/aria2.log`） |
| `aria2` | string | — | 创建时解析到的 aria2c 绝对路径；worker 发现该文件已不存在时会重新解析 |
| `poll` | number | 2 | RPC 轮询间隔秒数；`ats.py torrent` 不写，仅手工 spec 可加 |

### 3.5 指令包任务（`kind="pack"`）

`ats.py pack` 只写 `task.json`：**不写** `download.json` / `torrent.json` / `stdout.log` / `stderr.log` / `pid` / `exit_code`，任务停在 `pending`。

- 同包条目共享 `pack_id`，但**彼此独立**：没有依赖、没有顺序保证；`index` 只表示它在原文本中的位置
- 任意一条可直接 `run`，不影响同包其他任务（`run` 后与普通 Task 完全一致）
- `list --pack <pack_id>` 按 `pack.pack_id` 过滤

---

## 4. Plan

### 4.1 `plan.json`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | `<schedule_id>-a<attempt>` 或 spec id |
| `schedule_id` | string | 所属 Schedule |
| `title` | string \| null | 展示用 |
| `attempt` | int | 第几次尝试，从 1 开始 |
| `status` | string | `pending`/`running`/`done`/`failed`/`paused`/`cancelled` |
| `on_stage_failure` | string | `continue`/`stop` |
| `fail_on_critical` | string | `continue`/`stop`；`stop` 时 `critical: true` 的 Step 最终失败即终止整个 Plan |
| `stages` | array | spec 的 Stage 副本（执行依据） |
| `created_at` / `created_iso` | int / string | 创建时间 |
| `started_at` / `started_iso` | int / string \| null | 首次执行 |
| `ended_at` / `ended_iso` | int / string \| null | 结束 |
| `plan_stall_delivered` | bool | plan 级停滞事件是否已投递（one-shot 保护） |
| `paused_at` | int \| null | 暂停时间 |
| `paused_stage` | string \| null | 卡在哪个 Stage |
| `paused_step` | string \| null | 卡在哪个 Step |
| `updated_at` | int | 每次写入刷新 |

### 4.2 `stage.json`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | Stage ID |
| `mode` | string | `serial`/`parallel` |
| `status` | string | `pending`/`running`/`done`/`failed`/`paused`/`skipped` |
| `started_at` | int \| null | — |
| `ended_at` | int \| null | — |
| `waves` | int | 调度波次数。每轮进入时若还有未终结 step 就 +1：收尾的空轮不计；只做依赖跳过传播的轮次计入；本轮触发暂停（`paused`）而未执行任何 step 也计入。跨 `pause → resume` **累加不重置**，暂停前的轮次已落盘在 `stage.json` |
| `error_type` | string | 例如 `no_ready_steps` |
| `updated_at` | int | — |

### 4.3 `step.json`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | Step ID |
| `status` | string | `pending`/`running`/`succeeded`/`failed`/`timeout`/`skipped` |
| `depends_on` | string[] | 规范化后的依赖列表 |
| `retry` | int | 额外重试次数 |
| `retry_backoff` | int | 重试间隔秒数 |
| `attempts` | int | 已尝试次数（每 `start_step` +1） |
| `stall_delivered` | bool | step 级停滞事件是否已投递 |
| `started_at` | int \| null | 最近一次开始的 Unix 秒 |
| `ended_at` | int \| null | 最近一次结束 |
| `pid` | int \| null | 当前进程（结束后置 null） |
| `exit_code` | int \| null | 最近一次退出码 |
| `error_type` | string | 例如 `policy_denied` |
| `approval_id` | string \| null | 当前绑定的审批 |
| `approval_reissued_at` | int \| null | 自动重发审批时间 |
| `skipped_by` | string \| null | `user`/`dependency`/`critical_failed`/`cancelled` |
| `blocked_by` | string \| null | 导致跳过的直接上游 Step ID |
| `dependency_status` | string \| null | 上游终态（`failed`/`timeout`/`skipped`） |
| `aborted_by` | string \| null | `fail_fast`：被关键任务失败触发的即停终止了进程树 |
| `aborted_at` | int \| null | 被终止的 Unix 秒 |
| `updated_at` | int | — |

### 4.4 `summary.json`（快照）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plan_id` | string | — |
| `schedule_id` | string | — |
| `attempt` | int | — |
| `status` | string | Plan 终态 |
| `on_stage_failure` | string | — |
| `started_at` / `ended_at` | int \| null | — |
| `counts` | object | `{status: 数量}` |
| `steps` | array | 每步终态快照 |

`steps[]` 每项：

| 字段 | 类型 |
| --- | --- |
| `stage_id` | string |
| `step_id` | string |
| `status` | string |
| `attempts` | int |
| `exit_code` | int \| null |
| `started_at` / `ended_at` | int \| null |
| `skipped_by` / `blocked_by` | string \| null |

---

## 5. Schedule

`schedule.json`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | Schedule ID |
| `type` | string | `oneshot`/`recurring` |
| `status` | string | `active`/`paused`/`cancelled`/`done` |
| `every` | int | 周期秒；oneshot 为 0 |
| `next_run` | int \| null | 下次触发 Unix 秒。初始值取 `start_at`（`0` 表示已到期，仅此一次），之后每次触发都重写为 `now + every`；`null` 表示不再触发 |
| `next_run_iso` | string \| null | 便于人读；`next_run` 为 `0` / `null` 时写 `null`（`0` 是「已到期」而不是 1970 年） |
| `start_at` | int \| null | 原始 `start_at`，仅供追溯；运行期不再读取，只决定首轮 `next_run` 的初始值 |
| `runs` | int | 已触发次数 |
| `max_runs` | int \| null | 上限 |
| `last_run_at` | int | 最近触发时间 |
| `current_plan` | string \| null | 当前 Plan ID |
| `base_plan_id` | string | Plan ID 前缀（通常等于 `id`） |
| `plan_spec` | object | 内嵌的 Plan spec |
| `on_stage_failure` | string | 覆盖 Plan 设置 |
| `created_at` / `created_iso` | int / string | — |
| `paused_at` | int | — |
| `cancelled_at` | int | — |

---

## 6. 审批

`approvals/<approval_id>.json`：见 [permissions.md](permissions.md#62-字段)。

补充说明：

- 文件名 = `id` + `.json`
- 目录不存在时按需创建
- 无索引文件；`approve --all` 遍历目录

---

## 7. 事件

### 7.1 公共字段

所有事件都有：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ts` | int | Unix 秒 |
| `iso` | string | 本地时间 |
| `type` | string | 事件类型 |

按类型附加字段。

### 7.2 Task 层事件

| type | 字段 |
| --- | --- |
| `task_created` | `task_id`、`level`、`command` |
| `task_start` | `task_id`、`level`、`command`、`approval_id`、`gate` |
| `task_end` | `task_id`、`status`、`exit_code`、`timed_out`、`duration`、`command` |
| `task_post_hook` | `task_id`、`hook`、`status`、`exit_code`、`error_type` |
| `approval_created` | `approval_id`、`required_level`、`source`、`expires_at`、`resume` + 定位字段 |
| `approval_resolved` | `approval_id`、`status`、`required_level`、`resolved_by`、`note`、`resume` + 定位字段 |
| `approval_lease_auto` | `approval_id`、`required_level`、`rule`、`lease_token`、`source` + 定位字段 |
| `lease_autonomy_blocked` | `reason`、`lease_token`、`level` |

### 7.3 Plan 层事件

| type | 关键字段 |
| --- | --- |
| `schedule_created` | `schedule_id`、`kind`、`every`/`title` |
| `schedule_triggered` | `schedule_id`、`plan_id`、`attempt`、`next_run` |
| `schedule_paused` / `schedule_cancelled` | `schedule_id`、（`skipped_steps`） |
| `plan_materialized` | `plan_id`、`attempt`、`stages` |
| `plan_start` / `plan_resumed` | `plan_id`、`attempt`、（`approval`、`recovered`） |
| `plan_paused` | `plan_id`、`stage_id`、`step_id`、`approval_id`、`required_level`、`approval_error`、`reissued` |
| `plan_stalled` | `scope`（step/plan）、`quiet_seconds`、`threshold`、`attempt`/`status` |
| `plan_end` | `plan_id`、`status`、`attempt` |
| `stage_done` / `stage_failed` | `plan_id`、`stage_id`、`waves` |
| `stage_skipped` | `plan_id`、`stage_id`、`reason` |
| `task_start` | `stage_id`、`step_id`、`attempt`、`pid`、`command` |
| `task_done` | `stage_id`、`step_id`、`attempt` |
| `task_failed` | `stage_id`、`step_id`、`attempt`、`status`、`exit_code`、`timed_out` |
| `task_retry` | `stage_id`、`step_id`、`attempt`、`max_attempts`、`backoff_seconds`、`prev_error_type` |
| `task_skipped` | `stage_id`、`step_id`、`skipped_by`、`blocked_by`、`dependency_status` |
| `run_snapshot` | `plan_id`、`attempt`、`path`、`status`、`counts` |
| `prune_runs` | `keep`、`removed[]` |
| `steps_recovered` | `plan_id`、`steps[]` |
| `task_rerun` | `plan_id`、`stage_id`、`step_id`、`rearmed_steps` |

`task_failed` 还可能带 `error_type`（如 `policy_denied`）与 `required_level`/`policy_max`。

### 7.4 事件规则

1. 一行一个 JSON，禁止多行美化（否则无法流式读取）
2. 不做去重；消费方靠游标或业务 ID 判断
3. 事件是**追加**的，不修改历史
4. Plan 层事件的 `schedule_id` 一律存在；Task 层审批事件可能为 null

### 7.5 `events_state.json`（全局流游标）

```json
{"cursor": 128, "updated_at": 1793512103, "iso": "2026-11-01T13:48:23"}
```

`ats.py events` 默认读取 `cursor` 之后的行并推进；`--lines N` 只看尾部且**不**推进游标。

---

## 8. `scheduler.log`

JSONL，字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ts` | int | Unix 秒 |
| `iso` | string | 本地时间 |
| `level` | string | `info` 或等级（Task 层沿用 `L0`-`L3`） |
| `msg` | string | 事件名，如 `task_start`、`plan_end` |
| 其余 | — | `schedule_id`、`plan_id`、`stage_id`、`step_id`、`task_id`、`approval_id`、`status`、`gate` 等 |

轮转：文件超过 2 MB 时改名为 `scheduler.log.1`（覆盖旧的 `.1`）。

---

## 9. 时间与编码

- 所有时间为 **Unix 秒（int）**，另附 `*_iso` 本地时间字符串便于人读
- 所有文件 UTF-8，`json.dumps(..., ensure_ascii=False, indent=2)`
- 写入使用「临时文件 + `os.replace`」保证原子性
- 事件与日志为追加写，不重写

---

## 10. 版本兼容

| v2.0 → v2.1 变化 | 兼容性 |
| --- | --- |
| Approval 新增 `expires_at` | 旧记录无此字段 → 视为永不过期 |
| Step 新增 `depends_on`/`retry`/`attempts`/`stall_delivered` | 旧记录缺字段 → 默认 0/false |
| Plan 新增 `plan_stall_delivered` | 缺失视为 false |
| 新增 `runs/` 快照 | 旧 Plan 无快照，`history` 返回空列表 |
| `policy` 新增四键 | 旧 `policy.json` 无需改 |
| 事件新增类型 | 消费方应忽略未知 `type` |

**降级建议**：v2.1 写入的 `step.json` 直接给 v2.0 运行会忽略新字段，不会崩溃；
但不建议混用，因为 v2.0 不认识 `depends_on`，会并发跑本应串行的步骤。