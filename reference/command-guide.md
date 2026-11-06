# 命令书写与调用规范 — background-task-scheduler v2.7

本文面向「由 Agent 调用调度器脚本」这一使用场景，规定**怎么把命令传进去、怎么把结果读出来、怎么避免被 shell 吃掉引号**。几乎所有「调度器行为诡异」的事故都源于本文某一条被违反。

---

## 1. 两条铁律

### 1.1 一条命令一次调用，一次调用只解析一个 JSON

两个脚本的 stdout **只有** JSON（除 `list` / `logs` 的默认人类可读模式）。因此：

```powershell
# 正确：拿住 stdout，按 JSON 解析
$out = python scripts/bts.py status build1 --json | ConvertFrom-Json
```

```powershell
# 错误：把 stdout 直接丢弃，然后猜状态
python scripts/bts.py run build1 | Out-Null
```

例外：`create` 的返回值只含 `task` 与 `created` 两个键，如果你不需要 id（id 已指定），可以丢弃。但**不要**在 `run` / `pause` / `approve` 上丢弃，因为它们的返回值里含继续执行所需的关键字段（`approval_id`、`resume`、`status`）。

### 1.2 命令原文就是主键

`--command` 的字符串会：

1. 原样写进 `task.json` / step spec
2. 原样写进事件 `task_created` / `task_start`
3. 原样成为**审批绑定的比较文本**

所以下面三种写法会产生**三个不同**的审批键：

```powershell
python scripts/bts.py create a --command "npm run build"
python scripts/bts.py create b --command "npm  run build"     # 两个空格 → 不同
python scripts/bts.py create c --command "npm run build "      # 尾随空格 → 不同
```

**推论**：审批通过后，执行时必须用**完全一致**的字符串。不要在执行阶段动态拼接变量（`$env:VERSION`、时间戳、路径）。变量会变，字符串会变，审批会 `mismatch`。

推荐做法：把需要变化的东西放进 `env`，让命令文本保持字面稳定。

```powershell
# 好：命令文本不变，变化走 env
python scripts/bts.py create deploy1 --command "bash deploy.sh" --env STAGE=prod --env TAG=20261101
```

---

## 2. `--root`：状态目录定位

优先级：`--root <dir>` > 环境变量 `BTS_ROOT` > `<当前工作目录>/.bts`。

`--root` 是**两个脚本的全局参数**，必须写在子命令**之前**：

```bash
python scripts/bts.py --root D:\run\state run build1          # 正确
python scripts/bts.py run build1 --root D:\run\state          # 报错：run 没有 --root 参数
```

同理 `bts_plan.py`：

```bash
python scripts/bts_plan.py --root D:\run\state run_plan ci1-a1
```

三个实践建议：

1. **同一个 Plan 的所有命令必须用同一个 root**。`submit_plan` 写在 A 目录、`run_plan` 读 B 目录 → `not_found`。
2. 长期项目把 root 固化进环境变量，避免每条命令都写：

```powershell
$env:BTS_ROOT = "D:\work\proj\.bts"
python scripts/bts_plan.py list --json
```

3. 并行多个项目时优先用 `--root`（显式），因为环境变量在子进程里可能被继承或覆盖，容易串味。

---

## 3. 引号、转义与 shell 语义

这一节是事故高发区。先理解**两层 shell**：

```
[你的终端 / PowerShell]  →  [python]  →  [subprocess.Popen(shell=True)]  →  [系统 shell]  →  [真正的命令]
        第一层                第二层              第三层
```

你在终端写的引号被**第一层**消费；进到 Python 后是一段普通字符串；再被**第三层** shell 解释一次。两层都解释引号，所以要按「两层」来算。

### 3.1 Task 与 Step 的 shell 差异

| 实体 | shell 行为 | 可控 |
| --- | --- | --- |
| Task（`bts.py`） | 默认 `shell=True`（走 `cmd.exe`），可用 `--no-shell` 关掉 | 有开关 |
| Step（`bts_plan.py`） | **恒为 `shell=True`** | 无开关（spec 里写 `shell: false` 无效） |

Task 用 `--no-shell` 时，`command` 会被当成**单个可执行文件路径**（`Popen(command, shell=False)` 传的是字符串，Windows 下等价于把整串当程序名）。所以 `--no-shell` 只适合「纯可执行文件 + 不需要参数」的极少数场景，实际上很少用。

