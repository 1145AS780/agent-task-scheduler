# 错误分类与重试矩阵 — background-task-scheduler v2.7

本文把调度器可能产生的一切「不正常」拆成可判别的类别，给出**判别依据**（退出码 / JSON 字段 / 事件 / 文件）、**含义**与**处置动作**，并说明哪些错误可以交给 `retry` 自动消化、哪些必须人工改代码。

阅读顺序建议：先看第 1 节的「三层错误」划分，再按下表定位你手上的现象。

---

## 1. 三层错误：不要把它们混为一谈

调度器把「出错」拆成三个互不相同的信号层，任何一层都不能替代另一层。

| 层 | 载体 | 回答的问题 | 谁负责 |
| --- | --- | --- | --- |
| 调度器错误 | 进程退出码（0/2/3/4）+ JSON 顶层的 `error` | 调度器自己有没有把这次调用办成？ | 调度器 |
| 业务错误 | JSON 里的 `status` / `exit_code` / `timed_out` | 被调用的命令本身成没成？ | 你的命令 |
| 运行异常 | 事件里的 `error_type` / `skipped_by` / `note` | 为什么没成？是策略拦的、依赖断的，还是进程丢了？ | 调度器 + 编排语义 |

**最重要的推论**：命令失败（非 0 退出）时调度器的退出码仍然是 `0`。这是刻意设计——否则 Agent 会把「测试没通过」误读成「调度器坏了」，进而重试调度器而不是重试测试。

```bash
# 命令失败，但调度器正常返回 0
python scripts/bts.py run --wait tests1
# exit code of this python process = 0
# 而 JSON 里：{"task": {"status": "failed", "exit_code": 1, ...}}
```

**反例（错误做法）**：用 `$LASTEXITCODE` 判断任务成败。

```powershell
python scripts/bts.py run --wait tests1
if ($LASTEXITCODE -ne 0) { Write-Host "任务失败" }   # 永远是 false 分支
```

正确做法：解析 JSON 的 `status`。

```powershell
$out = python scripts/bts.py run --wait tests1 | ConvertFrom-Json
if ($out.task.status -ne "succeeded") { Write-Host "任务失败: $($out.task.status)" }
```

---

## 2. 退出码总表

| 码 | 常量名 | 语义 | 出现位置 | 是否可自动重试 |
| --- | --- | --- | --- | --- |
| 0 | `EXIT_OK` | 调用成功（业务成功与否另看 `status`） | 全部命令 | — |
| 2 | `EXIT_USAGE` | 参数或 spec 非法，调用根本没进入业务 | `create` / `pack` / `submit_plan` / `submit_schedule` / `prune` 等 | 否，必须先改输入 |
| 3 | `EXIT_CONFLICT` | 目标不存在，或当前状态不允许该动作 | 几乎所有命令 | 视情况（见第 4 节） |
| 4 | `EXIT_DENIED` | 权限不足或被策略拒绝 | `run` / `stop` / `delete` / `pause` / `cancel` / `rerun_task` / `resume` 等 | 否，需要审批或改策略 |

补充约定：

- `check` / `list` / `show` / `status` / `events` / `logs` / `policy` / `history` 这些只读命令**只会**返回 0 或 3，不会返回 4。
- `run_plan` 遇到 Step 需要审批时返回 **0** 且 `status=paused`，不是 4。因为「暂停」是编排的正常中间态，不是调用失败。
- `check_schedule` 永远返回 0（除非 schedule 不存在 → 3）。停滞只是一条告警数据，不是错误。
- `prune --dry-run` 返回 0，无论是否有待删快照。
- `download` / `torrent` 的 worker 有独立退出码（download 0/1/2，torrent 0/1/2/3），它写进 `task.exit_code`，**不是调度器退出码**；torrent 的 `3`（找不到 aria2c）与 `EXIT_CONFLICT` 无关（见 4.4）。

---

## 3. 调度器错误目录（按 `error` 字段值检索）

下表是**全部**会出现在 JSON 顶层 `error` 字段的取值。Agent 应当用 `error` 而不是 `exit_code` 做分支，因为同一个退出码可能对应多种原因。

### 3.1 输入类（`exit 2`）

