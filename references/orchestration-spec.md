# 编排规格 — agent-task-scheduler v2.9

本文是 Plan / Schedule 层的权威规格。实现以 `scripts/ats_plan.py` 为准；若文档与实现不一致，以脚本行为为准并修正文档。

---

## 1. 能力矩阵

| 能力 | 状态 | 版本 | 说明 |
| --- | --- | --- | --- |
| Task 创建/执行/停止/删除 | ✅ | 1.0 | `scripts/ats.py` |
| 异步启动 + 状态轮询 | ✅ | 1.0 | `run` 派生 supervise 进程；`check` 同步 |
| 超时杀进程树 | ✅ | 1.0 | `timeout` 秒；`taskkill /T /F` |
| L0-L3 分级 | ✅ | 2.0 | `permission_gate` |
| 审批文件 + 命令绑定 | ✅ | 2.0 | `approvals/<id>.json` |
| 审批有效期 | ✅ | 2.1 | `expires_at`、`approval_expired` |
| 批量批准/拒绝 | ✅ | 2.1 | `approve --all` / `--reject` |
| 白名单放行 | ✅ | 2.1 | `allow_rules` 正则，优先级最高 |
| 审批事件投递 | ✅ | 2.1 | 任务→全局流；Plan→schedule 流 |
| Stage 串行/并行 | ✅ | 2.0 | `mode` |
| Stage 内单层依赖 | ✅ | 2.1 | `depends_on` + 拓扑稳定排序 |
| 依赖失败传播 | ✅ | 2.1 | `skipped(dependency)` + `blocked_by` |
| Step 自动重试 | ✅ | 2.1 | `retry` + `retry_backoff` |
| 停滞检测 | ✅ | 2.1 | `plan_stalled`（step/plan 两级，one-shot） |
| `waves` 计数 | ✅ | 2.2 | 空轮不计；暂停轮计入；`pause → resume` 累加不归零 |
| 周期触发 | ✅ | 2.0 | `every` + `start_at` + `max_runs` |
| 中断恢复 | ✅ | 2.1 | `steps_recovered` + `recover_interrupted` |
| 运行快照 | ✅ | 2.1 | `runs/<plan_id>/` |
| 快照保留/清理 | ✅ | 2.1 | `keep_runs_per_plan`、`prune`、`history` |
| 全局日志 | ✅ | 2.1 | `scheduler.log`（JSONL，2MB 轮转） |
| 全局事件游标 | ✅ | 2.1 | `events` + `events_state.json` |
| 通用 DAG | ❌ | — | 不支持跨 Stage/跨 Plan 依赖 |
| cron 宏 | ❌ | — | 只支持秒级 `every` |
| 身份鉴权 | ❌ | — | 声明式 `actor_level` |

---

## 2. Task 层规约

### 2.1 创建

- `command` 为字符串；`shell=true` 时通过系统 shell 解释
- `--no-shell` 时整串按可执行文件路径解释（不拆分参数），适合无空格路径
- `cwd` 必须存在，否则 `bad_cwd`（exit 2）
- `id` 可指定；已存在需 `--force`，否则 `task_exists`（exit 3）
- `level` 必须是 L0-L3；默认 L1
- `timeout` 是秒；`0` = 不限时
- 命令末尾可带 `@enc <转换器参数>`：落盘时拆成 `command`（主命令）+ `post_hook`（`type="enc"`）

### 2.2 执行

- `run` 默认**异步**：派生 `supervise` 子进程，父进程立即返回 `supervisor_pid`
- `supervise` 负责：启动真实命令、写 `pid`、等待完成、超时杀进程、写终态、投递 `task_end`
- 主命令退出码为 0 且任务带 `post_hook` 时，先执行钩子（`hook-enc.log` + 事件 `task_post_hook`），钩子非 0 则任务落 `failed` 且 `exit_code` 取钩子码
- `run --wait` 在调用进程内同步执行（便于测试或需要立即结果时）
- `run --force` 允许重跑 `succeeded/failed/timeout/killed` 的任务
- 每次 `run` 会清空 `stdout.log` / `stderr.log`（新一次运行新输出）

### 2.3 状态同步

`check` 做三件事（按优先级）：

1. `exit_code` 文件已存在且未 `finalized` → 用退出码补齐终态（`succeeded`/`failed`）
2. 超过 `started_at + timeout` → 杀进程树，标 `timeout`
3. `supervisor_pid` 不存在且未终态 → 标 `failed`，`note=supervisor_lost`

