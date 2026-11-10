# 常见错误用法 — agent-task-scheduler v2.9.1

本文收集「看起来能用、实际一定会出问题」的用法。每一条都给**症状**（你在哪里看到它）、**为什么错**、**正确做法**。按主题分组，可以直接按你遇到的现象检索。

---

## 1. 编排设计类

### 1.1 把跨 Stage 的依赖写成 `depends_on`

**症状**：`submit_plan` 返回 `spec_invalid`，`errors` 里是 `stage[s1].step[b].depends_on_unknown:a`。

**为什么错**：`depends_on` 只在**同一个 Stage 内**解析。`deps_ready` 与 `find_step` 都只在该 stage 的 steps 里查，跨 Stage 一律视为未知。

**正确做法**：用 Stage 顺序表达跨 Stage 的先后。

```json
// 错
{"stages": [
  {"id": "s1", "steps": [{"id": "a", "command": "make lib"}]},
  {"id": "s2", "steps": [{"id": "b", "command": "make app", "depends_on": "a"}]}
]}

// 对：s1 全部结束后才进 s2，先后关系由 Stage 边界保证
{"stages": [
  {"id": "s1", "mode": "serial", "steps": [{"id": "a", "command": "make lib"}]},
  {"id": "s2", "mode": "serial", "steps": [{"id": "b", "command": "make app"}]}
]}
```

### 1.2 用 `depends_on` 表达「顺序」而不是「条件」

**症状**：一串 step 都写 `depends_on: 前一个`，行为看起来正常。

**为什么错**：`depends_on` 的语义是「上游**成功**才 ready；上游失败则下游 `skipped(dependency)`」。如果你只是想要「一个完后跑下一个，不管成没成」，这会让一次无关紧要的失败连锁跳过整条链。

**正确做法**：纯顺序用 `mode: serial`（一个结束就下一个，失败不影响后续）；只有「上游成功下游才有意义」时才用 `depends_on`。

| 你想要 | 用什么 |
| --- | --- |
| 依次执行，各自独立 | `mode: serial`，不写 `depends_on` |
| 上游成功才跑下游 | `mode: parallel` + `depends_on` |
| 上游失败就全停 | `on_stage_failure: "stop"` |
| 只有关键 step 失败才全停 | Step 加 `critical: true` + Plan 设 `fail_on_critical: "stop"` |
| 这一轮不想区分关键与否、失败就全停 | `run_plan --fail-fast` |

### 1.3 在 `serial` Stage 里期待「失败就停」

**症状**：第一个 step 失败，第二个还是跑了。

**为什么错**：`serial` 只控制**并发度**（一波一个），不控制**失败传播**。`run_stage` 的循环里，前一个 step 失败后仍会取下一个 todo。

**正确做法**：要「失败即停」，写 `on_stage_failure: "stop"`（Plan 级）或给下游加 `depends_on`；只想「关键任务失败才停」就给该 step 加 `critical: true` 并在 Plan 设 `fail_on_critical: "stop"`（`serial` 同样生效：命中后不再启动后续 step）。

### 1.4 把整条流水线塞进一个 Stage 的并行模式

**症状**：`mode: parallel` 下所有 step 同时启动，其中几个立刻因为「文件还没生成」失败。

**为什么错**：`parallel` 的语义是「**每一波**把所有 ready 的 step 一起启动」。没有 `depends_on` 的 step 在第一波就会全部启动，它们之间没有任何等待关系。

**正确做法**：要么补全 `depends_on` 让第一波只剩真正的入口，要么拆成多个 serial Stage。

### 1.5 用一个巨型 Step 跑完所有事

**症状**：`step.json` 只有一个 step，`exit_code=1`，`stderr.log` 里 500 行，不知道哪一步挂的。

**为什么错**：Plan 的价值就是把失败**局部化**。一个 step 承担全部逻辑，退化成「一个带权限门的 shell」，还丢掉了重试、下游跳过、快照分步统计。

**正确做法**：按「失败后要不要重跑前面」切分。前面步骤幂等且便宜 → 可以合并；前面昂贵 → 必须拆开。

### 1.6 把 Plan 当万能脚本执行器

**症状**：为了跑一条命令，写了一个只有一个 Stage 一个 Step 的 plan。

**为什么错**：Task 层更轻、更快、`check` 的 `next_poll` 更细，且不需要 `schedules/` 那一层目录。Plan 的额外开销包括物化、Stage 状态文件、快照、保留策略。