### 3.2 PowerShell 下的三种写法

**写法 A：单引号包整条命令（推荐，最少转义）**

```powershell
python scripts/bts.py create b1 --command 'npm run build -- --mode production'
```

PowerShell 单引号内**不**做变量展开，所以 `$`、`%` 原样保留。缺点是字符串里不能出现单引号。

**写法 B：双引号（需要变量展开时）**

```powershell
python scripts/bts.py create b1 --command "npm run build -- --define VERSION=$env:VERSION"
```

注意这是**在提交时**展开变量，不是执行时。这意味着审批绑定的是展开后的文本——如果之后 `$env:VERSION` 变了，重跑时文本不同，审批 `mismatch`。

**写法 C：用 spec 文件（命令复杂时的唯一可靠方式）**

```powershell
python scripts/bts.py create b1 --spec task.json
```

`task.json`：

```json
{"id": "b1", "command": "npm run build -- --mode production", "level": "L1", "timeout": 900}
```

spec 是 JSON，字符串里的引号用 `\"` 转义即可，没有 shell 二次解释。**命令里含有：双引号、`$`、`%`、`&`、`|`、`>`、换行、反斜杠路径时，一律用 spec。**

### 3.3 命令里需要传给下层程序的引号

假设你要执行：

```bash
node -e "console.log('hi')"
```

如果整条塞进 `--command`，PowerShell 会先把双引号消费掉，Python 拿到 `node -e console.log('hi')`，第三层 shell 又把它拆开。

三种可行写法：

```powershell
# 1) 外层单引号，内层双引号
python scripts/bts.py create e1 --command 'node -e "console.log(''hi'')"'

# 2) 用 spec 文件（最省心）
python scripts/bts.py create e1 --spec e1.json

# 3) 把复杂逻辑落到脚本文件，命令只调脚本
python scripts/bts.py create e1 --command 'node scripts\hello.js'
```

第 3 种是最好的工程实践：**命令保持一行、无内嵌引号、可复现**。

### 3.4 多行命令

`--command` 可以含换行（JSON 里写 `\n`），但极不推荐：

- 事件与日志里会出现多行字段，破坏 grep
- 审批比较文本含换行，人工核对困难
- `cmd.exe` 对多行的处理在不同版本上不一致

正确做法：写成 `.ps1` / `.bat` / `.sh` 文件，命令只调这个文件。

### 3.5 反斜杠路径

Windows 路径在 JSON 里必须转义：

```json
{"command": "<tool-path>\\build.exe --fast"}
```

在 PowerShell 命令行里则**不需要**双写：

```powershell
python scripts/bts.py create b1 --command 'C:\tools\bin\build.exe --fast'
```

### 3.6 管道与重定向

`|`、`>`、`<` 会被**第三层** shell 解释，这在 Task 里是你想要的：

```powershell
python scripts/bts.py create l1 --command 'pytest -q > reports\out.txt 2>&1'
```

但注意：

- 重定向发生在**子进程的 shell 里**，不是 PowerShell 里。`$env:TEMP` 这类 PowerShell 变量在第三层不可用（那里是 `cmd.exe`）。
- 建议不要用重定向——调度器已经把 stdout/stderr 分别落到 `stdout.log` / `stderr.log`。额外的重定向只会让你在 `show` 里看到空输出。
- 如果确实要重定向（例如输出太大），写到**绝对路径**。

### 3.7 `@enc` 钩子与引号

`@enc` 是命令文本的一部分，`create` / `pack` 落盘时会被拆走，所以引号规则与普通命令完全一样：钩子里的路径含空格时仍按 §3.1-§3.3 处理；含双引号、`$`、`%`、`&` 时改用 spec。

```powershell
# 外层单引号，主命令与钩子整条传入
python scripts/bts.py create enc1 --level L1 --command 'build.bat @enc --from gbk --to utf-8 logs'

# 参数多、路径含空格 → spec
python scripts/bts.py create --spec enc.json
```

- `@enc` 必须**独立成 token**（前后为空白、行首或行尾）：`--from gbk @enc --to utf-8` 这种放中间的写法不会被拆分，而是原样进主命令
- 转换器的 `path` 是**位置参数**，不要写 `--path`
- 审批绑定的是**主命令**（钩子已剥离）；钩子随任务等级执行，不额外触发审批
- 钩子参数串由 supervisor 以 `shell=True` 交给转换器，不要再套一层引号