| `error` | 触发命令 | 含义 | 处置 |
| --- | --- | --- | --- |
| `spec_not_found` | `create --spec` / `submit_plan` / `submit_schedule` | spec 文件路径不存在 | 检查路径；注意相对路径按**当前工作目录**解析，不是按 `BTS_ROOT` |
| `spec_invalid` | `create --spec` / `bts_plan` 两个 `submit_*` | spec 不是 JSON 对象（数组/字符串/空文件/null） | 用 `Get-Content spec.json \| ConvertFrom-Json` 先自检 |
| `missing_command` | `create` | 既没 `--command` 也没 spec 的 `command` | 补 `--command` |
| `bad_env` | `create --env` | `--env` 的值不含 `=` | 写成 `--env K=V`；值里可以有 `=`（按第一个 `=` 切分） |
| `bad_level` | `create --level` | 等级不在 `L0..L3`（argparse 会先拦，但 spec 里的 `level` 会走到这里） | 用大写 `L0`-`L3` |
| `bad_timeout` | `create` | `timeout` 不是整数（通常来自 spec 的字符串） | spec 里写数字，不要写 `"900"` |
| `bad_cwd` | `create` / `pack` | `cwd` 路径不存在 | 先确认目录存在；`~` 会被展开 |
| `bad_hook` | `create` | `@enc` 前后缺内容（主命令为空或钩子参数为空） | 写成 `<命令> @enc <参数>` |
| `bad_hook_item` | `pack` | 某条目 `@enc` 前后缺内容（`indexes` 指出位置，1 起） | 修正该条目；**整包不落盘** |
| `pack_empty` | `pack` | 传入内容为空（或只有空白） | 至少给一条命令 |
| `pack_empty_item` | `pack` | 逗号之间出现空项（`indexes` 指出位置，1 起） | 去掉多余逗号；命令内部的逗号写 `\,`；**整包不落盘** |
| `missing_approval_id` | `approve` | 既没给 id 也没 `--all` | 补 id 列表或 `--all` |
| `no_approvals` | `approve --all` | `approvals/` 里没有 `pending` 记录 | 通常说明已被处理过 |
| `missing_plan_spec` | `submit_schedule` | schedule spec 里没有 `plan` 键 | 补 `"plan": "./plan.json"` |
| `plan_spec_not_found` | `submit_schedule` | `plan` 指向的文件不存在 | 检查相对路径；`plan` 是相对**当前工作目录**，不是相对 schedule spec |
| `plan_spec_invalid` | `submit_schedule` | Plan spec 不是 JSON 对象 | 同上 |
| `every_invalid` | `submit_schedule` | `every` 缺失或 `<=0` 或非整数 | 写正整数秒 |
| `max_runs_invalid` | `submit_schedule` | `max_runs` 非整数或 `<=0` | 省略或写正整数 |
| `start_at_invalid` | `submit_schedule` | `start_at` 非整数 | 写 Unix 秒整数；`0` 合法（表示首轮立即触发） |
| `spec_invalid` + `errors[]` | `submit_plan` / `submit_schedule` | Plan spec 通过了 JSON 解析，但违反 V1-V12 规则 | 逐条读 `errors` 数组（见 3.2） |
| `missing_url` | `download` | 既没 `--url` 也没有可用的清单条目 | 补 `--url` 或 `--file` |
| `bad_url` | `download` | 条目 URL 不是 `http://` / `https://` | 下载器只做 HTTP(S)，`ftp://`、`file://` 需自己先转 |
| `bad_header` | `download --header` | 头不含 `:` 或键为空 | 写成 `--header "K: V"` |
| `name_requires_single_url` | `download --name` | `--name` 需要且只允许 1 个 `--url` | 多个 URL 用清单的 `save_as` |
| `digest_requires_single_url` | `download --sha256/--size` | 合并后条目数 > 1 | 多条目请在清单里逐条写 `sha256`/`size` |
| `too_many_items` | `download` | 条目数 > 500 | 拆成多批任务 |
| `name_conflict:<name>` | `download` | 不同 URL 映射到同一目标文件名（并发会互相覆盖） | 用 `save_as` 显式区分文件名 |
| `manifest_not_found` / `manifest_empty` | `download --file` | 清单不存在 / 去除注释与空行后没有条目 | 检查路径；注释用 `#` |
| `manifest_invalid` | `download --file` | `.json` 清单不是对象也不是数组，或对象缺 `items` 数组 | 用数组或 `{"outputs": "...", "items": [...]}` |
| `item_missing_url` | `download --file` | 清单里某条缺 `url` | 补 `url` 字段/列 |
| `magnet_invalid` | `torrent --magnet` | 链接不以 `magnet:` 开头 | 补全磁力链 |
| `torrent_not_found` | `torrent --torrent` | `.torrent` 文件不存在 | 检查路径；相对路径按**当前工作目录**解析 |
| `aria2_not_found` | `torrent` | 创建时在 `BTS_ARIA2` / `PATH` / 常见安装目录都没找到 aria2c | 安装 aria2 或设 `BTS_ARIA2`（见 [SKILL.md](../SKILL.md) §4.4） |