返回 `next_poll`：仍有运行中任务为 5 秒，否则 0。

---

## 3. Plan 层规约

### 3.1 提交

`submit_plan` 做：

1. 校验 spec（结构、字段类型、依赖合法性、环检测）
2. 创建一次性 Schedule（`type=oneshot`，`max_runs=1`）
3. 物化 Plan（`plans/<sid>-a1/`），生成 Stage/Step 状态文件
4. 投递 `schedule_created`、`plan_materialized`

不自动执行；Agent 再调 `run_plan <sid>-a1`。

### 3.2 校验规则

| 编号 | 规则 | 错误码 |
| --- | --- | --- |
| V1 | spec 是对象 | `spec_not_object` |
| V2 | `stages` 非空数组 | `stages_required` |
| V3 | 每个 Stage 有唯一 `id` | `stage[i].id_required` / `id_duplicate` |
| V4 | `mode ∈ {serial, parallel}` | `mode_invalid` |
| V5 | `steps` 非空 | `steps_required` |
| V6 | 每个 Step 有唯一 `id` 与 `command` | `id_required` / `id_duplicate` / `command_required` |
| V7 | `level ∈ L0-L3` | `level_invalid` |
| V8 | `retry` / `retry_backoff` / `timeout` 是非负整数 | `*_invalid` |
| V9 | `depends_on` 是字符串或字符串数组 | `depends_on_invalid` |
| V10 | 不得自依赖 | `depends_on_self` |
| V11 | 依赖必须存在于同 Stage | `depends_on_unknown:<id>` |
| V12 | 同 Stage 内无环（Kahn 排序长度检查） | `depends_on_cycle` |

一次可返回多条错误，`errors` 为字符串数组。

### 3.3 物化

```
plans/<plan_id>/plan.json
plans/<plan_id>/stages/<stage_id>/stage.json
plans/<plan_id>/stages/<stage_id>/steps/<step_id>/step.json
```

- `materialize_plan` 幂等：已存在的 `plan.json` / `stage.json` / `step.json` 不覆盖，只在缺失时补建
- 重复物化只更新 Plan 的 `stages` 副本（spec 变更时）
- 每个 Step 的初始态：`status=pending`、`attempts=0`、`stall_delivered=false`、依赖/重试参数落盘

### 3.4 执行循环

`run_plan` 的执行顺序：

```
对每个 Stage（按 spec 顺序）:
    若 Stage 已 done/failed/skipped → 跳过
    Stage.status = running
    循环（最多 MAX_WAVES=1000 波）:
        todo = 所有 status ∈ {pending} 的 Step
        若 todo 为空 → 跳出
        1) skip_dependents(todo)          # 上游失败 → 下游 skipped
        2) 逐个 ensure_step_permission    # 白名单/等级/审批
             任一 paused → Plan.status=paused，立即返回
             任一 denied(policy) → Step.failed(policy_denied)，按 on_stage_failure 处理
        3) ready = todo 中依赖全部 succeeded
        4) 若 ready 为空 → 跳出（后续判为死锁失败）
        5) serial 取 ready 第一个；parallel 取全部
        6) start_step 全部 → finish_with_retry 全部
           启用即停（plan.fail_on_critical="stop" 或 --fail-fast）且并行批次 >1:
               finish_batch_fail_fast：任一 critical Step 最终失败 → kill 同批兄弟进程树
               被终止 Step 记 aborted_by="fail_fast" + aborted_at，发 task_aborted
        7) on_stage_failure=stop 且任一失败 → 跳过剩余，Stage.failed，返回
        8) 即停命中（critical 最终失败）→ 剩余 Step 标 skipped(critical_failed)，
           Stage.failed，返回 critical_failure=True
    循环结束:
        todo 仍有残留 → Stage.failed(no_ready_steps)
        否则 Stage.done 或 Stage.failed（有失败 Step）
```

只有 Stage 完全结束才进入下一个 Stage。

**即停（fail-fast）语义**：判定依据是**重试耗尽后的退出码**（`failed` / `timeout`），不扫描日志文本。`critical` 只看 Step 自身；`--fail-fast` 把当次运行的全部 Step 视为 critical。串行 Stage 走顺序路径，命中后不再启动后续 Step。