---

## 4. 工作目录（`cwd`）与回退链

| 实体 | 回退顺序 |
| --- | --- |
| Task | `--cwd` > spec 的 `cwd` > 调度器进程的当前目录 |
| Step | step 的 `cwd` > plan 的 `cwd` > 调度器进程的当前目录 |

关键差异：**Plan 没有校验 `cwd` 的存在性**。Task 在 `create` 时会校验并报 `bad_cwd`；Step 会在 `start_step` 调 `Popen(cwd=...)` 时抛异常。所以：

- 提交 Plan 前，手工确认 `plan["cwd"]` 与每个 step 的 `cwd` 存在
- 建议每个 step 显式写 `cwd`（绝对路径），不依赖当前目录，因为调度器可能被不同工作目录的调用驱动

```json
{"id": "build", "command": "npm run build", "cwd": "D:/proj/web", "level": "L1"}
```

（JSON 里正斜杠也是合法路径分隔符，且不需要转义，推荐。）

---

## 5. 环境变量

三层合并，后者覆盖前者：

```
调度器进程的 os.environ  →  task/step 的 env  →  命令自身在 shell 里的赋值
```

写法差异：

```powershell
# Task：命令行用 --env，可重复
python scripts/bts.py create b1 --command "npm run build" --env CI=1 --env NODE_ENV=production
```

```json
// Step：spec 里是对象
{"id": "build", "command": "npm run build", "env": {"CI": "1", "NODE_ENV": "production"}}
```

要点：

- 值必须是字符串。JSON 里写 `{"CI": 1}` 会被 `str()` 成 `"1"`，能用但不推荐。
- `--env` 按**第一个** `=` 切分，所以 `--env URL=http://a=b` 得到 `URL=http://a=b`。
- 变量会**持久化**在 `task.json` / spec 里，不随环境漂移。这也意味着你无法在「重跑时」临时改 env —— 想改就 `create --force` 重建。
- 不要把密钥写进 env：`task.json` 是明文，`task_created` 事件也会记录 command（但**不**记录 env）。敏感信息请让命令自己去读凭据文件 / 系统密钥库。

---

## 6. 输出、编码与交互

### 6.1 输出落地位置

| 实体 | stdout | stderr |
| --- | --- | --- |
| Task | `tasks/<id>/stdout.log` | `tasks/<id>/stderr.log` |
| Step | `<plan>/stages/<stage>/steps/<step>/stdout.log` | 同目录 `stderr.log` |

读取方式：

```powershell
python scripts/bts.py show build1 --lines 40        # 两个 tail 都在 JSON 里
Get-Content .bts\tasks\build1\stderr.log -Tail 40    # 直接读
```

### 6.2 编码

两个脚本以 `utf-8` 写文件，读日志时用 `errors="replace"` 容错。在 Windows 上：

- Python 3.13 的默认 stdout 编码通常是 UTF-8（取决于终端）；若出现中文乱码，设 `$env:PYTHONIOENCODING = "utf-8"`
- 被调用的命令如果是 `cmd.exe` 下的老工具，可能输出 GBK 字节 → 落进 `stdout.log` 后按 UTF-8 读会变乱码。此时看原文：`Get-Content -Encoding Default`。这是**被调命令**的编码问题，不是调度器的问题。

### 6.3 没有 TTY

所有子进程的 stdin 都是 `DEVNULL`，且没有伪终端。因此：

- 交互式确认会立即失败或挂住（例如 `npm install` 问「是否继续」、`apt` 问「是否覆盖」）
- 进度条类输出（`tqdm`）会把大量 `\r` 写入日志，`show` 看起来是一行
- 颜色代码（ANSI）会原样落盘

对策：

```powershell
# 强制非交互
python scripts/bts.py create i1 --command "npm ci --yes --no-audit --no-fund" --env CI=true
python scripts/bts.py create i2 --command "pip install -r requirements.txt --no-input"
python scripts/bts.py create i3 --command "apt-get install -y pkg"
```

```json
{"id": "t1", "command": "pytest -q --no-header", "env": {"CI": "1", "PYTHONUNBUFFERED": "1"}}
```

`PYTHONUNBUFFERED=1` 对「长时间没有输出」的场景特别重要——否则 stdout 被缓冲，`show` 会一直是空的，还会误触发停滞告警。