**正确做法**：单条命令用 `ats.py create` + `run`。只有当出现「多步 + 依赖/重试/汇总」时才升级到 Plan。

### 1.7 依赖成环

**症状**：`errors` 里是 `stage[s1].depends_on_cycle`，或者运行到 `stage_failed` 且 `error_type=no_ready_steps`。

**为什么错**：`order_steps` 用 Kahn 拓扑排序，环上的节点入度永远不归零，排序结果长度小于节点数。校验阶段的环会被拦下；如果 spec 在校验后被外部改写，运行阶段就退化成死锁保护。

**正确做法**：提交前用「去掉所有无依赖节点」的思路自检。A→B→C→A 一定有问题。

### 1.8 给非幂等命令配 `retry`

**症状**：一个 `git push` 或 `kubectl apply` 被重试了 3 次，副作用叠加。

**为什么错**：`retry` 的语义是「进程重新起一遍」，不是「续跑」。任何「第二次执行会改变结果」的命令都不能重试。

**正确做法**：

| 命令性质 | `retry` |
| --- | --- |
| 只读（`curl GET`、`git fetch`、`pytest`） | 可配 |
| 幂等写（`npm ci`、`docker build`、`rsync`） | 可配少量 |
| 非幂等（`push`、`deploy`、`rm`、转账） | 必须 0 |

### 1.9 用 `retry` 掩盖确定性失败

**症状**：`attempts=3`，三次 `stderr.log` 内容一致，只是白等了 60 秒。

**为什么错**：确定性错误（编译报错、断言失败、参数错误）重试 100 次也一样。它还会污染 `task_retry` 事件，让人误以为「偶发抖动」。

**正确做法**：先跑一次看 `stderr`，确认是瞬时故障再配 `retry`。判断标准：**同样的输入，两次结果会不会不同**。

### 1.10 `timeout` 与 `retry` 互相拆台

**症状**：`timeout` 设 300 秒，命令正常要 280 秒；偶发稍慢就超时，然后重试，重试又超时，最终 `attempts=3` 全超时。

**为什么错**：每次重试都是**从头**跑，不继承进度。`timeout` 是单次上限，`retry` 是次数上限，两者相乘才是最坏耗时。

**正确做法**：`timeout` 取「正常耗时的 1.5-2 倍」，`retry_backoff` 留出资源回收时间。如果命令本身耗时不可预测，不要配 `retry`，改为把耗时长的部分拆出去。

### 1.11 认为 Plan 暂停会杀掉正在跑的 step

**症状**：有人在审批挂起时担心「正在跑的那个会不会被杀」。

**为什么错**：`paused` 发生在**调度循环**里：`ensure_step_permission` 返回 `paused` 后 `run_stage` 直接返回，不进入 `start_step`。已经 `running` 的 step 会继续跑完（但没人管它了，因为 `run_plan` 已返回）。

**正确做法**：Plan 暂停时，**不要**假定现场静止。要么等已 running 的 step 自行结束，要么 `cancel` + 手工清理。真正需要停的是对应 Task 的 `stop`（Plan 的 step 没有独立 stop 入口）。

### 1.12 期望 `run_plan` 重跑失败的 step

**症状**：Plan `failed`，再调 `run_plan` 一次，返回 `counts` 还是那套旧数字，什么都没变。

**为什么错**：`run_plan` 对已终结的 step 不再调度（`stage_todo` 过滤 `TERMINAL_STEP`）。`running` 的会被 `recover_interrupted` 复位，终态的不动。

**正确做法**：用 `rerun_task` 把目标 step 重置为 `pending`，它会顺带重新武装**直接下游**（仅 `skipped(dependency)` 的）。然后 `run_plan`。

### 1.13 期望 `rerun_task` 重置整条链

**症状**：`rerun_task` 之后，下游的深层依赖仍然 `skipped`。

**为什么错**：只重置**直接下游**（`args.step_id in deps`），不递归。`rearmed_steps` 里能看到实际范围。

**正确做法**：从最上游往下依次 `rerun_task`，或者直接重新提交一个 plan。

---

## 2. 权限与审批类

### 2.1 把 `actor_level` 当安全边界

**症状**：写了 `"actor_level": "L0"`，认为「Agent 就再也跑不了 L2 命令了」。