### 3.2 `spec_invalid` 的 `errors[]` 取值（Plan 校验规则）

`errors` 是数组，一次可以返回**多条**。每个元素都是稳定的字符串键，含出错的 stage / step 定位。

> [orchestration-spec.md](orchestration-spec.md) §3.2 把校验归并为 12 条**规则**（V1-V12）；下表按**原子错误键**展开为 19 行——一条规则可能对应多种键（例如「Stage id 合法」同时产出 `id_required` 与 `id_duplicate`）。两表互补，不冲突。

| 规则 | `errors` 元素形态 | 含义 |
| --- | --- | --- |
| V1 | `spec_not_object` | 顶层不是对象 |
| V2 | `stages_required` | `stages` 缺失、为空、不是数组 |
| V3 | `stage[i].not_object` | 第 i 个 stage 不是对象 |
| V4 | `stage[i].id_required` | stage 缺 `id` |
| V5 | `stage[i].id_duplicate:<id>` | 同一 Plan 内 stage id 重复 |
| V6 | `stage[<id>].mode_invalid` | `mode` 不是 `serial` / `parallel` |
| V7 | `stage[<id>].steps_required` | stage 的 `steps` 缺失、为空、不是数组 |
| V8 | `stage[<id>].step[j].not_object` | 第 j 个 step 不是对象 |
| V9 | `stage[<id>].step[j].id_required` | step 缺 `id` |
| V10 | `stage[<id>].step[<sid>].id_duplicate` | 同 Stage 内 step id 重复 |
| V11 | `stage[<id>].step[<sid>].command_required` | step 缺 `command`（空串也算缺） |
| V12 | `stage[<id>].step[<sid>].level_invalid` | `level` 不在 L0-L3 |
| V13 | `stage[<id>].step[<sid>].retry_invalid` | `retry` 非整数 / 负数 / 布尔 |
| V14 | `stage[<id>].step[<sid>].retry_backoff_invalid` | `retry_backoff` 非整数 / 负数 |
| V15 | `stage[<id>].step[<sid>].timeout_invalid` | `timeout` 非整数 / 负数 |
| V16 | `stage[<id>].step[<sid>].depends_on_invalid` | `depends_on` 既不是字符串也不是字符串数组 |
| V17 | `stage[<id>].step[<sid>].depends_on_self` | 自己依赖自己 |
| V18 | `stage[<id>].step[<sid>].depends_on_unknown:<dep>` | 依赖了**同 Stage 内**不存在的 step |
| V19 | `stage[<id>].depends_on_cycle` | 同 Stage 内依赖成环 |

> 注意 V18 的关键词是「同 Stage 内」。跨 Stage 写 `depends_on` 一定会命中 `depends_on_unknown`，这是最常见的一条报错。正确做法是把依赖关系改成 Stage 顺序。

### 3.3 状态冲突类（`exit 3`）

| `error` | 触发命令 | 含义 | 处置 |
| --- | --- | --- | --- |
| `task_exists` | `create` / `torrent` | 同名 task 已存在且没 `--force` | 换 id，或 `--force` 覆盖（会丢原记录） |
| `not_found` | Task 层多命令 / Plan 层多命令 | id 找不到（task / plan / schedule / ident） | 用 `list` 找真实 id；Plan 层 id 带 `-a<N>` 后缀 |
| `state_conflict` | `run` / `delete` / `pause` / `show`(间接) / `run_plan` / `rerun_task` / `resume_schedule` | 当前状态不允许该动作 | 见下方分状态说明 |
| `run_lock_busy` | `run_plan` / `rerun_plan` | 同一 Plan 已有驱动者持有 `plans/<pid>/run.lock` | 等对方结束；持有者进程已死时锁会被自动清理后重试 |
| `rerun_requires_force` | `run` | 任务已是终态却没给 `--force` | 加 `--force` |
| `not_running` | `stop` | 任务不在 `starting`/`running` | 无需停；先 `check` 看真实状态 |
| `schedule_exists` | `submit_plan` / `submit_schedule` | `schedules/<id>/schedule.json` 已存在 | 换 `--id`，或先 `delete` 旧实体 |
| `plan_done` | `resume` | Plan 已 `done`，没有可继续的步骤 | 若要重跑某步用 `rerun_task` |
| `already_cancelled` | `cancel` | 已被取消过 | 无需重复取消 |
| `no_current_plan` | `rerun_task` | schedule 还没有 `current_plan` | 先 `run_plan` / `check_schedule` 触发一次 |
| `stage_not_found` | `rerun_task` | 指定 stage 不在当前 plan | 用 `show` 看 stage 列表 |
| `step_not_found` | `rerun_task` | 指定 step 不在该 stage | 用 `show` 看 step 列表 |