---

## 7. 权限与审批的书写流程

标准三段式，不要跳步：

```powershell
# 第 1 段：尝试执行，被拦则拿到 approval_id
$run = python scripts/bts.py run deploy1 | ConvertFrom-Json
if ($run.error -eq "approval_required") {
    $apId = $run.approval_id
}

# 第 2 段：批准（人类动作，或高等级身份进程）
python scripts/bts.py approve $apId

# 第 3 段：带审批执行，命令文本必须与第 1 段完全一致
python scripts/bts.py run deploy1 --approval $apId --wait
```

要点：

- **不要自己编造 approval_id**。格式是 `ap-<8位hex>`，但不存在于 `approvals/` 的 id 会返回 `approval_error=missing`。
- **不要复用别人的 approval_id**。审批绑定 `command`，命令不同就 `mismatch`。
- **`approve --all` 只在明确「所有挂起审批都可以批」时用**。它会把所有 `pending` 一次性批掉，无法回滚。
- 拒绝时务必留 `--note`：

```powershell
python scripts/bts.py approve $apId --reject --note "生产窗口期，延后到 22:00"
```

- **批准者身份必须 ≥ 审批要求等级**。用 L1 批 L2 会返回 `ok=false` + `insufficient_level`，且退出码为 `4`。此时不要重试同一条命令，先把 `BTS_ACTOR_LEVEL` 提到 L2/L3 再批。
- `approve` 的退出码：单条失败时 `insufficient_level` → `4`，`missing` / `expired` / `already_*` → `3`；`approve --all` 恒为 `0`（批量语义，逐项看 `approvals[].ok`）。判断成败优先看 `ok`。

- 操作型命令（`stop` / `delete` / `pause` / `cancel` / `rerun_task`）被拦时，返回的是 `approval_required` + `approval_id`，其「命令文本」是固定模板（如 `delete build1`）。批准后带 `--approval` 重发同一命令。

Plan 层的审批书写：

```powershell
# run_plan 返回 status=paused + approval
$r = python scripts/bts_plan.py run_plan rel1-a1 | ConvertFrom-Json
$apId = $r.approval.id

python scripts/bts.py approve $apId
python scripts/bts_plan.py resume rel1 --approval $apId
```

`resume` 接受 **schedule_id 或 plan_id**（会先按 schedule 解析 `current_plan`，再按 plan 目录名找），比记 `-a<N>` 后缀更省事。

---

## 8. 轮询节奏

### 8.1 永远按 `next_poll` 睡，不要写死

| 命令 | `next_poll` 语义 |
| --- | --- |
| `check <task>` | 运行中 → `5`；终态 → `0` |
| `check`（无参数，批量） | 任一运行中 → `5`；否则 `0` |
| `check_schedule <sid>` | `min(30, 距 next_run 秒数)`；已到期 → `0`；有停滞 → 强制 `30` |
| 其他命令 | 无此字段 |

```powershell
do {
  $r = python scripts/bts.py check build1 | ConvertFrom-Json
  if ($r.next_poll -gt 0) { Start-Sleep -Seconds $r.next_poll }
} while ($r.next_poll -gt 0)
```

### 8.2 `check_schedule` 是「驱动」不是「查询」

它可能**同步执行**一次 Plan（`run_plan` 在一次调用内跑完）。如果 Plan 里有个 20 分钟的训练步骤，那次 `check_schedule` 就会阻塞 20 分钟。因此：

- 长 Plan 不要靠 `check_schedule` 驱动，改用 `Start-Process` 后台跑 `run_plan`
- 或者把长步骤拆成独立 Task，让 Plan 只做编排

```powershell
Start-Process -FilePath python -ArgumentList "scripts/bts_plan.py run_plan ci1-a1" -WindowStyle Hidden
```

### 8.3 停滞告警的处理节奏

`check_schedule` 返回 `stalled` 非空时，`next_poll` 被强制为 `30`。这不是让你 30 秒后再看一次就完事——它意味着需要**人工决策**：

1. 先看 `stalled[*].scope`
2. `scope=step` → 读该 step 的 `stdout.log` 尾部
3. `scope=plan` → 检查驱动者是否还在轮询、依赖链是否断

---

## 9. ID 命名规范

### 9.1 各层 id 的来源