**`waves` 计数**：每进入一轮（`todo` 非空）就 `+1`，收尾的空轮不计。暂停也算一轮——该轮确实进入了调度。暂停时 `waves` 立即落盘，`resume` 时从该值继续累加，不会归零。

### 3.5 Plan 收尾

- `status` 判定：任一 Stage 失败 → `failed`；否则 `done`
- `on_stage_failure=stop`：首个失败 Stage 之后的所有 Stage 标 `skipped`
- 即停命中（`critical_failure`）：后续 Stage 同样标 `skipped`，`stage_skipped.reason=critical_failed`；`plan.status=failed`
- 写 `plan_end` 事件与日志
- 写运行快照
- 按策略裁剪旧快照

### 3.6 暂停与恢复

触发暂停的唯一原因：Step 权限不足且无有效审批。

- Plan → `paused`，记录 `paused_at` / `paused_stage` / `paused_step`
- Stage → `paused`，`waves` 保持前值并落盘（恢复后接着累加，不归零）
- 事件 `plan_paused`（带 `approval_id`、`required_level`、`approval_error`、`reissued`）
- 恢复：`resume <sid|pid> --approval <ap>`
- 恢复不会重跑已终结的 Step；`running` 残留 Step 由 `recover_interrupted` 复位为 `pending`

### 3.7 重跑单个 Step

`rerun_task <sid> <stage_id> <step_id>`：

1. 目标 Step → `pending`，`attempts=0`，清空 `exit_code/skipped_by/blocked_by`
2. 同 Stage 内因它而 `skipped(dependency)` 的直接下游 → `pending`（`rearmed_steps`）
3. Stage → `pending`；Plan → `pending`，`plan_stall_delivered=false`
4. 之后调 `run_plan` 继续

只重新武装**直接**下游；更深层依赖会在下一波 `skip_dependents` 时按实际状态重新评估。

---

## 4. Schedule 层规约

### 4.1 提交

- `every > 0`（秒）；`max_runs` 可选正整数
- `plan` 指向 Plan spec 文件，提交时**内嵌**为 `schedule["plan_spec"]`
- `start_at` 缺省 `now + every`；`start_at=0` 表示「首轮立即触发」。它**只影响首轮**：之后每次触发都会把 `next_run` 重写为 `now + every`
- 同名 schedule 已存在 → `schedule_exists`（exit 3）

### 4.2 驱动

`check_schedule <sid>`：

1. 若 `status=active` 且 `next_run is not None` 且 `now >= next_run` → 触发
2. 触发时：`attempt = runs + 1`，`plan_id = <base>-a<attempt>`，物化并执行
3. `next_run = now + every`；若达到 `max_runs` → `status=done`，`next_run=null`
   （`next_run` 每次触发后即被覆盖，`start_at` 不再参与计算；距上次触发不足 `every` 秒时 `triggered=[]`）
4. 执行遇 `paused` → Schedule 也置 `paused`
5. 遍历该 Schedule 下所有 Plan 做停滞检测
6. 返回 `next_poll`：到期阻塞时间为 0，否则 `min(30, 距下次触发秒数)`；有停滞时固定 30

**注意**：`run_plan` 在 `check_schedule` 内同步执行。单个 Step 最长占用一次调用（受 `timeout` 约束）。需要真正后台化时，在外部把 `check_schedule` 派生为后台进程。

### 4.3 暂停/取消

| 操作 | Schedule | Plan | Step |
| --- | --- | --- | --- |
| `pause` | `paused`，保留 `next_run` | 不变 | 不变 |
| `cancel` | `cancelled`，`next_run=null` | 未完成的 → `cancelled` | 未终结的 → `skipped(user)` |

`cancel` 不发 `task_skipped` 逐个事件，只在 `schedule_cancelled` 的 `skipped_steps` 里列出。

### 4.4 两类实体的删除

`delete <ident>`：

- 若 ident 是 plan id：只删 `plans/<pid>/`（`running` 时拒绝）
- 若 ident 是 schedule id：先置 `cancelled`，删除整个 `schedules/<sid>/`
- 需要 L2，可通过 `--approval` 提权

---

## 5. 事件映射速查

