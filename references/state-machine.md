# 状态机 — background-task-scheduler v2.9

本文描述 Task / Plan / Stage / Step / Schedule / Approval 六类实体的状态集合、迁移条件与对应事件。

**v2.1 变化**：Step 有三种 `skipped` 来源；自动重试中间失败不发 `task_failed`；新增 `plan_stalled`、`plan_resumed`、`steps_recovered`。

**v2.2 变化**：`stage_done` / `stage_failed` 事件携带 `waves`；Stage 暂停时 `waves` 落盘，`resume` 后累加不归零。

---

## 1. Task 状态机

### 1.1 状态集合

| 状态 | 终态 | 说明 |
| --- | --- | --- |
| `pending` | 否 | 已创建未启动 |
| `starting` | 否 | `run` 已派生监督进程，尚未确认子进程启动 |
| `running` | 否 | 子进程在跑 |
| `succeeded` | 是 | 主命令退出码 0（且无 `post_hook`，或钩子也返回 0） |
| `failed` | 是 | 非 0 退出、钩子非 0 退出，或监督进程丢失 |
| `timeout` | 是 | 超过 `timeout` 被杀 |
| `killed` | 是 | 被 `stop` 主动终止 |

### 1.2 迁移

| 从 | 到 | 触发 | 事件 |
| --- | --- | --- | --- |
| （新建） | `pending` | `create` | `task_created` |
| （新建） | `pending` | `pack`（一次 N 条） | `task_created`（每条一次） |
| `pending` | `starting` | `run`（权限通过） | `task_start` |
| `starting` | `running` | supervise 写入 pid | —（已在 `task_start` 中） |
| `starting`/`running` | `succeeded` | 主命令退出码 0 且钩子（若有）返回 0 | `task_end`（status=succeeded） |
| `starting`/`running` | `failed` | 主命令非 0 退出 | `task_end`（status=failed） |
| `starting`/`running` | `failed` | 主命令 0 退出但 `post_hook` 非 0（任务 `exit_code` 取钩子码） | `task_post_hook` + `task_end`（status=failed） |
| `starting`/`running` | `timeout` | 超时 | `task_end`（timed_out=true） |
| `starting`/`running` | `killed` | `stop` | `task_end`（status=killed） |
| `starting`/`running` | `failed` | `supervisor_pid` 消失且无 exit_code | `task_end`（note=supervisor_lost） |
| `failed`/`timeout`/`killed`/`succeeded` | `starting` | `run --force` | `task_start` |
| 任意终态 | （删除） | `delete` | 日志 `task_deleted` |

### 1.3 非法迁移

| 尝试 | 结果 |
| --- | --- |
| `run` 一个 `starting`/`running` | `state_conflict`（exit 3） |
| `delete` 一个 `starting`/`running` | `state_conflict`（exit 3），提示先 stop |
| `stop` 非运行中任务 | `not_running`（exit 3） |
| `run` 终态任务不给 `--force` | `rerun_requires_force`（exit 3） |

---

## 2. Approval 状态机

| 状态 | 可用 | 说明 |
| --- | --- | --- |
| `pending` | 否 | 等待决策 |
| `approved` | 是（未过期） | — |
| `rejected` | 否（永久） | — |
| `expired` | 否（永久） | 到期 |

| 从 | 到 | 触发 |
| --- | --- | --- |
| （新建） | `pending` | `create_approval` |
| `pending` | `approved` | `approve`（身份足够） |
| `pending` | `rejected` | `approve --reject` |
| `pending` | `expired` | `approve` 时发现已过期 / Plan 执行时发现已过期并重发 |
| 任意终态 | — | 无后向迁移 |

`approved` 记录在**使用**时才判过期。所以「已批准但过期」对外表现为 `expired`，文件里的 `status` 仍可能是 `approved`。

---

## 3. Plan 状态机

| 状态 | 终态 | 说明 |
| --- | --- | --- |
| `pending` | 否 | 已物化未执行 |
| `running` | 否 | 执行中 |
| `done` | 是 | 全部 Stage 结束且无失败 |
| `failed` | 是 | 至少一个 Stage 失败 |
| `paused` | 否 | 因审批暂停 |
| `cancelled` | 是 | 被 cancel |