| 实体 | 来源 | 示例 |
| --- | --- | --- |
| Task | `create` 的位置参数，或 `--spec` 的 `id`，或自动生成 `t-<6hex>` | `build1`、`t-3f2a91` |
| Plan（oneshot） | `<schedule_id>-a1` | `ci1-a1` |
| Plan（recurring） | `<schedule_id>-a<N>`，N 从 1 递增 | `poll1-a3` |
| Schedule | `submit_*` 的 `--id`，或 spec 的 `id`，或自动生成 `s-<6hex>` | `poll1` |
| Approval | `ap-<8hex>` | `ap-3f2a91c4` |

**关键推论**：`oneshot` 的 schedule id 与 plan id 基名相同。所以 `bts_plan.py show ci1` 与 `show ci1-a1` 都能解析到同一个 plan（前者走 schedule 的 `current_plan`，后者走目录名匹配）。

### 9.2 命名建议

- 用小写 + 短横线 + 语义：`ci-lint-test`、`nightly-patrol`、`train-v3`
- **不要**用纯数字（`create 1` 在某些 shell 里会被当成重定向对象）
- **不要**用空格、中文、`/`、`:`、`*`（虽然目录名允许大部分字符，但会让命令与日志难处理）
- 不要手动让 task id 长得像 `<sid>-a<N>`，否则 `history` 的目录名分组会有歧义（按 `summary.json` 的 `plan_id` 分组，通常没事，但不值得冒险）

### 9.3 `rerun_task` 的三段定位

```bash
python scripts/bts_plan.py rerun_task ci1 test e2e
#                                   ^^^ ^^^^ ^^^
#                              schedule  stage  step
```

第一个参数是 **schedule_id**（不是 plan_id）。内部会用 `schedule.json` 的 `current_plan` 找当前 plan。所以重跑的是**当前这一轮**，历史轮次不受影响。

---

## 10. `--force` 与幂等语义

| 命令 | 不带 `--force` | 带 `--force` |
| --- | --- | --- |
| `create` | 同名 task 存在 → `task_exists` | 覆盖 `task.json`（旧 stdout/stderr 保留，`exit_code` 会被下次 run 清） |
| `run` | 终态任务 → `rerun_requires_force` | 允许重跑 |
| `run` 运行中 | `state_conflict` | **同样** `state_conflict`（force 不能并发） |
| `stop` | 无差别（`--force` 对 stop 无实际作用，兼容保留） | — |

`create --force` 的实际语义是「就地覆盖定义」，**不**清理 `stdout.log` / `stderr.log` / `exit_code`。若你希望干净重跑，先 `delete` 再 `create`（delete 需要 L2 权限）。

---

## 11. 场景速查卡

### 11.1 「跑一个可能很慢的命令，之后我还要看结果」

```powershell
python scripts/bts.py create job1 --command 'bash run.sh' --level L1 --timeout 3600
python scripts/bts.py run job1            # 立刻返回 supervisor_pid
python scripts/bts.py check job1          # 按 next_poll 轮询
python scripts/bts.py show job1 --lines 60
```

### 11.2 「两条命令依次执行（前一条挂了后一条照跑）」

用 Plan，serial Stage，不用 `depends_on`（Stage 顺序已经表达了先后）：

```json
{"id": "seq1", "stages": [
  {"id": "s1", "mode": "serial", "steps": [
    {"id": "fetch", "command": "git fetch --all"},
    {"id": "build", "command": "npm run build", "timeout": 600}
  ]}
]}
```

serial Stage 的语义：一个 step 结束（无论成败）才启动下一个。**它不会**因为前一个失败而跳过后面——要那种语义请用 `depends_on` 或 `on_stage_failure=stop`。

### 11.3 「B 依赖 A，A 失败 B 应跳过」

```json
{"id": "dep1", "stages": [
  {"id": "s1", "mode": "parallel", "steps": [
    {"id": "a", "command": "make lib"},
    {"id": "b", "command": "make app", "depends_on": "a"}
  ]}
]}
```

### 11.4 「同一条命令偶发失败，自动重试两次」

```json
{"id": "net1", "stages": [
  {"id": "s1", "mode": "parallel", "steps": [
    {"id": "fetch", "command": "git clone --depth 1 https://example/repo.git", "retry": 2, "retry_backoff": 15}
  ]}
]}
```