| 场景 | 事件 | 投递位置 |
| --- | --- | --- |
| 创建任务 | `task_created` | 全局 |
| 启动任务 | `task_start` | 全局（Task）/ schedule（Plan） |
| 任务结束 | `task_end` | 全局 |
| 命令成功钩子 | `task_post_hook` | 全局 |
| Step 成功 | `task_done` | schedule |
| Step 失败（最终） | `task_failed` | schedule |
| Step 重试（中间失败） | `task_retry` | schedule |
| Step 被跳过 | `task_skipped` | schedule |
| Step 被即停终止 | `task_aborted` | schedule（`reason=critical_failed`、`pid`） |
| 需要审批 | `approval_created` | 有无 `schedule_id` 决定 |
| 审批结果 | `approval_resolved` | 同上 |
| 计划暂停 | `plan_paused` | schedule |
| 停滞 | `plan_stalled` | schedule |
| Stage 结束 | `stage_done` / `stage_failed` / `stage_skipped` | schedule |
| 计划结束 | `plan_end` | schedule |
| 快照 | `run_snapshot` | schedule |
| 清理 | `prune_runs` | schedule |
| 中断恢复 | `steps_recovered` | schedule |
| 局部重跑 | `task_rerun` | schedule |
| 周期触发 | `schedule_triggered` | schedule |

全局流通过 `ats.py events` 消费（游标存 `events_state.json`）；schedule 流可直接读文件，或看 `check_schedule` 返回。

---

## 6. 命令 → API 映射

| 意图 | 命令 | 返回要点 |
| --- | --- | --- |
| 建任务 | `ats.py create` | `task`、`created` |
| 加载指令包 | `ats.py pack` | `pack_id`、`count`、`executed=false`、`tasks[]` |
| 跑任务 | `ats.py run` | `supervisor_pid` 或终态；被拦时 `approval_id` |
| 查任务 | `ats.py check/show/status` | `status`、`exit_code`、`next_poll` |
| 停/删任务 | `ats.py stop/delete` | gate 结果、`approval_id` |
| 批审批 | `ats.py approve` | `approvals[]`、`events[]` |
| 看事件 | `ats.py events` | 增量事件 + `cursor` |
| 看日志 | `ats.py logs` | 解析后的日志行 |
| 看策略 | `ats.py policy` | 六个策略键 |
| 提交计划 | `ats_plan.py submit_plan` | `plan_id`、`run_with` |
| 执行计划 | `ats_plan.py run_plan` | `stages[]`、`counts`、`run_dir`；`--fail-fast` 可临时即停 |
| 继续计划 | `ats_plan.py resume` | 同 run_plan；无审批时返回待批信息；`--fail-fast` 可临时即停 |
| 周期驱动 | `ats_plan.py check_schedule` | `triggered`、`stalled`、`next_poll` |
| 局部重跑 | `ats_plan.py rerun_task` | `rearmed_steps` |
| 整体重跑 | `ats_plan.py rerun_plan` | `rearmed_steps` |
| 恢复周期 | `ats_plan.py resume_schedule` | `next_run`、`current_plan_status` |
| 审批清理 | `ats.py approval prune` | `removed[]` 或 `would_remove[]` |
| 历史 | `ats_plan.py history` | `runs[]`（路径/状态/统计） |
| 清理 | `ats_plan.py prune` | `removed[]` 或 `would_remove[]` |

---

## 7. 并发与竞态

- 状态文件用「写临时文件 + `os.replace`」原子替换，读取不会看到半截 JSON
- **Plan 层已有**跨进程互斥锁：`plans/<pid>/run.lock`（`run_plan` 与 `rerun_plan` 执行期间持有，`finally` 释放）。
  原子建锁（`O_CREAT`+`O_EXCL`）；仅当持有者 pid 已死时清理残留并重试一次，**不按锁龄抢锁**（长任务不会被误判）。
  并发驱动同一 Plan 的第二个进程拿到 `run_lock_busy`（exit 3）
- 锁是 **per-plan**：同一 Schedule 的不同 attempt（`<sid>-a1` / `-a2`）互不阻塞
- `scheduler.log` 为追加写，多进程并发安全（操作系统级别追加语义）

---

## 8. 性能边界

| 项 | 量级 | 说明 |
| --- | --- | --- |
| Step 数量 | 数十到数百 | 每波重读状态文件，O(步骤数) |
| 单 Step 输出 | 建议 < 100 MB | 全量写入 `stdout.log`，无限增长 |
| 每 Plan 波次 | 上限 1000 | 超过视为异常，Stage 标 `failed` |
| 日志轮转 | 2 MB | 只保留 1 代（`.1`） |
| 快照 | 每 Plan 默认 5 份 | 含 plan/schedule/steps/summary 副本 |