**为什么错**：`actor_level` 是**声明式**的。任何进程只要 `$env:ATS_ACTOR_LEVEL = "L3"` 或改 `policy.json` 就能自称高等级。`permission_gate` 读的就是这个值，它不做任何鉴权。

**正确做法**：把 `actor_level` 理解为「防误操作」而不是「防恶意」。真正的边界放在外层：CI token、操作系统账户、人工二次确认。

### 2.2 用审批绕过 `max_level`

**症状**：命令返回 `denied_policy`，用户反复 `approve` 却依然被拦。

**为什么错**：策略上限紧接在白名单之后判定，`level > max_level` 在**审批之前**就返回，审批压根不参与。身份直通只对 `approval_mode=auto` 的等级生效（默认仅 L0/L1）。

**正确做法**：改 `policy.json` 的 `max_level`，或把 step 的 `level` 降下来。`denied_policy` 的返回里带 `policy_max`，用它对齐。

### 2.3 命令文本拼接变量

**症状**：审批通过了，执行时 `approval_error=mismatch`。

**为什么错**：审批绑定 `command` 原文。你在提交时展开了一次变量（拿到了审批），执行时又展开一次（值变了），两个字符串不等。

**正确做法**：命令文本保持字面常量，变化的东西放 `env`。见 [command-guide.md](command-guide.md) §1.2。

### 2.4 反复 `resume` 而不批准，Plan 一直 `paused`

**症状**：`resume` 调了很多次，`check` 每次都是 `status=paused`，`approval_id` 始终是同一个。

**为什么错**：`approval_check` 对 `status=pending` 返回的 `error` 是 `pending`，不进入 `ensure_step_permission` 的重建分支（只处理 `missing`/`expired`）。所以同一 Step 反复 `resume` 不批准，既不会重发审批，也不会推进。

**正确做法**：`resume` 之前先 `approve` 那个 `approval_id`。`resume` 不加 `--approval` 时会返回 `status=paused` 并告诉你当前 `approval_id`——先读那个值。

**注意**：`approvals/` 里堆积多条 `pending` 通常来自**多个 Step 各自被拦**，或对不同 Plan 操作，而不是同一 Step 反复 `resume`。

### 2.5 `approve --all` 当日常操作

**症状**：批完之后，几条本不该跑的高危命令也跑起来了。

**为什么错**：`--all` 会把**所有** `pending` 批掉，包括其他会话、其他计划挂着的。

**正确做法**：只在确认「当前 root 下所有挂起审批都属于本次操作」时用。日常用单 id。批量前先看有哪些：

```powershell
Get-ChildItem .ats\approvals\*.json | ForEach-Object {
  $r = Get-Content $_ -Raw | ConvertFrom-Json
  if ($r.status -eq "pending") { "$($r.id)  $($r.command)  $($r.required_level)" }
}
```

### 2.6 白名单写成 `.*`

**症状**：所有命令都免审批，等于权限系统不存在。

**为什么错**：`allow_rules` 用 `re.search`，不区分大小写，且**优先级最高**（在 `max_level` 之前）。一条 `.*` 就让整套 L0-L3 失效，而且它还能让 `denied_policy` 失效。

**正确做法**：白名单只放「明确安全」的前缀，且锚定开头：

```json
{"allow_rules": ["^git status", "^git diff", "^npm run lint", "^pytest -q"]}
```

避免：`.*`、`build`（会匹配 `rm -rf build`）、`status`（会匹配 `git reset --hard status`）。

### 2.7 想用白名单放行 `stop` / `delete`

**症状**：配了 `^npm` 之类的规则，`delete` 还是要审批。

**为什么错**：操作型命令的白名单匹配对象是**合成 label**，不是原始命令。`delete t1` 的 label 就是 `delete t1`。

**正确做法**：针对 label 写规则：`^delete ` 或 `^stop <具体id>`。粒度越具体越好。

### 2.8 指望审批后 Schedule 自动恢复

**症状**：批准了审批，`check_schedule` 仍然返回 `status=paused`，不再触发。

**为什么错**：Plan 暂停会把 `schedule.json` 的 `status` 置为 `paused`，而 `resume` 只恢复 Plan、不会改 Schedule 状态；`check_schedule` 对 `paused` 不触发。