| 从 | 到 | 触发 | 事件 |
| --- | --- | --- | --- |
| （物化） | `pending` | `materialize_plan` | `plan_materialized` |
| `pending` | `running` | `run_plan` | `plan_start` |
| `paused`/`running` | `running` | `resume` | `plan_resumed` |
| `running` | `paused` | Step 需审批 | `plan_paused` |
| `running` | `done` | 所有 Stage 完成 | `plan_end`（done） |
| `running` | `failed` | 有 Stage 失败 | `plan_end`（failed） |
| `paused`/`running` | `pending` | `rerun_task`（单步） | `task_rerun` |
| `done`/`failed`/`cancelled`/`paused` | `pending` | `rerun_plan`（整个 Plan） | `plan_rerun` |
| 未终态 | `cancelled` | `cancel` | `schedule_cancelled` |

**注意**：`run_plan` 对已 `done`/`failed`/`cancelled` 的 Plan 直接返回 `state_conflict`（exit 3），不执行、不写快照。要重跑请先用 `rerun_task`（单步）或 `rerun_plan`（整个 Plan）把状态重置为 `pending`。

同一 Plan 的 `run_plan`/`rerun_plan` 会持有 `plans/<pid>/run.lock`；已有驱动者时返回 `run_lock_busy`（exit 3）。

---

## 4. Stage 状态机

| 状态 | 说明 |
| --- | --- |
| `pending` | 未开始 |
| `running` | 执行中 |
| `done` | 所有 Step 终结且无失败 |
| `failed` | 有 Step 失败 / 死锁（`no_ready_steps`）/ `on_stage_failure=stop` 触发 / 关键任务失败触发即停 |
| `paused` | 因审批暂停 |
| `skipped` | 上游 Stage 失败且 `on_stage_failure=stop`，或即停命中（`critical_failed`） |

Stage `done` 的条件：所有 Step 处于终结态，且其中没有任何 `failed`/`timeout`。`skipped` 是终结态且不算失败。

---

## 5. Step 状态机

| 状态 | 终结 | 说明 |
| --- | --- | --- |
| `pending` | 否 | 未开始 / 被 reset |
| `running` | 否 | 进程在跑 |
| `succeeded` | 是 | 退出码 0 |
| `failed` | 是 | 非 0 退出 / 策略拒绝 |
| `timeout` | 是 | 超时被杀 |
| `skipped` | 是 | 四种来源（见 5.2） |

### 5.1 正常路径

| 从 | 到 | 触发 | 事件 |
| --- | --- | --- | --- |
| （物化） | `pending` | `default_step_state` | — |
| `pending` | `running` | `start_step` | `task_start` |
| `running` | `succeeded` | 退出码 0 | `task_done` |
| `running` | `failed` | 非 0 退出（且重试耗尽） | `task_failed` |
| `running` | `timeout` | 超时（且重试耗尽） | `task_failed`（status=timeout） |
| `running` | `pending` | 中断恢复 | `steps_recovered` |
| 终态 | `pending` | `rerun_task` | `task_rerun` |

**即停终止**：同批兄弟 Step 被 `kill_pid` 杀掉后同样落 `failed`（非 0 退出），但 `step.json` 带 `aborted_by="fail_fast"` + `aborted_at`，事件是 `task_aborted`（不再发 `task_failed`）。

### 5.2 跳过来源

| `skipped_by` | 触发 | 事件 |
| --- | --- | --- |
| `dependency` | 上游 `failed`/`timeout`/`skipped` | `task_skipped`（带 `blocked_by`、`dependency_status`） |
| `user` | `cancel` schedule | 仅汇总 |
| `stage_failed` | `on_stage_failure=stop` 时剩余 Step | `task_skipped` |
| `critical_failed` | 关键任务最终失败触发即停时，当前 Stage 剩余 Step | `task_skipped` |

### 5.3 重试路径

```
pending → running(attempt 1) → failed(中间)
        → [task_retry] → running(attempt 2) → failed(最终) → [task_failed]
        → [task_retry] → running(attempt 2) → succeeded → [task_done]
```

关键点：

- 中间失败时 `step.json` 的 `status` **确实**短暂为 `failed`，但对外只发 `task_retry`
- 只有最后一次仍失败才发 `task_failed`
- `attempts` 每 `start_step` +1；`max_attempts = 1 + retry`
- 重试会重置 `stall_delivered=false`，允许新一次停滞检测

### 5.4 策略拒绝

```
pending → failed(error_type=policy_denied)
```

事件为 `task_failed` 带 `error_type=policy_denied`，附 `required_level`、`policy_max`。
这类失败**不**受 `retry` 影响（`ensure_step_permission` 直接落终态，不进入 `finish_with_retry`）。

---

## 6. Schedule 状态机