`state_conflict` 的分状态语义：

| 目标状态 | 尝试的动作 | 为什么不允许 |
| --- | --- | --- |
| `starting` / `running` | `run` | 同一个任务不能有两个监督进程并发写同一批文件 |
| `starting` / `running` | `delete` | 文件正在被写，删了会留下孤儿进程 |
| 任何非运行态 | `stop` | 没有进程可杀（走 `not_running`） |
| `paused` 且无 `--approval` | `resume` | 需要先批审批，命令会返回 `status=paused` 提示而不是报错 |
| `done` | `resume` | 走 `plan_done` |
| `done`/`failed`/`cancelled` | `run_plan` | 终态不可重跑；需 `rerun_plan`（整个 Plan）或 `rerun_task`（单步） |
| 非 `paused` | `resume_schedule` | 只有 `paused` 的 Schedule 才需要恢复 |

### 3.4 权限类（`exit 4`）

| `error` | 含义 | 关键字段 | 处置 |
| --- | --- | --- | --- |
| `denied_policy` | 命令等级 > `max_level`，**审批不可绕过** | `level`、`policy_max` | 改 `policy.json` 的 `max_level`，或降低 step/task 的 `level` |
| `approval_required` | 等级 > `actor_level`，且没有有效审批 | `approval_id`、`resume`、`approval_error` | 走 `approve <id>` 后再执行 |
| `insufficient_level` | `approve` 时批准者身份低于审批要求等级 | `approval_id`、`required_level`、`actor_level` | 用更高 `BTS_ACTOR_LEVEL` 的进程批准 |
| `already_approved` | `approve` 一条已批准的记录 | `approval_id` | 直接执行，无需再批 |
| `already_rejected` | `approve` 一条已拒绝的记录 | `approval_id` | 拒绝不可翻案，重新发起 |
| `already_expired` | `approve` 一条已过期的记录 | `approval_id` | Plan 层会自动重发；Task 层需重新 `run` 触发新审批 |

`approval_required` 时附带的 `approval_error` 取值，是**真正原因**：

| `approval_error` | 含义 | 处置 |
| --- | --- | --- |
| `missing` | 没给 `--approval` | 先 `run` 一次拿到 `approval_id`，批准后再带 `--approval` |
| `pending` | 给了 id，但还没批准 | `approve <id>` |
| `expired` | 记录存在但已过有效期（含 `status=approved` 但 `expires_at` 已过） | 重新发起审批 |
| `rejected` | 该审批已被拒绝 | 重新发起（同 id 永久失效） |
| `mismatch` | 审批绑定的 `command` 与当前命令原文不一致 | 为**新的命令文本**重新申请审批 |
| `insufficient_level` | 身份不足以批准（只在 `approve` 阶段出现） | 提升批准者等级 |

`approve` 的退出码细则：单条 `approve <id>` 失败时，`insufficient_level` → **4**，`missing` / `expired` / `already_approved` / `already_rejected` / `already_expired` → **3**；`approve --all` 一律 **0**（批量语义，逐项成败看 `approvals[].ok`）。所以判断批准是否生效，优先看 `ok`，其次才是退出码。

### 3.5 命令与权限门的对应关系

不同命令用的等级是**写死**的，不是从任务继承：

| 命令 | 操作等级 | 白名单匹配文本 |
| --- | --- | --- |
| `run` | task 的 `level`（缺省 L1） | 任务命令原文 |
| `stop` | L1 | `stop <task_id>` |
| `delete`（task） | L2 | `delete <task_id>` |
| `pause` | L1 | `pause <schedule_id>` |
| `cancel` | L2 | `cancel <schedule_id>` |
| `rerun_task` | L1 | `rerun <schedule>:<stage>:<step>` |
| `delete`（plan/schedule） | L2 | `delete <ident>` |

所以「task 是 L1，为什么 delete 还要审批」的答案是：删除动作本身定级 L2，与任务等级无关；而 `stop` / `pause` / `rerun_task` 是可恢复动作，只定级 L1。

白名单匹配的是**上表第三列那条合成文本**，不是你原来提交的命令。所以想让 `stop` 免审批，`allow_rules` 要写 `^stop `，写 `^npm` 没有用。

---

## 4. 执行侧错误类型（Step / Task 的失败原因）

### 4.1 Task 的终态与原因