**正确做法**：先 `resume <schedule_id> --approval <id>` 把当前 Plan 跑完（它会走 `resolve_plan` 按 `current_plan` 找到 plan），再 `resume_schedule <schedule_id>` 把 Schedule 改回 `active`（L1 审批；保留 `next_run`，为空时按 `every` 重算）。若依赖长期自动循环，更稳的做法是让 Plan 内不出现需要审批的 step。

### 2.9 给 Step 设 `level` 高于 `max_level`

**症状**：`stage_failed` + `error_type=policy_denied`，`exit_code=null`。

**为什么错**：`policy_denied` 不进重试、不进审批，直接终态失败，整条下游全跳过。它是最「亏」的一种失败——一次调度机会白费。

**正确做法**：spec 里所有 `level` ≤ `max_level`。用 `ats.py policy --json` 先确认上限再写 spec。

---

## 3. 命令与 shell 类

### 3.1 把复杂命令塞进 `--command`

**症状**：`create` 成功了，`run` 报 `CommandNotFound` 或命令只跑了前半截。

**为什么错**：两层 shell 各消费一次引号。你看到的字符串和最终执行的不一样。

**正确做法**：复杂命令落到脚本文件（`.ps1` / `.bat` / `.cmd` / `.sh`），`--command` 只写 `powershell -File scripts\job.ps1` 这种一行调用。

### 3.2 用 `--no-shell`

**症状**：`--no-shell` 后报 `FileNotFoundError`，或者命令参数被当作文件名。

**为什么错**：`shell=False` 时 `Popen` 拿到的是一整个字符串，没有参数切分语义。它只适合「可执行文件的绝对路径且无参数」。

**正确做法**：不要用 `--no-shell`。需要精确参数控制时，写脚本文件。

### 3.3 在第三层 shell 里用 PowerShell 语法

**症状**：命令里写了 `Get-ChildItem`、`$env:FOO`，执行失败。

**为什么错**：第三层是 `cmd.exe`（Windows 的 `shell=True` 默认）。PowerShell 的 cmdlet、`$env:` 语法在那里不存在。

**正确做法**：要么命令写 `cmd` 语法（`dir`、`%FOO%`），要么显式调 `powershell -NoProfile -Command "..."`，要么写 `.ps1` 文件。

### 3.4 期望交互式命令能正常跑

**症状**：命令卡到超时，`stdout.log` 停在某个提示后面。

**为什么错**：stdin 是 `DEVNULL`。任何 `[y/N]`、密码提示、`Press any key` 都会立刻读到 EOF 或直接挂住。

**正确做法**：加非交互开关：`--yes`、`-y`、`--no-input`、`CI=1`、`DEBIAN_FRONTEND=noninteractive`。

### 3.5 只用重定向，不看调度器的日志文件

**症状**：`--command 'cmd > out.txt 2>&1'`，`show` 的 `stdout_tail` 为空。

**为什么错**：重定向把输出从调度器的 `stdout.log` 抢走了。你还失去了「stdout/stderr 分离」这个诊断便利。

**正确做法**：不要重定向。要分离按 stream 看就分别读两个文件，要看全部就拼接。

### 3.6 把 `show` 当实时监控

**症状**：每 2 秒 `show` 一次，日志里没有输出，以为卡住了。

**为什么错**：被调命令的 stdout 可能被**缓冲**（Python 默认全缓冲、`npm` 会缓冲）。文件里有，但只在 flush 之后。

**正确做法**：加 `PYTHONUNBUFFERED=1`、`NODE_OPTIONS=--no-buffer`、命令行 `-u`。并且按 `next_poll` 的节奏看，不要自己刷。

### 3.7 `cwd` 写相对路径

**症状**：Task 正常，Step 报 `FileNotFoundError` 或者跑错目录。

**为什么错**：Step 的 `cwd` 相对的是**调度器进程的工作目录**，而这个目录随调用方而变（你从 `D:\a` 调一次、从 `D:\b` 调一次，行为不同）。而且 Plan 的 `cwd` 不做存在性校验。

**正确做法**：`cwd` 一律写绝对路径（盘符路径或 `D:/proj/x`）。Task 与 Step 都是。

### 3.8 把密钥写进 `env`

**症状**：`task.json` 里明文躺着 token。

**为什么错**：`env` 持久化到磁盘，不加密。虽然事件不记 env，但文件本身就在那。

**正确做法**：命令自己去读凭据（环境变量由外部注入更好，但你注入的值仍会被落盘）。真正敏感的场景，让命令读文件或系统密钥库，`env` 里只放键名。