### 11.5 「每小时巡检一次，跑 12 次停」

```json
{"id": "patrol1", "every": 3600, "max_runs": 12, "plan": "./patrol-plan.json"}
```

```powershell
python scripts/bts_plan.py submit_schedule --spec patrol.json
python scripts/bts_plan.py check_schedule patrol1     # 按 next_poll 循环调用
```

### 11.6 「审批挂起后怎么继续」

```powershell
python scripts/bts.py approve --all                   # 批量批准
python scripts/bts_plan.py resume <schedule_id>       # 恢复 Plan（自动找 current_plan）
python scripts/bts_plan.py resume_schedule <schedule_id>   # Schedule 改回 active（保留 next_run）
```

**注意**：`resume` 只恢复 Plan；`schedule.json` 的 `status` 仍是 `paused`，`check_schedule` 不会继续触发。
要继续周期任务，再执行 `resume_schedule <sid>`（L1 审批，被拦时带 `--approval` 重发；`next_run` 为空时按 `every` 重算）。见 [anti-patterns.md](anti-patterns.md) §2.8。

### 11.7 「只想重跑失败的那一步」

```powershell
python scripts/bts_plan.py show ci1-a1                # 找失败 step
python scripts/bts_plan.py rerun_task ci1 test e2e
python scripts/bts_plan.py run_plan ci1-a1
python scripts/bts_plan.py rerun_plan ci1-a1          # 或：整个 Plan 全部重置后重跑
```

### 11.7.1 「关键这一步失败就整停」

```powershell
# 方式一：写进 spec（可长期生效）——给关键 step 加 critical，Plan 加 fail_on_critical
# {"fail_on_critical": "stop", "stages": [... {"id": "lint", "critical": true} ...]}
python scripts/bts_plan.py run_plan ci1-a1

# 方式二：不改 spec，本次运行临时全量即停（所有 step 都当作 critical）
python scripts/bts_plan.py run_plan ci1-a1 --fail-fast
python scripts/bts_plan.py resume ci1-a1 --fail-fast      # 恢复时同样可带
```

- 判定只看**退出码**（重试耗尽后仍 `failed` / `timeout`），不扫描日志文本
- 并行批次里命中后，同批兄弟进程树会被 `taskkill /T /F` 终止，`step.json` 记 `aborted_by="fail_fast"`，事件 `task_aborted`
- 当前 Stage 剩余 step 标 `skipped(critical_failed)`，后续 Stage 全 `skipped`，`plan.status=failed`

### 11.8 「事后复盘某次运行」

```powershell
python scripts/bts_plan.py history ci1
python scripts/bts_plan.py history ci1 --plan ci1-a2 --logs --lines 40
python scripts/bts.py logs --lines 100 --json | ConvertFrom-Json
```

### 11.9 「批量下载一批文件，断了能续传」

```powershell
# 清单可以是 .json / .txt（URL<TAB>文件名）/ .csv（url,文件名）
python scripts/bts.py download --file urls.txt --out D:\dl --concurrency 4 --rate 524288
python scripts/bts.py check dl-1a2b3c          # 按 next_poll 轮询
Get-Content "$root\tasks\dl-1a2b3c\stdout.log" -Encoding UTF8 | Select-Object -Last 1
```

- **不要再用 `Invoke-WebRequest` 包一层**：Worker 已封装 Range 续传、重试、校验，外包一层反而会丢掉 `.part`
- 断了就直接再跑一次同样的命令：已完成的文件按 `sha256`/`size` 校验后跳过（`status: existing`），未完成的从 `.part` 续传
- 想看每项结果就解析 `stdout.log` 的最后一行 `summary`（`total` / `succeeded` / `failed` / `items`）
- **下载失败不会让 `--wait` 返回非 0**：`--wait` 仍是 Task 语义（0/2/4），成败看 `task.status` 或 `summary`

### 11.10 「下载一个种子（磁力或 .torrent）」

```powershell
python scripts/bts.py torrent --magnet "magnet:?xt=urn:btih:..." --out D:\dl
python scripts/bts.py torrent --torrent D:\dl\sintel.torrent --out D:\dl --seed-time 30
python scripts/bts.py status tor-1a2b3c --json
Get-Content "$root\tasks\tor-1a2b3c\stdout.log" -Encoding UTF8 | Select-Object -Last 2
```