| `status` | 判别依据 | 含义 |
| --- | --- | --- |
| `succeeded` | `exit_code == 0` | 正常结束（带 `@enc` 钩子时还要求 `post_hook.status == "ok"`） |
| `failed` | `exit_code != 0` 且 `note != "supervisor_lost"` | 命令自己失败 |
| `failed` | `post_hook.status == "failed"` | 主命令成功但钩子失败；任务 `exit_code` 取钩子退出码，细节在 `hook-enc.log` |
| `failed` | `exit_code == null` 且 `note == "supervisor_lost"` | 监督进程消失，无法确认子进程结果 → 保守判失败 |
| `timeout` | `timed_out == true` | 超时被杀（`exit_code` 常为 `-1`） |
| `killed` | 由 `stop` 写入 | 人为终止 |

**`post_hook.error_type`**（写在 `task.json` 的 `post_hook`，不进顶层 `error`）：

| 值 | 含义 | 处置 |
| --- | --- | --- |
| `encoder_not_found` | `BTS_ENCODER_PATH`、`<cwd>/tools/`、`<当前目录>/tools/` 都没找到 `encoding_converter.py` | 设 `BTS_ENCODER_PATH` 或把转换器放到任务 `cwd` 的 `tools/` 下 |
| `hook_timeout` | 钩子执行超过任务的 `timeout` 被强杀 | 放宽 `--timeout` 或缩小转换范围 |
| `hook_spawn_failed` | 启动转换器进程抛 `OSError` | 检查 Python 解释器与转换器路径 |

`supervisor_lost` 的成因：机器重启、进程被外部杀、`taskkill` 打到父进程但没打到子进程。它**不代表命令失败**，只是「没人记账了」。处置：`run --force` 重跑。

另一个成因是**宿主终端的 Job Object**：supervisor 被启动命令的进程树回收（命令一结束就整棵被杀）。`task.json` 的 `detach_mode` 可区分：`wmi` / `breakaway` 表示已脱离；若为 `detached`（兜底路径），说明宿主既拒绝 breakaway、WMI 也失败，任务可能在发起命令结束时就消失。

### 4.2 Step 的 `error_type`

Step 没有 `note` 字段，用 `error_type` 与 `skipped_by` 表达原因。

| `error_type` | 出现位置 | 含义 | 是否受 `retry` 影响 |
| --- | --- | --- | --- |
| `policy_denied` | `step.json` + `task_failed` 事件 | 策略上限拒绝（`level > max_level`） | **否**，直接落 `failed`，不重试 |
| `no_ready_steps` | `stage.json` + `stage_failed` 事件 | 该轮没有任何 ready 的 step，也无法判定完成 → 死锁保护 | 否，属于编排错误 |

`policy_denied` 的完整字段：`step.json` 里 `status=failed`、`exit_code=null`、`error_type=policy_denied`；事件带 `required_level` 与 `policy_max`。它**不会**创建审批——因为审批本来就不能绕过策略上限。

`no_ready_steps` 的成因几乎总是：依赖图里存在校验漏掉的环（例如 spec 在校验后被外部改过），或 `depends_on` 指向的 step 被删了。处置：修 spec → 删除该 plan → 重新提交。

### 4.3 `skipped_by` 的四种来源

| `skipped_by` | 触发 | 附字段 | 事件 |
| --- | --- | --- | --- |
| `dependency` | 上游 `failed` / `timeout` / `skipped` | `blocked_by`、`dependency_status` | `task_skipped` |
| `user` | schedule 被 `cancel` | 无 | `task_skipped`（仅汇总） |
| `stage_failed` | `on_stage_failure=stop` 时剩余 step | 无 | `task_skipped` |
| `critical_failed` | 关键任务最终失败触发即停时剩余 step | 无 | `task_skipped` |

识别「这是不是我的错」的经验法则：

- `skipped` 且 `blocked_by` 有值 → 看 `blocked_by` 指向的那个 step，问题在**上游**
- `skipped` 且 `skipped_by=user` / `stage_failed` / `critical_failed` → 不是该 step 自己失败：`user` 是主动取消，后两者是上游/关键任务把它拖停的
- `failed` 且 `error_type=policy_denied` → 改策略或降等级，重跑命令没用
- `failed` 且 `exit_code` 有值 → 命令自己的问题，去看 `stderr.log`

### 4.4 下载 / 种子 worker 的失败原因

`download` / `torrent` 的失败原因写在 `tasks/<id>/stdout.log` 的 JSONL 里，`task.json` 只有 `exit_code` 与 `status`。判别顺序：先看最后一行 `summary.error`，再看 `torrent_error` / `item_error` 的 `detail`。

Torrent worker（`bts_torrent.py`）的退出码与 `summary.error`：