### 3.9 输出无限增长

**症状**：几个月后 `.ats` 目录几个 GB。

**为什么错**：`stdout.log` / `stderr.log` 是纯追加，没有任何轮转。只有 `scheduler.log` 会 2 MB 轮转。

**正确做法**：让命令自己控制输出量（`-q`、`--silent`、`--progress=plain`），或把细节写到业务自己的日志目录并在命令里控制轮转。定期清理 `tasks/` 里不要的历史任务。

### 3.10 把 `@enc` 写在命令中间

**症状**：`create --command "build.bat --from gbk @enc --to utf-8 logs"` 创建成功，但钩子从未执行。

**为什么错**：`@enc` 只有独立成 token 且在**命令末尾**时才会被拆出；放在中间会被当成主命令的一部分，原样传给 shell（多数命令会报未知参数）。

**正确做法**：写成 `<主命令> @enc <转换器参数>`，钩子只放最后。

---

## 4. 执行与轮询类

### 4.1 在同一个回合里死等

**症状**：Agent 卡住十几分钟；工具调用超时；对话被占住。

**为什么错**：`run` 已经异步返回了，`run --wait` 才是同步。用 `--wait` 等一个 30 分钟的训练任务，就是在赌工具超时时间。

**正确做法**：`run`（不带 `--wait`）→ 本回合结束 → 下一回合 `check`。长 Plan 用 `Start-Process` 后台跑 `run_plan`。

### 4.2 写死 `Start-Sleep 60`

**症状**：任务 10 秒就结束了，还多睡 50 秒；或者 5 分钟才好，60 秒醒来白查一次。

**为什么错**：调度器在返回里给了 `next_poll`，它已经算好了「下次值得看的时间」。

**正确做法**：`Start-Sleep -Seconds $r.next_poll`，用返回值驱动。

### 4.3 用 `check_schedule` 驱动长 Plan

**症状**：`check_schedule` 调用很久不返回，甚至看起来「卡死」。

**为什么错**：到期时它会**同步**调用 `run_plan`，整个 Plan 跑完才返回。

**正确做法**：长 Plan 拆到独立 Task，或外部后台派生 `run_plan`，`check_schedule` 只负责触发与停滞检测。

### 4.4 多个驱动者并发跑同一个 Plan

**症状**：step 状态错乱、`attempts` 莫名增加、快照出现同名目录、日志交错。

**为什么错**：没有文件锁。两个 `run_plan` 同时跑同一个 plan 会同时 `start_step` 同一个 step（各自 Popen 一个进程，两个 `pid` 先后覆盖 `step.json`，只有一个会被记录）。

**正确做法**：约定单一驱动者。Agent 侧保证同一时刻只有一个进程在跑某个 plan；周期性驱动交给 `check_schedule`，不要再用别的东西并跑。

### 4.5 以为 `check` 会重启任务

**症状**：`check` 之后任务还是 `failed`，没自动重跑。

**为什么错**：`check` 只**同步状态**（落 exit_code、判超时、判 `supervisor_lost`），从不启动任何东西。

**正确做法**：重跑用 `run --force`（Task）或 `rerun_task` + `run_plan`（Step）。

### 4.6 忽略 `stalled`

**症状**：`check_schedule` 返回 `stalled: [...]`，Agent 只读 `next_poll` 就继续睡。

**为什么错**：停滞是「需要人看一眼」的信号。`next_poll` 被强制成 30 只是因为「想让你快点再看」，不代表问题会自愈。

**正确做法**：`stalled` 非空就当本回合结束，上报给用户（或按 [error-taxonomy.md](error-taxonomy.md) §8 自查）。

### 4.7 用轮询精度做定时

**症状**：要求「每 5 分钟准点抓一次」，实际抖动几分钟。

**为什么错**：触发由 `check_schedule` 的调用时刻决定，精度 ≈ 轮询间隔 + 调用开销。

**正确做法**：需要精确定时用操作系统调度（`schtasks` / `cron`），本技能只做「大致周期 + 断点续跑」。

### 4.8 在 `paused` 的 Plan 上反复 `run_plan`

**症状**：每次 `run_plan` 都返回 `paused`，`approval_reissued_at` 一直在变。

**为什么错**：`run_plan` 对暂停态会走 `recover_interrupted` + 重新尝试权限门。没批准就再跑，只会再造一条审批。