- 依赖 aria2c：`winget install --id aria2.aria2 -e`；装好后新开一个 shell 才认 PATH，BTS 也会去 WinGet 目录里找，所以当前 shell 直接可用
- `--seed-time` 单位是**分钟**，`0`（默认）表示下完即停；做种期间进度行带 `seeding: true`
- 磁力链先下元数据：进度行 `percent` 一直是 0、`name` 形如 `[METADATA]xxx`，元数据到手后才换成真实 gid（日志里会多一条 `torrent_metadata`）
- `stop` 是硬杀进程树：靠 `*.aria2` 控制文件续传（`--auto-save-interval=10`），`run --force` 从上次保存点接着下
- 下载失败不会让 `--wait` 返回非 0：成败看 `task.status` 或最后一行 `summary`

### 11.11 「审批记录堆积了怎么清」

```powershell
python scripts/bts.py approval prune --dry-run --older-than 2592000   # 先看会删哪些
python scripts/bts.py approval prune --older-than 2592000           # 删 30 天前的已决议记录
```

- 只删 `approved` / `rejected` / `expired`；**`pending` 永不删除**
- `--older-than` 按 `resolved_at`（缺失时 `created_at`）判断；`0` = 不限时间 = 全删已决议
- 与 `bts_plan.py prune --keep 0` 的 `0` 语义相反（后者是「不清理」）

### 11.12 「一次登记多条命令（指令包）」

```powershell
python scripts/bts.py pack "npm run build,npm test,git status" --pack-id pk-build
python scripts/bts.py list --pack pk-build
python scripts/bts.py run t-1a2b3c        # 要跑哪条再单独 run
```

- **只登记文本**：N 条命令各建一个 `pending` Task（`kind="pack"`），不执行、不写日志、不解析内容
- 分隔符是**英文逗号**；命令内部的逗号写 `\,`（PowerShell 双引号里反斜杠原样保留）
- 空项（如 `a,,b`）会让**整包**被拒（`pack_empty_item`，退出码 2），一条也不落盘
- 每条 Task 与普通 Task 同权：`run` / `check` / `show` / `stop` / `delete` 都可用，审批门不受 `pack` 影响
- 包内**没有依赖与顺序**：`index` 只表示它在原文本中的位置

---

## 12. Windows 专项

### 12.1 异步派生的实现

`bts.py run` 不是用 `Start-Process`，而是在 Python 内部：

```python
subprocess.Popen([sys.executable, __file__, "supervise", task_id],
                 creationflags=0x00000008 | 0x00000200,   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                 stdin=DEVNULL, stdout=supervisor.log, stderr=supervisor.log)
```

后果：

- 子进程**脱离**当前控制台。父终端关闭不影响任务。
- 但它仍在**同一个会话**里，所以机器注销会杀掉它（不是开机自启服务）。
- `supervisor_pid` 是 `supervise` 进程的 pid，`pid` 是真正命令的 pid。`stop` 会两个都杀（`taskkill /T /F` 杀进程树）。

### 12.2 `check` 在 Windows 上如何判活

用 `tasklist /FI "PID eq <n>" /NH` 的输出里是否含该 pid。注意：

- pid 复用是可能的。极端情况下旧 pid 被新进程占用，会误判「还活着」。概率低，但这也是 `supervisor_lost` 有时不触发的根因。
- `tasklist` 对无权限查询的其他用户进程会返回空 → 误判为丢失。调度器和任务通常同用户，所以影响小。

### 12.3 杀不干净的情况

`taskkill /T /F` 杀**进程树**，但：

- 若命令自行 `start` 了脱离的进程（`Start-Process`、`&` 后台），它不在树里 → 杀不到
- 服务型进程（Windows Service）杀不掉 → 需要 `sc stop`

处置：`stop` 后手工验证

```powershell
Get-Process | Where-Object { $_.ProcessName -like "*node*" }
```

### 12.4 路径与盘符

- `cwd` 支持盘符路径与 `~`（`Path.expanduser()`）
- 网络盘（`\\server\share`）可能因会话不持久而失败（DETACHED_PROCESS 会话没有映射的盘符）。**不要**在网络盘上跑长任务。
- 长路径（>260 字符）在旧 API 下会失败。保持 root 路径短。

### 12.5 控制台编码与中文

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