| exit | `error` | 含义 | 处置 |
| --- | --- | --- | --- |
| 0 | —（`summary.status=complete`） | 下完，或做种时长已到 | — |
| 1 | `aria2_start_failed` | aria2c 起不来，或 RPC 在 20 秒内没就绪 | 看 `aria2.out.log`；排查端口、权限、杀软拦截 |
| 1 | `aria2_add_failed` | RPC 拒绝了该种子/磁力链 | 看 `rpc_code` + `detail`；链接失效或元数据下载失败 |
| 1 | `aria2_error_<code>` | 下载中途 aria2 报错，`code` 原样取自 `errorCode` | 按 `detail` 处理；常见 3=资源不存在、9=磁盘空间不足、6=网络错误 |
| 1 | `download_removed` | 任务在 aria2 里被移除 | 通常是外部操作；`run --force` 重来 |
| 1 | `internal_error` | worker 自身异常 | 看 `traceback` |
| 2 | `spec_error`（`magnet_invalid` / `torrent_not_found` / `source_type_invalid` / `spec_missing_source`） | `torrent.json` 非法 | 正常不会发生（`bts.py torrent` 已先校验）；手工改 spec 才会 |
| 3 | `aria2_not_found` | 任务创建后 aria2c 被卸载/移走，重新解析也失败 | 重新安装，或在 `torrent.json` 里钉住存在的 `aria2`；`run --force` |

**注意**：这里的 `3` 是 **worker 退出码**（写进 `task.exit_code`），与调度器的 `EXIT_CONFLICT=3` 无关。判断调用本身是否成功，看 `bts.py` 返回的 JSON 与调度器退出码；`aria2_not_found` 若发生在**创建阶段**，`bts.py` 会直接返回 `exit 2`，不会生成任务。

---

## 5. 证据在哪：错误 → 文件/命令映射

| 想知道 | 查哪里 | 命令 |
| --- | --- | --- |
| Task 的最终状态与退出码 | `tasks/<id>/task.json` | `bts.py show <id>` |
| 命令的实际报错文本 | `tasks/<id>/stderr.log` | `bts.py show <id> --lines 80` |
| 种子下载失败原因 | `tasks/<id>/stdout.log` 末行 `summary` + `torrent_error` | `bts.py show <id> --lines 40` |
| 指令包条目为什么没跑 | 条目 `task.json` 的 `status`（`pack` 只登记文本，恒为 `pending`） | `bts.py list --pack <pack_id>` |
| Step 的退出码与尝试次数 | `step.json` 的 `exit_code` / `attempts` | `bts_plan.py show <plan_id>` |
| Step 的 stdout / stderr | `step 目录/stdout.log`、`stderr.log` | 直接读文件 |
| 为什么被跳过 | `step.json` 的 `blocked_by` / `dependency_status` | `bts_plan.py show` |
| 哪些审批还挂着 | `approvals/*.json` 中 `status=pending` | `bts.py approve --all` 会列出；也可直接读目录 |
| 某个 plan 的完整时间线 | `scheduler.log`（按 `plan_id` 过滤） | `bts.py logs --lines 100 --json` |
| 历史尝试汇总 | `runs/<plan_id>-a<N>/summary.json` | `bts_plan.py history <sid>` |
| 是否卡住 | `plan_stalled` 事件 | `bts_plan.py check_schedule <sid>` 的 `stalled` 字段 |
| 权限判定细节 | `task_start` 事件的 `gate` 字段；`policy` 命令 | `bts.py policy --json` |

`task_start` 事件的 `gate` 取值直接说明了放行原因：`whitelist` / `level` / `approval`。这三个值就是「为什么它被允许跑」的答案。

---

## 6. 重试矩阵

`retry` 只对 **Step** 生效（Task 层没有 retry 字段，需要外部循环）。

### 6.1 该不该配 retry

| 错误类别 | 典型表现 | 建议 `retry` | 建议 `retry_backoff` | 理由 |
| --- | --- | --- | --- | --- |
| 网络抖断 | `curl` 28、`npm` ECONNRESET、`pip` 超时 | 2-3 | 10-30 | 瞬时性，重试大概率成功 |
| 分布式锁竞争 | `database is locked`、`Another process is using` | 2-5 | 5-15 | 等待窗口即可 |
| 临时资源不足 | OOM、`Too many open files` | 1-2 | 30-60 | 给系统回收时间 |
| 端口占用 | `EADDRINUSE` | 1-2 | 5-10 | 前一个实例可能正在退 |
| 下载/克隆 | `git clone` 断流、镜像 5xx | 2-3 | 10-20 | 幂等且瞬时 |
| 编译失败 | `tsc` 报类型错误 | 0 | — | 确定性错误，重试只是浪费 |
| 测试断言失败 | `pytest` assertion | 0 | — | 确定性错误 |
| 参数/配置错误 | `unknown option`、`invalid config` | 0 | — | 输入没变，结果不变 |
| 权限拒绝 | `policy_denied`、`approval_required` | 0（且无效） | — | 本层不进入重试逻辑 |
| 依赖未就绪 | `skipped(dependency)` | 0（且无效） | — | 跳过是终态，不会重试 |
| 超时 | `status=timeout` | 1（谨慎） | 30-60 | 只在「慢但可能成功」时用；`timeout` 值要同时调大 |
| 非幂等写操作 | 转账、发版、`rm -rf` | 0 | — | 重复执行后果不可控 |