**正确做法**：`approve` 之后用 `resume <id> --approval <id>`。

### 4.9 把 `pack` 当成「批量执行器」

**症状**：`pack "a,b,c"` 之后期待三条命令跑起来，`list` 里却全是 `pending`。

**为什么错**：`pack` 只登记文本，**不启动任何任务**（返回 `executed: false`）；它也不改变权限——每条照样要 `run`、照样过审批门。

**正确做法**：要执行就逐条 `run <task_id>`；要顺序或依赖请用 Plan（`submit_plan` + `run_plan`）。

---

## 5. 存储与运维类

### 5.1 删 `.ats` 当「重置」

**症状**：不小心把正在跑的 Task 记录删了，进程还在，但再也没人收尸。

**为什么错**：状态即文件，删目录 = 丢掉所有记录，但**不杀进程**。孤儿进程会继续跑、继续写日志（写到已删除的 inode / 失败）。

**正确做法**：重置前先 `list` 看有没有 `starting`/`running`，有就先 `stop`。真要清空，杀完再删。

### 5.2 直接手改 `events.jsonl` / `scheduler.log`

**症状**：`events` 游标错位，`history` 里出现解析失败的行。

**为什么错**：这两个是 **append-only JSONL**。手工编辑会破坏行完整性；`events_state.json` 的 `cursor` 是按**行号**计的，删一行就整体错位。

**正确做法**：需要清理就整体轮转/删除（接受游标归零）；不要局部编辑。

### 5.3 手工改 `plan.json` 的 `stages`

**症状**：改了 spec 里的 step，但运行行为还是老的。

**为什么错**：`submit_plan` 已经把 spec **物化**进 `plan.json` 并创建了 stage/step 目录。改原始 spec 文件对已提交的 plan 无效。

**正确做法**：改 `plan.json` 的 `stages`（并删掉不再存在的 step 目录），或者干脆重新 `submit_plan` 一个新 id。

### 5.4 把快照当备份

**症状**：以为 `runs/` 里有完整历史可以恢复。

**为什么错**：`runs/<plan_id>-a<N>/` 只有 4 个文件（plan / schedule / steps.jsonl / summary），**不含** stdout/stderr，也不含审批。而且默认只保留最近 5 份。

**正确做法**：需要归档就定期把整个 `.ats` 复制走。`runs/` 的价值是「快速看某次每步结果」，不是「恢复现场」。

### 5.5 `prune --keep 0` 以为删光

**症状**：`prune --keep 0` 返回 `count: 0`，什么都没删。

**为什么错**：`keep <= 0` 是「**不清理**」的开关（`preview_prune` 直接返回空）。它和「保留 0 份」是相反的意思。

**正确做法**：想删到只留 1 份就 `--keep 1`。想全删就手工删 `runs/` 目录（不影响运行状态）。

**注意区分**：`approval prune --older-than 0` 的 `0` 是**相反**语义——不限时间，会删掉所有已决议记录（`pending` 仍保留）。两个 prune 都叫 prune，但 `0` 的含义不同。

### 5.6 在网络盘上跑长任务

**症状**：`supervisor_lost`、`exit_code` 文件读不到、日志丢失。

**为什么错**：`run` 派生的是 `DETACHED_PROCESS`，新会话里网络盘映射通常不存在。

**正确做法**：`ATS_ROOT` 放本地盘（`C:` / `D:` 本地卷）。网络盘可以做只读备份。

### 5.7 多个项目共用一个 root

**症状**：`list` 里混着别人的任务，`prune` 删了不该删的快照。

**为什么错**：`ATS_ROOT` 是硬隔离边界。共用 root = 共用 tasks/schedules/approvals/日志。

**正确做法**：一个项目一个 root。用 `--root` 显式指定，或用环境变量在各自的会话里设。

### 5.8 期望 `.ats` 能进版本控制

**症状**：提交了 `.ats`，每次跑任务都产生巨大 diff。

**为什么错**：里面有 pid、时间戳、日志、快照，全是易变数据。

**正确做法**：`.gitignore` 掉整个 `.ats`。要版本化的是 **spec 文件**（`plan.json` / `schedule.json` / `task.json` 定义），不是状态。

---

## 6. 观测与诊断类

### 6.1 用 `exit_code` 判断任务成败

**症状**：`run --wait` 返回 0，就以为任务成功了。