| 状态 | 说明 |
| --- | --- |
| `active` | 等待/正在触发 |
| `paused` | 暂停（保留 `next_run`） |
| `cancelled` | 已取消 |
| `done` | 达到 `max_runs` |

| 从 | 到 | 触发 |
| --- | --- | --- |
| （提交） | `active` | `submit_plan` / `submit_schedule` |
| `active` | `done` | 达到 `max_runs` |
| `active` | `paused` | `pause`，或触发的 Plan 暂停 |
| `paused` | `active` | `resume_schedule`（保留 `next_run`，需 L1 审批），或手动改 `schedule.json` 的 `status` |
| `active`/`paused` | `cancelled` | `cancel`，或 `delete` |

**注意**：Plan 暂停会把 Schedule 也置 `paused`，但不会自动改回 `active`。
`check_schedule` 对 `paused` 状态不做触发。恢复顺序：先 `approve` + `resume <sid>` 处理审批并跑完 Plan，
再 `resume_schedule <sid>` 把 Schedule 改回 `active`（保留 `next_run`；为空时按 `every` 重算）。
`resume_schedule` 走 L1 审批门，被拦时带 `--approval` 重发；也可直接编辑 `schedule.json` 的 `status`。

---

## 7. 事件 — 状态映射速查

| 事件 | 状态变化 |
| --- | --- |
| `task_created` | Task → pending |
| `task_start` | Task → starting；Step → running |
| `task_end` | Task → succeeded/failed/timeout/killed |
| `task_post_hook` | 无独立状态变化；钩子非 0 时随后的 `task_end` 落 `failed` |
| `task_done` | Step → succeeded |
| `task_failed` | Step → failed/timeout（最终） |
| `task_retry` | Step 短暂 failed 后 pending |
| `task_skipped` | Step → skipped |
| `steps_recovered` | Step running → pending |
| `task_rerun` | Step 终态 → pending |
| `plan_materialized` | Plan → pending |
| `plan_start` | Plan → running |
| `plan_resumed` | Plan paused → running |
| `plan_paused` | Plan → paused；Stage → paused |
| `plan_stalled` | 无状态变化（告警） |
| `plan_end` | Plan → done/failed |
| `stage_done` | Stage → done |
| `stage_failed` | Stage → failed |
| `stage_skipped` | Stage → skipped |
| `schedule_created` | Schedule → active |
| `schedule_triggered` | Schedule `runs` +1，`next_run` 重写为 `now + every`（或 `null`，若已达 `max_runs`） |
| `schedule_paused` | Schedule → paused |
| `schedule_cancelled` | Schedule → cancelled |
| `run_snapshot` | 无状态变化（落盘） |
| `prune_runs` | 无状态变化（删文件） |
| `approval_created` | Approval → pending |
| `approval_resolved` | Approval → approved/rejected |

---

## 8. 状态一致性规则

1. **Plan 不可能是 `done` 而 Step 仍 `pending`**：`run_plan` 只有全部终结才收尾
2. **Stage `done` 蕴含所有 Step 终结**（由 `run_stage` 末尾判定）
3. **`paused` 的 Plan 必有 `paused_stage` 与 `paused_step`**
4. **`failed` 的 Step 必有 `exit_code`**（`policy_denied` 除外，其 `exit_code=null`）
5. **`skipped` 的 Step 必有 `skipped_by`**（`cancel` 场景为 `user`）
6. **`timeout` 的 Task/Step 必有 `timed_out=true`**（Step 用 `status=timeout`）
7. **Schedule `done` 必有 `next_run=null`**

若发现违反，通常意味着状态文件被外部改写或版本不兼容。可用 `show` / `history` 定位，并用 `rerun_task` 或直接改 `step.json` 修复。

---

## 9. 修复手段

| 现象 | 修复 |
| --- | --- |
| Plan 卡 `running` 但无进程 | 再跑一次 `run_plan`，`recover_interrupted` 会把残留 `running` Step 复位 |
| Step 永久 `pending` 且无 ready | 检查 `depends_on` 指向的 Step 状态；必要时 `rerun_task` 重置链 |
| Step 卡 `running` | 手工改 `step.json` 的 `status=pending`，或 `rerun_task` |
| Stage `failed(no_ready_steps)` | 说明依赖图在该轮无法推进，检查校验是否漏掉环 |
| Schedule `paused` 不自动恢复 | 批准审批后把 `schedule.json` 的 `status` 改回 `active` |
| 快照被意外删除 | 不影响运行状态，只是少一份历史 |