**边界提醒**：`retry` 计数的是「额外重试次数」，`max_attempts = 1 + retry`。`retry: 2` 意味着最多共跑 3 次。

### 6.2 重试期间发生了什么

```
attempt 1 失败
  → 发 task_retry（attempt=1, max_attempts=3, prev_error_type=failed/timeout, backoff_seconds）
  → sleep(backoff)
  → start_step：attempts=2, stall_delivered 重置 false
  → attempt 2 若再失败 → 再发 task_retry（attempt=2）
  → attempt 3 成功 → 发 task_done（attempt=3）
  → attempt 3 仍失败 → 发 task_failed（最终）
```

要点：

- 中间失败**不发** `task_failed`，所以只统计 `task_failed` 不会把抖动算成失败
- `step.json` 在中间重试期间确实会短暂写成 `failed`，这是真实写入；不要用它做最终判断，看 `attempts` 与最后事件
- `stdout.log` / `stderr.log` 是追加写，重试不会清空历史 → 可以用 `attempt N` 的时间边界切开分析
- 每次 `start_step` 都会把 `stall_delivered` 复位，所以每次尝试都能触发一次停滞报警

### 6.3 外部重试（Task 层）

Task 没有 `retry`，需要外部循环：

```powershell
$max = 3
for ($i = 1; $i -le $max; $i++) {
  python scripts/bts.py create job1 --command "npm run build" --level L1 --timeout 900 --force | Out-Null
  $out = python scripts/bts.py run job1 --wait --force | ConvertFrom-Json
  if ($out.task.status -eq "succeeded") { break }
  if ($i -eq $max) { Write-Host "3 次均失败" ; exit 0 }
  Start-Sleep -Seconds (10 * $i)
}
```

也可以把「带重试的单条命令」包成一个 Step 放进 Plan，交给调度器做——这是更推荐的方式，因为能看到 `attempts` 与 `task_retry` 事件。

---

## 7. 诊断决策树

按顺序判断，每步只和解一个歧义。

```
调用返回了什么？
├─ 退出码 2
│   └─ 读 JSON 的 error
│       ├─ spec_*        → 检查文件路径 / JSON 合法性
│       ├─ *_invalid     → 看 errors[] 或字段名，改 spec
│       └─ bad_*         → 单个字段格式问题
├─ 退出码 3
│   └─ 读 JSON 的 error
│       ├─ not_found     → list 找真实 id（注意 -a<N> 后缀）
│       ├─ *_exists      → 换 id 或先 delete
│       ├─ state_conflict→ 看当前 status，按 §3.3 的表处理
│       └─ plan_done     → 若要局部重跑用 rerun_task
└─ 退出码 4
    └─ 读 error 与 approval_error
        ├─ denied_policy      → 改 max_level 或降 level（审批无用）
        └─ approval_required  → 按 approval_error 分支
            ├─ missing  → 拿 id → approve → 带 --approval 重跑
            ├─ pending  → approve
            ├─ expired  → 重新触发，拿新 id
            ├─ rejected → 重新触发
            └─ mismatch → 命令变了，按新文本重新申请
```

退出码 0 但你觉得「没跑成」：

```
status / counts 是什么？
├─ Task: failed / timeout / killed
│   └─ show --lines 80 看 stderr；supervisor_lost 则直接 run --force
├─ Plan: paused
│   └─ 找到 approval_id → approve → resume --approval
├─ Plan: failed
│   └─ 找 failed / timeout 的 step
│       ├─ error_type=policy_denied → 策略问题
│       └─ 有 exit_code → 命令问题，读该 step 的 stderr.log
├─ Plan: done 但下游都是 skipped
│   └─ 找 blocked_by 指向的上游
└─ 什么都没发生（status 没变）
    └─ 可能所有 step 都在跑 → check_schedule 看 stalled
```

---

## 8. 停滞告警（`plan_stalled`）

停滞**不是错误**，是「怀疑卡住」的告警。它有两种 scope，都在 `check_schedule` 返回的 `stalled` 数组里。