**为什么错**：调度器成功 ≠ 命令成功。业务失败在 JSON 的 `status` 里。

**正确做法**：读 `status`（`succeeded` / `failed` / `timeout` / `killed`）。见 [error-taxonomy.md](error-taxonomy.md) §1。

### 6.2 用 `step.json` 的瞬时 `status` 判断重试

**症状**：看到某个 step 是 `failed`，但它其实还在重试。

**为什么错**：中间失败**确实**会写 `status=failed` 到 `step.json`，只是不发 `task_failed` 事件。

**正确做法**：看 `attempts` 与 `task_retry` 事件的 `max_attempts`。或者直接看最终 `task_failed` / `task_done`。

### 6.3 只统计 `task_failed` 就下结论

**症状**：报告说「本轮 3 个失败」，但实际只有 1 个 step 最终失败。

**为什么错**：如果没有区分事件流里的中间态，容易把 `task_retry`（含 `prev_error_type=failed`）误算。

**正确做法**：按 `(plan_id, stage_id, step_id)` 归组，取**最后一条**终态事件（`task_done` / `task_failed` / `task_skipped`）。

### 6.4 用 `events --lines` 当增量消费

**症状**：每次都读到同样的尾部事件，重复处理。

**为什么错**：带 `--lines` 时是 **tail 模式**，`mode=tail`，**不推进** `events_state.json` 游标。这是「想看最近发生了什么」，不是「消费」。

**正确做法**：增量消费用 `events`（不带 `--lines`），它会推进游标。两者别混。

### 6.5 期望全局 `events` 能看到 Plan 事件

**症状**：`ats.py events` 里看不到 `task_done`、`stage_done`。

**为什么错**：Plan 层事件投递到 `schedules/<sid>/events.jsonl`，**不**进全局流。`ats.py events` 只消费 `<root>/events.jsonl`，那里只有 Task 层事件。Plan 的审批事件因为带 `schedule_id`，也一并落在 schedule 的事件流里。

**正确做法**：Plan 事件直接读 `schedules/<sid>/events.jsonl`，或看 `check_schedule` 的返回、`show` 的汇总、`history` 的快照。

### 6.6 只看 `scheduler.log` 不看 `stderr.log`

**症状**：日志里只有 `task_end status=failed exit_code=1`，不知道为什么。

**为什么错**：`scheduler.log` 记的是**调度器动作**（start/end/retry/skip），不含命令输出。命令为什么失败只有 `stderr.log` 知道。

**正确做法**：`show <id> --lines 80` 一次拿 `stdout_tail` + `stderr_tail`。

### 6.7 认为日志有 `error` 级别就代表出错

**症状**：`logs --lines 20` 看到几行 `level=info` 的 `task_failed`，以为调度器报错了。

**为什么错**：`task_failed` 是**业务失败**的记录，日志级别仍是 `info`——对调度器来说这是正常事件。

**正确做法**：区分「日志级别」与「事件类型」。要看调度器自己出错，看命令的退出码与 JSON 的 `error`。

### 6.8 时间对不上

**症状**：`created_at` 与 `iso` 差 8 小时。

**为什么错**：`ts` 是 Unix 秒（UTC 基准），`iso` 是本地时间字符串。两个字段都可信，但基准不同。

**正确做法**：做时间运算用 `ts`，给人看用 `iso`。不要拿 `iso` 解析后再和 `ts` 比较。

---

## 7. 一页速查：最常犯的 10 条

| # | 反模式 | 一句话改法 |
| --- | --- | --- |
| 1 | 用 `$LASTEXITCODE` 判任务成败 | 读 JSON 的 `status` |
| 2 | 跨 Stage 写 `depends_on` | 拆 Stage |
| 3 | 命令里拼变量，审批 `mismatch` | 命令字面化，变量进 `env` |
| 4 | `allow_rules: [".*"]` | 锚定具体前缀 |
| 5 | 用审批绕过 `denied_policy` | 改 `max_level` 或降 `level` |
| 6 | 一回合里 `--wait` 等 30 分钟 | 异步 `run` + 下回合 `check` |
| 7 | 写死 `Start-Sleep` | 用返回的 `next_poll` |
| 8 | 多驱动者并发同一 plan | 单一驱动者 |
| 9 | 非幂等命令配 `retry` | `retry: 0` |
| 10 | 反复 `resume` 不批准 | 先 approve 该 step 的 `approval_id` 再 resume |