否则中文项目的 `show` 输出可能抛 `UnicodeEncodeError`（发生在**打印**阶段，非数据损坏）。

---

## 13. Agent 集成模板

### 13.1 PowerShell：提交并等待，带错误分类

```powershell
function Invoke-Bts {
  param([string[]]$Args)
  $raw = python scripts/bts.py @Args
  $code = $LASTEXITCODE
  $obj = $null
  try { $obj = $raw | ConvertFrom-Json } catch { }
  [pscustomobject]@{ code = $code; data = $obj; raw = $raw }
}

$r = Invoke-Bts @("run", "build1", "--wait")
switch ($r.code) {
  0 { $r.data.task.status }              # succeeded / failed / timeout
  2 { "参数错误: $($r.data.error)" }
  3 { "状态冲突: $($r.data.error)" }
  4 {
        if ($r.data.error -eq "approval_required") {
            "需要审批: $($r.data.approval_id)"
        } else { "策略拒绝: $($r.data.error)" }
     }
}
```

### 13.2 Python：解析 Plan 结果并决定下一步

```python
import json, subprocess, sys

def bts(*args):
    p = subprocess.run([sys.executable, "scripts/bts_plan.py", *args],
                       capture_output=True, text=True, encoding="utf-8")
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        data = {"raw": p.stdout}
    return p.returncode, data

code, out = bts("run_plan", "ci1-a1")
if out.get("status") == "paused":
    ap = out["approval"]["id"]
    print("需要审批", ap)
elif out.get("status") == "failed":
    for s in out.get("stages", []):
        if s.get("result") == "failed":
            print("失败 stage", s["stage_id"], s.get("detail"))
```

要点：

- 永远指定 `encoding="utf-8"`，否则 Windows 下中文会乱
- 永远先判 `returncode`，再读字段；`error` 存在时不要假设 `status` 存在
- 不要 `shell=True` 包一层——多一层 shell 多一层引号事故

### 13.3 一个轮次只推进一小步

Agent 的典型循环：

```
1. 取当前状态（check / check_schedule / show）
2. 若需人决策（approval_required / stalled / failed）→ 上报，结束本轮
3. 否则推进（run / run_plan / resume）
4. 若是长任务 → 立即返回，下一轮再 check
```

**反模式**：在一个轮次里写 `while` 死循环等任务结束。这会占住对话，且长任务可能超过工具超时。

---

## 14. 常见调用事故对照

| 事故 | 真实原因 | 修法 |
| --- | --- | --- |
| 命令报 `unexpected argument '--root'` | `--root` 写在子命令后 | 移到子命令前 |
| `not_found` 但明明刚提交 | 两次调用用了不同 `BTS_ROOT` / 不同 cwd | `--root` 显式统一 |
| 审批永远 `mismatch` | 命令文本在两次调用间被 shell 展开成不同值 | 用 spec + 环境变量，固定命令文本 |
| 命令里 `%VAR%` 不生效 | 第三层是 `cmd.exe`，但 PowerShell 已把 `%VAR%` 当字面量（这是对的）；若写成 `$env:VAR` 则被 PowerShell 先展开 | 明确你想在哪一层展开：提交时（PowerShell）还是执行时（cmd） |
| 中文乱码 | 子命令输出 GBK，日志按 UTF-8 读 | 让子命令输出 UTF-8，或看原文时用 `-Encoding Default` |
| `show` 的 stdout 一直空 | 子命令 stdout 被缓冲 | 加 `PYTHONUNBUFFERED=1` / `stdbuf -oL` |
| 交互式命令直接失败 | stdin 是 DEVNULL | 加非交互参数 |
| Plan 里 `depends_on` 全报 `depends_on_unknown` | 写了跨 Stage 依赖 | 拆成 Stage 顺序 |
| `check_schedule` 阻塞很久 | 它在同步跑 Plan | 用 `Start-Process` 后台跑 `run_plan` |
| 提交时报 `plan_spec_not_found` | `plan` 路径相对的是**当前 cwd**，不是 spec 所在目录 | 写绝对路径，或用 `--root` 同级的相对路径 |
| `prune` 删了不该删的 | `--keep` 语义是「每个 plan 保留 N 份」 | 先 `--dry-run` |
| 批了 `approve --all` 之后有任务跑起来了 | 它批了所有 pending | 只在对环境有完全把握时用；日常批准单个 |