| scope | 判据 | 字段 | 含义 |
| --- | --- | --- | --- |
| `step` | 某 step `status=running` 且已运行 ≥ `stall_seconds` | `stage_id`、`step_id`、`quiet_seconds`、`threshold`、`attempt` | 单个命令跑太久 |
| `plan` | plan `status=running` 且距最近一条同 plan 日志 ≥ `stall_seconds` | `quiet_seconds`、`threshold`、`status` | 整个 plan 没有任何进展记录 |

关键语义：

- 两种都**只发一次**（step 用 `stall_delivered`，plan 用 `plan_stall_delivered`），不会每轮轮询重复刷
- 只对 `status == running` 检测；`paused` 的 plan 不会被判停滞（它在等审批，本来就静默）
- 阈值来自 `stall_seconds`，`<=0` 表示关闭检测（默认 300）
- `quiet_seconds` 是**实际**静默时长，通常明显大于 `threshold`，因为轮询本身有间隔

判别建议：

| 现象 | 大概率原因 | 动作 |
| --- | --- | --- |
| step 级停滞，且 `attempt=1` | 命令确实慢，或卡在交互式输入 | 看 stdout 最后几行；确认命令不会等 stdin |
| step 级停滞，且 `attempt>1` | 每次重试都慢，backoff + 执行时间累加 | 调大 `timeout` 或减小 `retry` |
| step 级停滞，`quiet_seconds` 极大（小时级） | 子进程僵死 / 被挂起 | `stop` 对应 task；plan 用 `cancel` 或手工改 `step.json` |
| plan 级停滞，但所有 step 都是 `pending` | 依赖永远不 ready（上游被跳过） | 用 `show` 查 `blocked_by` 链 |
| plan 级停滞，且没有 `check_schedule` 日志 | 驱动者没在轮询（Agent 侧断档） | 恢复轮询；这不是调度器问题 |

---

## 9. 常见现象 → 处置速查

| 现象 | 最可能的原因 | 处置 |
| --- | --- | --- |
| `run` 返回 `approval_required`，但没人批准 | 正常拦截 | `approve <id>` 或 `approve --all` |
| 批准了还是被拦，`approval_error=mismatch` | 命令文本改过（哪怕多一个空格） | 重新申请；审批绑定原文 |
| 批准了还是被拦，`approval_error=expired` | 超过 `approval_ttl_seconds` | 重新触发；或调大 TTL |
| Plan 一直 `paused` 且 `approval_id` 不变 | 审批仍是 `pending`（`pending` 不会重发） | `approve <approval_id>` 后再 `resume --approval <id>` |
| Plan `failed` 但所有 step `succeeded` | 曾被 `no_ready_steps` 判死锁（spec 被改过） | 删 plan 重新提交 |
| 下游全 `skipped(dependency)`，上游 `succeeded` | 上游有多个依赖，其中一个失败了 | 查 `blocked_by` 指向的那个 |
| `task.json` 状态一直 `running` 但进程没了 | 机器重启 / 监督进程被杀 | `check` 会把 `supervisor_lost` 判为 failed；再 `run --force` |
| `timeout` 但 `exit_code=0` | 极端竞态：超时判定先于退出码落盘 | 看 `timed_out=true` 即算超时；可重跑 |
| 快照目录缺失 | `keep_runs_per_plan` 裁剪过 | 不影响运行状态；调大 keep |
| `scheduler.log` 只有 `.1` 没有主文件 | 刚轮转过 | 正常 |
| `history` 里有两个同名 attempt | plan id 已含 `-a<N>` 又追加了一次（历史 bug） | 无影响；`history` 按目录名展示 |

---

## 10. 错误预防清单（写 spec 前自查）

1. **Stage id 全局唯一**，Step id 在 Stage 内唯一
2. `depends_on` 只指向**同 Stage** 的 Step，不写跨 Stage
3. 有环吗？用「A→B→C→A」自检一遍
4. 非幂等命令 `retry: 0`
5. `timeout` 是不是小于命令的正常耗时？超时 + 重试 = 反复杀
6. `level` 是否超过 `max_level`？超了就是 `policy_denied`，白费一次调度
7. 命令里是否含**变量**（`$env:FOO`、`%VAR%`）？审批绑定的是原文，变量值变化不改变绑定，但会让审计失去意义
8. 交互式命令是否加了非交互参数（`-y`、`--yes`、`CI=1`）？没有会挂到超时
9. 输出量是否过大？`stdout.log` 会无限增长，考虑命令内重定向到文件
10. `cwd` 是否存在？不存在会在 `create` 阶段报 `bad_cwd`，但 Step 的 `cwd` 不校验，会在运行时 `Popen` 抛异常
