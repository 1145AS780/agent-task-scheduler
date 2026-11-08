---
name: agent-task-scheduler
description: A local task scheduler an AI agent starts, polls, and steers between turns. Three entity layers (Task / Plan / Schedule), L0-L3 permission tiers with human approval, intra-stage dependency orchestration with failure propagation, step retry with backoff, stall detection, run snapshots and logs, a built-in HTTP downloader with resume and integrity check, torrent and magnet download via aria2c, and automatic encoding conversion via the @enc hook. Use when a long-running command must leave the chat turn, when commands need staged orchestration, when work repeats on a schedule, when batch-downloading files or torrents, or when a high-risk command needs approval first. 中文触发：面向 AI Agent 的本地任务调度技能，Agent 能在轮次之间启动、轮询并中途转向。三层实体（Task / Plan / Schedule）、L0-L3 分级权限与人工审批闭环（有效期、批量批准、白名单放行、存活租约免审）、Stage 内依赖编排（depends_on + 失败传播）、步骤自动重试与退避、停滞检测（plan_stalled）、运行快照与调度器日志、内置 HTTP 下载器（断点续传、自动重试、完整性校验、并发与限速）、种子/磁力下载（aria2c）、命令成功后自动编码转换（`@enc` 钩子）。当需要让长时间运行的命令脱离对话轮次、分阶段编排多条命令、按周期反复执行、批量下载文件或种子、把多条命令一次性登记为指令包（仅登记不执行）、或需要审批才能执行高风险命令时使用。Licensed MIT. Source: github.com/1145AS780
version: "2.9.1"
---

# agent-task-scheduler v2.9.1

一个纯本地、无守护进程的调度器。它把「命令」分成三层实体，用文件系统作为唯一状态源，由 Agent 在轮次之间启动、轮询并中途转向。

- 不依赖外部服务、不依赖第三方库（仅 Python 3 标准库）
- 所有状态落在 `ATS_ROOT`（默认 `<cwd>/.ats`），可用文本编辑、可版本控制、可异地备份
- 每个命令都以 JSON 输出，便于 Agent 解析；退出码语义稳定

---

## 1. 什么时候用

**适用**

- 训练、构建、批量转码、长时间测试等耗时命令，不想占住对话轮次
- 需要按固定节奏反复跑同一套命令（巡检、轮询、定时抓取）
- 多条命令有先后关系，其中一条失败后下游应自动跳过
- 失败可能只是偶发（网络抖动、锁竞争），需要自动重试
- 高风险命令（删除、发布、动生产数据）需要人工点头才允许执行
- 需要事后回看「这次跑到哪一步、每步退出码是多少」
- 需要批量下载文件、断点续传或按限速抓取（直接用内置 `download`，不必自己写脚本）；磁力链 / `.torrent` 用 `torrent`（依赖 aria2c）

**不适用**

- 毫秒级、必须在同一事务内完成的原子操作
- 需要真实分布式锁、多机协调的场景（本技能只做单机文件锁语义）
- 需要秒级精确定时（本技能靠 Agent 轮询驱动，实际精度取决于轮询间隔）
- 需要不可篡改身份鉴权的场景（身份来自配置/环境变量，属声明式而非安全边界）

---

## 2. 三层模型

![三层实体模型与权限闸门安检链](assets/ats-architecture.zh-CN.svg)

| 层 | 载体 | 一句话职责 | 生命长度 |
| --- | --- | --- | --- |
| Task | `tasks/<task_id>/` | 单条命令 + 权限 + 超时 | 一次执行 |
| Plan | `schedules/<sid>/plans/<pid>/` | 一次编排：多个 Stage、每个 Stage 多个 Step | 一次编排 |
| Schedule | `schedules/<sid>/schedule.json` | 周期或一次性触发 Plan | 长期 |

- Stage 内的 Step 可以有**单层** `depends_on` 依赖；跨 Stage 的先后关系用 **Stage 切分**（前一个 Stage 全部结束才进下一个）。
- 每个 Step 本质上是「带权限门的 Task」。Plan 层的 Step 直接复用 Task 层的 `permission_gate`、审批、事件、日志能力。

```
Schedule（周期/一次性）
  └─ Plan（第 N 次尝试，id = <sid>-a<N>）
       ├─ Stage s1（serial | parallel）
       │    ├─ Step one  ──→ 可 depends_on 同 Stage 内其它 Step
       │    ├─ Step two
       │    └─ Step three
       └─ Stage s2
```

---

## 3. 60 秒上手

### 3.1 单条命令

```bash
# 创建
python scripts/ats.py create build1 --command "npm run build" --level L1 --timeout 900

# 异步启动（立即返回，进程脱离对话）
python scripts/ats.py run build1

# 之后任意时刻同步状态
python scripts/ats.py check build1
python scripts/ats.py show build1 --lines 40
```

### 3.2 一次性编排

`plan.json`：

```json
{
  "id": "ci1",
  "title": "lint + test",
  "on_stage_failure": "continue",
  "stages": [
    {"id": "lint", "mode": "serial", "steps": [
      {"id": "eslint", "command": "npm run lint", "level": "L1", "timeout": 300}
    ]},
    {"id": "test", "mode": "parallel", "steps": [
      {"id": "unit", "command": "npm run test:unit", "level": "L1", "timeout": 600, "retry": 1, "retry_backoff": 10},
      {"id": "e2e",  "command": "npm run test:e2e",  "level": "L1", "timeout": 600, "depends_on": "unit"}
    ]}
  ]
}
```

```bash
python scripts/ats_plan.py submit_plan --spec plan.json
python scripts/ats_plan.py run_plan ci1-a1
python scripts/ats_plan.py history ci1
```

### 3.3 周期任务

`schedule.json`：

```json
{"id": "poll1", "every": 600, "max_runs": 12, "plan": "./plan.json"}
```

```bash
python scripts/ats_plan.py submit_schedule --spec schedule.json
python scripts/ats_plan.py check_schedule poll1   # 每次轮询调用；到期才触发
```

`check_schedule` 的返回里带 `next_poll`，按它决定下一次轮询间隔即可。

### 3.4 下载

```bash
# 单个文件（带 SHA-256 校验）
python scripts/ats.py download --url https://example.com/a.zip --out D:\dl --sha256 <hex>

# 批量清单（.json / .txt / .csv），4 并发 + 限速 512 KiB/s
python scripts/ats.py download --file urls.json --out D:\dl --concurrency 4 --rate 524288

# 调试时同步等完；不加 --wait 则立即返回、脱离对话
python scripts/ats.py download --url https://example.com/a.zip --wait
```

下载任务就是一个普通 Task（`kind: "download"`），因此 `check` / `show` / `stop` / `events` 全部可用，`--level` 也走同一套审批门。

### 3.5 种子下载

```bash
# 磁力链
python scripts/ats.py torrent --magnet "magnet:?xt=urn:btih:..." --out D:\dl

# .torrent 文件 + 做种 30 分钟
python scripts/ats.py torrent --torrent D:\dl\sintel.torrent --out D:\dl --seed-time 30
```

`torrent` 同样是普通 Task（`kind: "torrent"`）——状态、审批、日志、`stop` 全套复用，详见 §4.4。依赖 aria2c（`winget install --id aria2.aria2 -e`）。

### 3.6 指令包

```bash
# 一次登记 3 条命令（不执行）
python scripts/ats.py pack "echo A,echo B,echo C" --pack-id pk-demo

# 按包查看
python scripts/ats.py list --pack pk-demo
```

`pack` 只把命令**文本**写成 N 个 `pending` Task，不启动进程、不产生日志；要执行仍逐条 `run <task_id>`（审批门照旧）。

### 3.7 本地控制台

```bash
# 启动（默认 127.0.0.1:8765，--open 自动打开浏览器）
python scripts/ats_ui.py --open

# 未运行则后台自启，已在运行则直接复用（--ensure）
python scripts/ats_ui.py --ensure --open

# Windows 一键启动（等价于 --ensure --open；可传 ATS_ROOT）
scripts\ats_ui.cmd <ATS_ROOT>

# 查看 / 停止由 --ensure 启动的控制台
python scripts/ats_ui.py --status
python scripts/ats_ui.py --stop
```

控制台提供只读监控、运行/停止/重试、审批与创建编排；数据仍全部读写 `ATS_ROOT`，不引入第二套状态。MCP 接入见 §12。

---

## 4. 命令总表

### 4.1 Task 层 `scripts/ats.py`

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| `create` | 创建任务（命令末尾可挂 `@enc` 钩子，见 §4.7） | `--command` / `--cwd` / `--level` / `--timeout` / `--env K=V` / `--no-shell` / `--spec` / `--force` |
| `pack` | 加载指令包：多条命令一次性登记为独立 Task，**不执行**（每项均可挂 `@enc`） | `commands`（英文逗号分隔）/ `--pack-id` / `--cwd` / `--level` / `--timeout` / `--description` |
| `run` | 启动任务 | `--approval AP` / `--wait` / `--force`（重跑需 force） |
| `download` | 创建并启动下载任务（`kind="download"`，复用 Task 全链路） | `--url` / `--file` / `--out` / `--name` / `--sha256` / `--size` / `--header` / `--proxy` / `--concurrency` / `--rate` / `--retry` / `--no-resume` / `--overwrite` / `--insecure` / `--level` |
| `torrent` | 创建并启动种子下载任务（`kind="torrent"`，依赖 aria2c） | `--magnet` / `--torrent` / `--out` / `--seed-time` / `--max-download` / `--max-upload` / `--level` / `--timeout` |
| `check` | 同步运行中任务（超时、退出、监督进程丢失） | 可选 `task_id`；不带则批量 |
| `list` | 列表 | `--status succeeded,failed` / `--pack <pack_id>` / `--json` |
| `show` | 详情 + stdout/stderr 尾部 | `--lines N` |
| `status` | 轻量状态 | `--json` |
| `stop` | 停止运行中任务（杀进程树） | `--approval AP` |
| `delete` | 删除任务目录 | `--approval AP` |
| `approve` | 批准/拒绝审批（可批量） | 多个 id / `--all` / `--reject` / `--note` |
| `approval prune` | 清理已决议审批记录（`pending` 永不删） | `--dry-run` / `--older-than 秒` |
| `events` | 消费全局事件流 | `--lines N`（只看尾部且不推进游标） |
| `logs` | 查看调度器日志 | `--lines N` / `--plain` / `--json` |
| `policy` | 查看生效策略 | `--json` |

### 4.2 Plan / Schedule 层 `scripts/ats_plan.py`

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| `submit_plan` | 提交一次性计划 | `--spec` / `--id` |
| `submit_schedule` | 提交周期计划 | `--spec` / `--id` |
| `run_plan` | 执行一个 plan | `plan_id` / `--approval AP` / `--fail-fast` |
| `resume` | 从暂停/中断处继续 | 支持 schedule_id 或 plan_id / `--approval AP` / `--fail-fast` |
| `check_schedule` | 驱动周期计划 + 停滞检测 | `schedule_id` |
| `pause` | 暂停周期 | `--approval AP` |
| `cancel` | 取消并跳过剩余步骤 | `--approval AP` |
| `resume_schedule` | 恢复 paused 的周期计划 | `schedule_id` / `--approval AP` |
| `rerun_task` | 重跑某 Step 并重新武装下游 | `schedule_id stage_id step_id` / `--approval AP` |
| `rerun_plan` | 把整个 Plan 重置为 pending | `plan_id` / `--approval AP` |
| `list` | 计划总览 | `--json` |
| `show` | 计划详情（含 step 汇总） | `ident` / `--json` |
| `delete` | 删除 plan 或 schedule 目录 | `--approval AP` |
| `history` | 运行快照列表（可附日志） | `--plan` / `--logs` / `--lines` |
| `prune` | 清理旧快照 | `--keep N` / `--dry-run` |

### 4.3 下载器 `ats.py download`

`download` 不是独立体系：它**创建**一个 `kind="download"` 的 Task，把参数写成 `tasks/<id>/download.json`，再用 `argv` 直启 `scripts/ats_download.py`（不经过 shell，避开 Windows 引号问题）。权限、审批、事件、日志、`stop`、`check` 与普通 Task 完全一致。

**输入**

| 参数 | 说明 |
| --- | --- |
| `--url URL` | 下载地址，可重复；可与 `--file` 混用 |
| `--file PATH` | 清单：`.json`（`{"outputs": "...", "items": [...]}` 或纯数组）/ `.txt`（每行 `URL<TAB>文件名`）/ `.csv`（`url,文件名`，首行表头可省） |
| `--out DIR` | 输出目录；缺省取清单里的 `outputs`，再缺省为当前目录 |
| `--name NAME` | 单 URL 时的文件名 |
| `--sha256 HEX` / `--size N` | 单 URL 时的完整性期望值 |
| `--header "K: V"` / `--proxy URL` / `--insecure` | 请求头（可重复）/ 代理 / 跳过 HTTPS 证书校验 |

**行为**

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--concurrency N` | 2 | 线程池并发条目数 |
| `--rate N` | 0 | 全局限速（字节/秒），跨并发共享；0 不限 |
| `--retry N` | 2 | 单个条目失败后的重试次数（共 N+1 次） |
| `--backoff N` | 3 | 线性退避基数：第 k 次重试前等 `N*k` 秒 |
| `--io-timeout N` | 30 | 单次网络读超时 |
| `--no-resume` / `--overwrite` | 关 | 禁用续传 / 目标已存在也重下 |
| `--level L0-L3` | L1 | 走 `permission_gate`；身份不足时返回 `approval_required` 并带 `approval_id` |
| `--timeout N` | 0 | 整个下载任务的上限秒数；超时由 supervisor 杀进程，任务状态 `timeout` |
| `--wait` | 关 | 同步等完（调试用，会占住当前轮次） |

**断点续传**：半成品落在 `<目标>.part`，重试或下次运行按 `.part` 的字节数续传（`Range: bytes=N-`）。

- `206` → 追加写；`200`（服务端不支持 Range）→ 丢弃偏移从头重下
- 续传且要求 SHA-256 时，先把已有 `.part` 读入哈希再追加新字节
- `416` 且 `Content-Range` 的 total 等于偏移 → 视为已完成，直接改名落盘
- 只有完整通过大小 / SHA-256 校验后才 `os.replace(.part → 目标)`

**去重与命名**：`(URL, 目标文件名)` 完全相同的条目会被合并（返回里 `deduped` 计数）；**不同 URL 撞同名文件**直接报错 `name_conflict:<name>`，避免并发覆盖。

**stdout 协议**（`tasks/<id>/stdout.log`，JSONL）：`item_start` / `item_retry` / `item_done` / `item_error` / `summary`（最后一行，含 `total` / `succeeded` / `failed` / `bytes` / `elapsed` / `items`）。

**退出码**：worker 全成功 `0`、有条目失败 `1`、spec 非法 `2`。`ats.py download --wait` 本身仍按 Task 语义返回 `0/2/4`——**下载是否全部成功看 `task.status` 或 `summary`**。

**返回结构**（默认异步）：

```json
{"task_id": "dl-1a2b3c", "status": "starting", "supervisor_pid": 12345, "detached": true,
 "download": {"items": 2, "outputs": "<output-dir>", "deduped": 0,
              "spec": "tasks/dl-1a2b3c/download.json", "log": "tasks/dl-1a2b3c/stdout.log"}}
```

spec 报错枚举：`missing_url` / `bad_url` / `bad_header` / `name_requires_single_url` / `digest_requires_single_url` / `too_many_items`（上限 500）/ `name_conflict:<name>` / `manifest_not_found` / `manifest_invalid` / `manifest_empty` / `item_missing_url`。

### 4.4 种子下载器 `ats.py torrent`

`torrent` 与 `download` 同一套路：创建一个 `kind="torrent"` 的 Task，参数写成 `tasks/<id>/torrent.json`，`argv` 直启 `scripts/ats_torrent.py`。worker 为每个任务拉起一个**只监听 `127.0.0.1`** 的 aria2c RPC 实例，下完（或到做种时长）即关；`check` / `show` / `stop` / `events` / `delete` 全部复用。

| 参数 | 说明 |
| --- | --- |
| `--magnet LINK` / `--torrent FILE` | 二选一（必填）：磁力链或 `.torrent` 文件 |
| `--out DIR` | 输出目录，默认当前目录 |
| `--seed-time MIN` | 做种分钟数；`0`（默认）表示下完即停 |
| `--max-download BPS` / `--max-upload BPS` | 全局上下行限速（字节/秒），0 不限 |
| `--timeout N` | 整个任务上限秒数，超时由 supervisor 杀进程（状态 `timeout`） |
| `--level L0-L3` | 默认 L1，与 `download` 同一审批门 |

**依赖**：aria2c。解析顺序 `ATS_ARIA2` → `PATH` → WinGet / scoop / choco 常见安装目录。安装：

```bash
winget install --id aria2.aria2 -e
```

**行为**

- 下载目录会留 `*.aria2` 控制文件（`--auto-save-interval=10`）；`stop` 后 `run --force` 从上次保存点续传
- 做种期间 `torrent_progress` 带 `seeding: true` 与 `seed_remaining`（秒）
- 磁力链先下元数据，worker 跟随 `followedBy` 换到真实 gid，并输出一条 `torrent_metadata`
- 找不到 aria2c 时任务以退出码 `3` 失败，`stdout.log` 里是 `aria2_not_found` + 安装提示
- aria2c 带 `--stop-with-process=<worker pid>`：worker 被强杀时它自行退出，不留孤儿

**stdout 协议**（`tasks/<id>/stdout.log`，JSONL）：`aria2_start` / `torrent_start` / `torrent_metadata` / `torrent_progress`（约 5 秒一条）/ `torrent_done` / `torrent_error` / `summary`（最后一行）。

**退出码**：worker 完成 `0`、下载失败 `1`、spec 非法 `2`、找不到 aria2c `3`。`--wait` 本身仍按 Task 语义返回 `0/2/4`。

**返回结构**（默认异步）：

```json
{"task_id": "tor-1a2b3c", "status": "starting", "supervisor_pid": 12345,
 "detach_mode": "wmi",
 "torrent": {"source": "torrent", "outputs": "<output-dir>", "seed_time": 0,
             "spec": "tasks/tor-1a2b3c/torrent.json",
             "aria2": "<aria2c-path>", "log": "tasks/tor-1a2b3c/stdout.log"}}
```

### 4.5 指令包 `ats.py pack`

`pack` 把一段**逗号分隔的文本**拆成多条命令，各建一个 `pending` Task（`kind="pack"`，共享 `pack_id`）。**它只传文本，不执行**：不写 `stdout.log`、不拉 supervisor、不解析命令内容。

```bash
python scripts/ats.py pack "npm run build,npm test,git status" --pack-id pk-build
```

| 规则 | 说明 |
| --- | --- |
| 分隔符 | 未转义的**英文逗号** |
| 转义 | `\,` → 字面逗号；`\\` → 字面反斜杠 |
| 裁剪 | 每项 `strip()` |
| 空包 | `pack_empty`，退出码 2，不落盘 |
| 空项 | `pack_empty_item`（`indexes` 给出位置），退出码 2，**整包都不落盘** |
| 执行 | 不执行；逐条 `run <task_id>` 才会真正启动 |

**返回结构**：

```json
{"pack_id": "pk-build", "count": 3, "executed": false,
 "note": "指令包只登记命令文本，未启动任何任务",
 "tasks": [{"id": "t-1a2b3c", "index": 1, "command": "npm run build", "level": "L1", "status": "pending"}]}
```

**查看与流转**：`list --pack pk-build` 筛出整包；每条 Task 与普通 Task 完全同权——`run` / `check` / `show` / `stop` / `delete` 都可用，审批门不受 `pack` 影响。

### 4.6 全局参数

两个脚本都支持 `--root <dir>`，优先级：`--root` > 环境变量 `ATS_ROOT` > `<cwd>/.ats`。

### 4.7 `@enc` 后缀钩子（命令成功后自动转码）

在 `create` / `pack` 的命令文本**末尾**追加一段 `@enc <转换器参数>`，该任务就会在主命令**退出码为 0** 之后自动执行一次编码转换。`@enc` 必须是独立 token（前后为空白、行首或行尾）。

```bash
python scripts/ats.py create enc-logs --level L1 \
  --command "make build @enc --from gbk --to utf-8 build\\logs"
```

| 项 | 语义 |
| --- | --- |
| 拆分时机 | `create` / `pack` 落盘时即拆开：`task.json` 的 `command` 只留主命令，钩子写入 `post_hook`（`{"type": "enc", "args": "..."}`） |
| 触发条件 | 主命令 `exit_code == 0`；失败、超时、被 `stop` 时不执行钩子，也不生成钩子日志 |
| 失败语义 | 钩子退出码非 0 → 任务整体 `failed`，任务 `exit_code` 取钩子退出码；Plan 层按既有失败传播规则处理 |
| 日志与事件 | 钩子输出写 `tasks/<id>/hook-enc.log`；发事件 `task_post_hook`（含 `status` / `exit_code` / `error_type`） |
| 转换器定位 | 环境变量 `ATS_ENCODER_PATH` > `<task cwd>/tools/encoding_converter.py` > `<当前目录>/tools/encoding_converter.py`；都不存在 → `encoder_not_found` |
| 参数归属 | `@enc` 之后的参数原样交给转换器（`path` 是**位置参数**，不是 `--path`） |
| 坏语法 | `create` 报 `bad_hook`；`pack` 报 `bad_hook_item`（整包不落盘）；退出码均为 2 |

钩子只是把转换器登记到任务上，转换器本身仍是独立 CLI，可单独使用：

```bash
python tools/encoding_converter.py logs --detect --to utf-8 --dry-run
```

`--spec` 传入的 `command` 走同一套拆分逻辑；`ATS_ENCODER_PATH` 适合在转换器不在任务 `cwd` 下时显式指定。

---

## 5. 权限模型（L0-L3）

### 5.1 等级

| 等级 | 语义 | 典型命令 |
| --- | --- | --- |
| L0 | 只读、无副作用 | `ls`、`git status`、`cat` |
| L1 | 可恢复的写操作 | 构建、跑测试、写临时文件 |
| L2 | 有外部影响 | 部署、发布、改配置、删数据 |
| L3 | 不可逆/高危 | 删库、强推、动生产 |

### 5.2 判定顺序

1. **白名单放行**：命令文本命中 `allow_rules` 任一正则 → 直接允许（优先于等级）
2. **策略上限**：`level > max_level` → 直接拒绝，审批与租约都不能绕过
3. **身份上限**：`level <= actor_level` → 直接允许
4. **存活租约免审**：`lease_autonomy` 总闸开启、租约存活且命令命中其白名单 → 直接允许（见 5.6）
5. **审批**：否则要求有效审批；无审批则创建审批记录、投递事件、返回 `approval_required`

有效等级 = `min(required_level, actor_level, max_level)`，用于解释为什么被拦。
`reason` 取值按上表一一对应：`whitelist` / `denied_policy` / `level` / `lease_autonomy` / `approval` / `approval_required`。

### 5.3 策略文件与环境变量

`<root>/policy.json`（可选）：

```json
{
  "max_level": "L3",
  "actor_level": "L2",
  "allow_rules": ["^git status", "^npm run (lint|test)", "^pytest"],
  "approval_ttl_seconds": 3600,
  "keep_runs_per_plan": 5,
  "stall_seconds": 300,
  "lease_autonomy": {
    "enabled": false,
    "max_level": "L2",
    "rules": ["^jar-deploy", "^gradlew build"],
    "deny_rules": ["--force", "^rm "]
  },
  "lease_file": ""
}
```

环境变量覆盖同名键（优先级最高）：

| 键 | 环境变量 | 说明 |
| --- | --- | --- |
| `max_level` | `ATS_MAX_LEVEL` | 策略硬上限 |
| `actor_level` | `ATS_ACTOR_LEVEL` | 当前身份等级 |
| `allow_rules` | `ATS_ALLOW_RULES` | 正则列表，用 `\|\|` 分隔 |
| `approval_ttl_seconds` | `ATS_APPROVAL_TTL` | 审批有效期，`<=0` 表示不过期 |
| `keep_runs_per_plan` | `ATS_KEEP_RUNS` | 每个 plan 保留快照数，`<=0` 不清理 |
| `stall_seconds` | `ATS_STALL` | 停滞阈值，`<=0` 关闭检测 |
| `lease_autonomy` | `ATS_LEASE_AUTONOMY` | 租约免审配置，JSON 字符串；坏值一律回落关闭 |
| `lease_file` | `ATS_LEASE_FILE` | 租约文件路径，默认 `<root>/../temp/py_lease.json`，不存在则回落 `<root>/../.autopilot/temp/py_lease.json` |

### 5.4 审批闭环

```bash
# 1. 执行被拦，返回 approval_id 与 resume
python scripts/ats.py run deploy1 --level L2
# → {"error":"approval_required","approval_id":"ap-3f2a...","resume":"python ats.py run deploy1 --approval <approval_id>"}

# 2. 人工批准（可批量）
python scripts/ats.py approve ap-3f2a91c4
python scripts/ats.py approve --all
python scripts/ats.py approve ap-3f2a91c4 --reject --note "窗口期不允许"

# 3. 用审批重跑
python scripts/ats.py run deploy1 --approval ap-3f2a91c4
```

审批记录字段（`approvals/<id>.json`）：

```json
{
  "id": "ap-3f2a91c4",
  "command": "npm run deploy",
  "required_level": "L2",
  "status": "approved",
  "created_at": 1793512103,
  "expires_at": 1793598503,
  "resolved_at": 1793512103,
  "resolved_by": "actor:L3",
  "note": null,
  "source": "run",
  "schedule_id": null, "plan_id": null, "stage_id": null, "step_id": null,
  "task_id": "deploy1",
  "resume": "python ats.py run deploy1 --approval <approval_id>"
}
```

关键语义：

- 审批**绑定命令原文**：命令改了必须重新审批（返回 `mismatch`）
- 审批**有有效期**：过期后 `approval_check` 返回 `expired`，`approve` 拒绝，执行被拦
- **拒绝不可翻案**：`status=rejected` 后同一审批 ID 永远不可用；需要重新发起
- Plan 层暂停后自动重发：step 的审批缺失/过期，且当前身份足够时，重新创建审批并更新 `step.json` 的 `approval_id` 与 `approval_reissued_at`
- 任务级审批事件投递到 `<root>/events.jsonl`；Plan 级审批事件投递到 `schedules/<sid>/events.jsonl`

### 5.5 身份模型的能力边界

`actor_level` 是**声明式**的：任何能改环境变量或 `policy.json` 的进程都能自称 L3。它约束的是「Agent 自觉」，不是敌对环境下的安全边界。需要真正鉴权请在外层包一层（CI token、人工二次确认、操作系统账户隔离）。

### 5.6 存活租约免审（`lease_autonomy`）

当外部授权窗口（`.trae/scripts/py_consent.py`）仍在运行时，本调度器可以据此对**命中白名单的高危指令**跳过逐条审批。它是第 4 条放行路径，**不是新等级**。

五道关缺一不可，任一不满足即退回普通审批：

| 关 | 判据 | 不满足时 |
| --- | --- | --- |
| 1 总闸 | `policy.lease_autonomy.enabled` 为真且有 `rules` | 权限门根本不读租约文件 |
| 2 等级 | `required_level <= min(policy.max_level, 租约授予的 max_level)` | 退回审批（策略上限那一关已先行拦截） |
| 3 存活 | 租约 `granted`、心跳未过期（默认 8s）、未到期限、宿主 PID 仍存活 | 退回审批 |
| 4 命令 | 命中 `rules` 且不命中 `deny_rules` | 退回审批 |
| 5 边界 | 租约里勾过的「额外禁止」未命中该动作 | 退回审批并记 `lease_autonomy_blocked` |

放行时**不产生待办**，但会写一条 `status=approved` 的审批记录留痕：`auto_approved=true`、`resolved_by="py_lease:<令牌>"`、`note="lease_autonomy: <规则>"`，并附带 `lease_token` / `lease_pid` / `lease_heartbeat_at`。事后审计走 `approvals/`，`approve --all` 不会把它当成待决议项。

```json
// policy.json 片段
"lease_autonomy": {"enabled": true, "max_level": "L2", "rules": ["^jar-deploy"], "deny_rules": ["--force"]}
```

关键语义：

- **默认关闭**：配置缺失、字段拼错、类型不对、`enabled` 非真，一律按关闭处理（fail-closed）
- **不突破策略上限**：`max_level` 仍是硬天花板，租约只能放宽「审批」这一关，不能放宽等级
- **租约授予由用户勾选**：`py_consent.py` 第 4 题选「否 / 仅 L2 / 含 L3」，自定义文本**不构成授予**
- **随租约消亡**：窗口关闭或心跳过期后，下一次权限门立刻回到审批流程，无需清理配置
- **只读判定**：本调度器只读租约文件，不写、不删、不杀进程；撤销仍由用户关窗完成

---

## 6. 编排规范

### 6.1 Plan spec

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 否 | Plan 基名；不填自动生成 |
| `title` | string | 否 | 展示用 |
| `on_stage_failure` | `continue` \| `stop` | 否 | 默认 `continue`；`stop` 表示某 Stage 失败后跳过后续 Stage |
| `fail_on_critical` | `continue` \| `stop` | 否 | 默认 `continue`；`stop` 表示标记 `critical: true` 的 Step 最终失败时立即终止整个 Plan（同批兄弟进程树一起杀掉） |
| `stages` | array | 是 | Stage 列表，至少一个 |

### 6.2 Stage spec

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | Stage 内唯一 |
| `mode` | `serial` \| `parallel` | 否 | 默认 `serial`；`parallel` 每波并行启动所有 ready Step |
| `steps` | array | 是 | 至少一个 Step |

### 6.3 Step spec

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | Stage 内唯一 |
| `command` | string | 是 | shell 命令原文，也是审批绑定文本 |
| `cwd` | string | 否 | 缺省用 Plan 的 `cwd`，再缺省用调度器进程 cwd |
| `level` | L0-L3 | 否 | 默认 L1 |
| `timeout` | int | 否 | 秒；`0`/缺省表示不限时 |
| `env` | object | 否 | 追加环境变量 |
| `depends_on` | string \| string[] | 否 | **同 Stage 内**依赖；不得自依赖、不得成环 |
| `retry` | int | 否 | 失败后额外重试次数；`0` 表示不重试 |
| `retry_backoff` | int | 否 | 每次重试前等待秒数；`0` 表示立即重试 |
| `idempotent` | bool | 否 | 语义提示；`false` 时重跑前应确认（由 Agent 遵守） |
| `critical` | bool | 否 | 默认 `false`；`true` 且 Plan 设置 `fail_on_critical: "stop"` 时，该 Step 最终失败即终止整个 Plan |

### 6.4 依赖语义

- 依赖只能在**同一个 Stage 内**；跨 Stage 用 Stage 顺序表达
- 依赖的 Step 必须 `succeeded` 才算 ready；`pending/running` 时本轮不调度
- 依赖处于 `failed` / `timeout` / `skipped` → 下游标记 `skipped(dependency)`，并记录 `blocked_by` 与 `dependency_status`
- Stage 内允许链式/菱形依赖；拓扑序稳定（无依赖时保持声明顺序）
- 依赖被跳过会向上传播：A 失败 → B 跳过 → 依赖 B 的 C 也跳过

### 6.5 重试语义

- `max_attempts = 1 + retry`
- 中间失败**不**发 `task_failed`，只发 `task_retry`（含 `attempt`、`max_attempts`、`backoff_seconds`、`prev_error_type`）
- 只有最后一次仍失败才发 `task_failed`，并计入 Stage 失败
- 重试会重新开进程（旧的 stdout/stderr 追加写入，不清空历史）
- 不适合配重试的错误：参数错误、权限拒绝（`policy_denied`）、语法错误、确定性断言失败
- `critical` 的判定发生在**重试耗尽之后**：中间失败只触发 `task_retry`，最终仍失败才终止整个 Plan

### 6.6 Schedule spec

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 否 | Schedule 基名；Plan id 为 `<sid>-a<N>` |
| `every` | int | 是 | 周期秒数，`>0` |
| `plan` | string | 是 | Plan spec 文件路径（提交时内嵌进 schedule.json，之后文件可移动） |
| `start_at` | int | 否 | **首轮**触发的 Unix 秒；缺省 `now + every`；`0` 表示首轮立即。**只影响首轮** |
| `max_runs` | int | 否 | 最大触发次数；达到后 `status=done` |
| `on_stage_failure` | `continue` \| `stop` | 否 | 覆盖 Plan 内设置 |

> `start_at` 只在提交时参与一次 `next_run` 的初始计算。每次触发后 `next_run` 都被重写为 `now + every`，所以从第二轮起节奏完全由 `every` 决定，与 `start_at` 无关。连续调用 `check_schedule` 不会每调一次触发一次——距上次触发不足 `every` 秒时 `triggered` 是空数组。

---

## 7. 状态与事件

### 7.1 Task 状态

`pending` → `starting` → `running` → `succeeded` / `failed` / `timeout` / `killed`

`failed` 之后可 `run --force` 重跑；`running` 时 `delete` 会被拒绝（先 `stop`）。

### 7.2 Plan 状态

`pending` → `running` → `done` / `failed` / `paused` / `cancelled`

- `paused`：有 Step 需要审批
- `failed`：至少一个 Stage 失败（`on_stage_failure=continue` 时仍会跑完其它 Stage）；`critical` 触发或 `--fail-fast` 时立即终止，当前 Stage 剩余 Step 标 `skipped(critical_failed)`，后续 Stage 标 `skipped`
- `cancelled`：被 `cancel` 取消，剩余 Step 标记 `skipped(user)`

### 7.3 Step 状态

| 状态 | 含义 |
| --- | --- |
| `pending` | 未开始 |
| `running` | 进程在跑 |
| `succeeded` | 退出码 0 |
| `failed` | 非 0 退出 |
| `timeout` | 超过 `timeout` 被杀 |
| `skipped` | 四种来源：`user`（`cancel` 取消）、`dependency`（上游失败）、`stage_failed`（`on_stage_failure=stop` 清理）、`critical_failed`（关键任务失败即停） |

### 7.4 事件（`events.jsonl`，JSONL，一行一个）

Task 层（`<root>/events.jsonl`）：

| type | 触发 | 主要字段 |
| --- | --- | --- |
| `task_created` | 创建 | `task_id`、`level`、`command` |
| `task_start` | 启动 | `task_id`、`gate`、`approval_id` |
| `task_end` | 结束 | `task_id`、`status`、`exit_code`、`timed_out`、`duration` |
| `approval_created` | 需要审批 | `approval_id`、`required_level`、`resume`、定位字段 |
| `approval_resolved` | 批准/拒绝 | `approval_id`、`status`、`resolved_by`、`resume` |

Plan 层（`schedules/<sid>/events.jsonl`）：

| type | 触发 |
| --- | --- |
| `schedule_created` / `schedule_triggered` / `schedule_paused` / `schedule_cancelled` | Schedule 生命周期 |
| `plan_materialized` / `plan_start` / `plan_resumed` / `plan_end` | Plan 生命周期 |
| `plan_paused` | 因审批暂停 |
| `plan_stalled` | 停滞（`scope=step｜plan`） |
| `stage_done` / `stage_failed` / `stage_skipped` | Stage 结束 |
| `task_start` / `task_done` / `task_failed` / `task_retry` / `task_skipped` | Step 粒度 |
| `task_aborted` | 关键任务失败触发即停，同批兄弟进程被终止（`reason=critical_failed`、`pid`） |
| `approval_created` / `approval_resolved` | 审批（带 `schedule_id` 时投递到此） |
| `run_snapshot` / `prune_runs` / `steps_recovered` / `task_rerun` | 历史与恢复 |

消费方式：`python scripts/ats.py events` 读取全局流并推进 `events_state.json` 游标，只返回增量。Plan 事件由 `check_schedule` 汇总在返回 JSON 的 `stalled` 字段；也可直接读文件。

---

## 8. 日志、历史与清理

### 8.1 `scheduler.log`

`<root>/scheduler.log` 是全局运行日志（JSONL），超过 2 MB 自动轮转为 `scheduler.log.1`。

```bash
python scripts/ats.py logs --lines 20          # 人类可读
python scripts/ats.py logs --lines 50 --json   # 机器可读
python scripts/ats.py logs --plain             # 原样输出
```

每条日志都带 `schedule_id` / `plan_id` / `stage_id` / `step_id`（有则记），便于按计划过滤。

### 8.2 运行快照

每次 Plan 结束写 `schedules/<sid>/runs/<plan_id>/`：

```
runs/ci1-a1/
  plan.json        # 当时的 Plan 元数据
  schedule.json    # 当时的 Schedule 元数据
  steps.jsonl      # 每行一个 Step 终态
  summary.json     # 汇总（counts、时间、状态）
```

```bash
python scripts/ats_plan.py history ci1
python scripts/ats_plan.py history ci1 --plan ci1-a3 --logs --lines 30
```

### 8.3 保留策略

- 默认每个 plan 保留最近 `keep_runs_per_plan=5` 份快照
- Plan 收尾时自动裁剪，并写 `prune_runs` 事件与日志
- 手动清理：`prune`（`--dry-run` 只预览）；`--keep 0` 表示不清理

---

## 9. 退出码

| 码 | 含义 | 典型场景 |
| --- | --- | --- |
| 0 | 正常 | 查询成功、执行完成、即使业务命令非 0（业务退出码在 JSON 的 `exit_code`） |
| 2 | 参数/spec 非法 | spec 缺字段、`every` 非正、缺 `--approval` |
| 3 | 不存在或状态冲突 | task 不存在、重复 submit、running 时 delete |
| 4 | 权限不足 | `denied_policy`、`approval_required`、审批身份不足 |

注意：Step/命令失败**不**改变调度器退出码，成功与否看 JSON 的 `status` / `exit_code`。这样 Agent 不会因为业务失败而误判调度器出错。

---

## 10. 典型工作流

### 10.1 长任务：创建 → 轮询 → 收集

```bash
python scripts/ats.py create train1 --command "python train.py --epochs 30" --level L1 --timeout 7200
python scripts/ats.py run train1              # 异步返回 supervisor_pid
# 轮询
python scripts/ats.py check train1           # next_poll 为 5 时继续轮询
python scripts/ats.py show train1 --lines 40 # 看尾部输出
```

### 10.2 有审批的高危步骤

```bash
# Plan 跑到第 3 步被拦 → status=paused，返回 approval
python scripts/ats_plan.py run_plan rel1-a1
# 人工批准
python scripts/ats.py approve ap-xxxx
# 继续（已完成的 Step 不会重跑）
python scripts/ats_plan.py resume rel1 --approval ap-xxxx
```

### 10.3 周期巡检

```bash
python scripts/ats_plan.py submit_schedule --spec patrol.json
python scripts/ats_plan.py check_schedule patrol1
# 按返回的 next_poll（秒）休眠后再调
# 返回 stalled 非空 → 计划卡住，需要人工介入或 rerun_task
```

### 10.4 局部重跑

```bash
python scripts/ats_plan.py rerun_task ci1 test e2e
# 返回 rearmed_steps：被重新武装的下游
python scripts/ats_plan.py run_plan ci1-a1
```

### 10.5 事故排查

```bash
python scripts/ats_plan.py history ci1            # 有几次尝试、每次结果
python scripts/ats.py logs --lines 50             # 时间线
python scripts/ats.py logs --plain | Select-String "ci1-a1"   # 只看某次
```

---

## 11. 设计约束

1. **状态即文件**：所有判断都从磁盘重读，不依赖内存缓存，因此可以在任意轮次、任意进程继续。
2. **单层依赖**：Stage 内依赖刻意不做成通用 DAG。跨 Stage 的复杂拓扑请拆成更多 Stage。
3. **同步执行 + 外部轮询**：`run_plan` 在一次调用内跑完；长 Step 会占用该次调用。需要真正异步请在外部用 `Start-Process` 派生，再用 `check_schedule` / 事件观察。
4. **审批绑定原文**：命令字符串是审批主键，不要在执行时临时拼接变量。
5. **不静默失败**：每次失败都有事件 + 日志 + 退出码三处可查。
6. **幂等边界**：`submit_plan` / `submit_schedule` 对同名 schedule 拒绝重复创建；`create` 需 `--force` 覆盖。

---

## 12. MCP 接入（stdio 与 HTTP）

本技能提供两种 MCP 用法，**共用同一份工具表**：HTTP 端点直接 `import ats_mcp`，复用它的 `TOOLS` 与分发逻辑，不维护第二套工具定义。

| 用法 | 进程归属 | 传输 | 启停方式 | 适用场景 |
| --- | --- | --- | --- | --- |
| `scripts/ats_mcp.py`（stdio） | MCP 客户端自己拉起 | stdio，每行一个 JSON-RPC 2.0 消息 | 客户端断开即退出 | 桌面 / IDE 里的 MCP 客户端 |
| `scripts/ats_mcp_http.py`（HTTP） | `scripts/ats_ui.py` 控制台进程内 | Streamable HTTP，`POST /mcp` | 控制台开关，**默认关闭** | 需要网页开关、常驻本机端点 |

### 12.1 stdio：客户端拉起，单文件独立运行

```bash
python scripts/ats_mcp.py [--root DIR]
```

- stdout 只写协议消息，日志 / traceback 全部走 stderr；`--root` 原样透传给 `ats.py` / `ats_plan.py`
- 不 import `ats.py` / `ats_plan.py`，每次工具调用都以子进程走现有 CLI，状态读写只有一份真相
- 启动只检查同目录的 `ats.py` 与 `ats_plan.py` 是否存在，缺失时退出码 `2`
- 客户端配置：

```json
{
  "mcpServers": {
    "agent-task-scheduler": {
      "command": "python",
      "args": ["<repo>/scripts/ats_mcp.py", "--root", "<ATS_ROOT>"]
    }
  }
}
```

### 12.2 HTTP：控制台内置，开关控制，默认关闭

HTTP 端点由 `ats_ui.py` 在进程内创建，**不会随控制台启动而自动监听**：

```bash
# 控制台默认 127.0.0.1:8765；MCP 端点默认使用 控制台端口 + 1（即 8766）
python scripts/ats_ui.py --open

# 显式指定 MCP 端口
python scripts/ats_ui.py --port 8765 --mcp-port 8766 --open
```

| 项 | 说明 |
| --- | --- |
| 默认端口 | 控制台端口 + 1；`--mcp-port` 可覆盖（`ats_ui.py` 的 `--port` 仍是控制台端口） |
| 端点地址 | `http://127.0.0.1:<mcp-port>/mcp` |
| 开启方式 | 控制台界面的 MCP 开关，或 `POST /api/action` 且 body 为 `{"action":"mcp.start","confirm":true}` |
| 确认要求 | 缺少 `confirm:true` 时返回 HTTP 400 `bad_action`；**开启等于授权本机任意进程执行命令** |
| 状态 | `stopped`（未启动）/ `running`（运行中）/ `failed`（启动失败，界面显示原因，例如端口被占用） |
| 关闭 | `{"action":"mcp.stop"}`；立即停止监听并释放端口 |
| 会话 | `initialize` 用响应头 `Mcp-Session-Id` 下发；`DELETE /mcp` 结束会话；空闲 30 分钟的会话自动剔除 |
| 传输细节 | `GET /mcp` 返回 405（不伪造 SSE 推送流）；`GET /health` 返回端点状态；通知消息返回 202 空体 |
| 进程退出 | 控制台退出时一并停止端点、释放端口 |

客户端配置（地址与界面「一键复制」给出的片段一致）：

```json
{
  "mcpServers": {
    "agent-task-scheduler": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

**协议版本**：服务端支持 `2025-06-18` / `2025-03-26` / `2024-11-05`；`initialize` 请求命中哪个就回声哪个，否则回最新的 `2025-06-18`。HTTP 端点上报 `2025-06-18`。

### 12.3 工具清单（16 个）

工具名与 `ats_mcp.py` 的 `TOOLS` 一一对应，stdio 与 HTTP 完全相同：

| 分组 | 工具 | 作用 |
| --- | --- | --- |
| Task | `task_create` | 创建任务，不启动 |
| Task | `task_run` | 启动任务；`wait=true` 阻塞到结束 |
| Task | `task_status` | 单个任务的轻量状态 |
| Task | `task_list` | 列表，可按状态过滤 |
| Task | `task_logs` | 任务详情 + stdout/stderr 尾部 |
| Task | `task_stop` | 停止任务（杀进程树） |
| Task | `task_check` | 核对存活状态，返回变更 / 运行中 / `next_poll` |
| 审批 | `approval_resolve` | 批准或拒绝审批（可批量） |
| Plan | `plan_list` | 列出 Schedule / Plan 概况 |
| Plan | `plan_show` | 按 id 查看 Plan / Schedule |
| Plan | `plan_run` | 执行 Plan 并等待 Stage 结束 |
| Plan | `plan_resume` | 恢复中断的 Plan 或暂停的 Schedule |
| Schedule | `schedule_check` | 驱动周期计划并返回 `next_run` / `next_poll` |
| Schedule | `schedule_pause` | 暂停周期计划 |
| Schedule | `schedule_resume` | 恢复暂停的周期计划 |
| Schedule | `schedule_cancel` | 取消周期计划并跳过剩余步骤 |

### 12.4 错误行为

JSON-RPC 层（stdio 与 HTTP 相同）：

| 场景 | 返回 |
| --- | --- |
| JSON 无法解析 | `-32700` Parse error |
| 消息不是合法 JSON-RPC 2.0 请求 | `-32600` Invalid Request |
| 未知方法 | `-32601` Method not found |
| 未知工具 | `-32602` Invalid params |
| 工具参数缺失 / 类型错误 | `-32602` Invalid params（携带具体字段说明） |
| 工具执行失败（任务不存在、CLI 非 0 等） | `result.content[0]` 内是错误 JSON，且 `isError:true`（不是协议层错误） |
| 工具调用内部异常 | `isError:true` 的结果 `{"error":"internal_error"}`，traceback 写入 stderr；不返回 `-32603` |
| 通知（无 `id`） | 不产生响应；HTTP 返回 202 空体 |

HTTP 层额外状态码：`403`（`forbidden_host` / `forbidden_origin`）、`415`（Content-Type 非 `application/json`）、`406`（Accept 不含 JSON 或 SSE）、`413`（请求体超过 8 MB）、`404`（路径错误或 `session_not_found`）、`405`（`GET /mcp` 或 `OPTIONS`）、`503`（端点未启动）、`400`（空请求体或缺少 `confirm`）。

### 12.5 安全约束

- **只绑 `127.0.0.1`**：MCP 端点不跟随控制台的 `--host` 暴露到局域网
- **Host 校验**：只接受 `127.0.0.1:<mcp-port>` 或 `localhost:<mcp-port>`
- **Origin 校验**：浏览器来源必须与端点同源；不返回任何 CORS 头
- **默认关闭**：控制台启动不代表 MCP 已开启；开启必须显式确认
- **权限含义**：开启开关等于把「执行命令」的能力暴露给本机任意进程；本端点没有 token / 账户鉴权，别把它当成安全边界
- **不落盘**：会话与连接计数只在内存中，关闭即释放端口；控制台进程退出也会一并释放

---

## 13. 目录索引

- [orchestration-spec.md](references/orchestration-spec.md) — 编排规格、能力矩阵、事件与命令映射
- [permissions.md](references/permissions.md) — 等级、策略、白名单、审批闭环细节
- [data-model.md](references/data-model.md) — 所有 JSON 字段与语义
- [storage-layout.md](references/storage-layout.md) — `.ats` 目录树与清理策略
- [state-machine.md](references/state-machine.md) — 状态迁移与事件对应
- [error-taxonomy.md](references/error-taxonomy.md) — 错误分类与重试矩阵
- [command-guide.md](references/command-guide.md) — 命令书写与调用规范
- [anti-patterns.md](references/anti-patterns.md) — 常见错误用法
- [examples/](references/examples) — 可直接运行的最小样例

---

## 14. 限制与未实现

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 通用 DAG / 跨 Stage 依赖 | 不支持 | 只做 Stage 内单层依赖；跨 Stage 用拆 Stage |
| 不可篡改身份鉴权 | 不实现 | `actor_level` 是声明式的，不是安全边界 |
| 分布式/多机协调 | 不实现 | 单机文件状态，无跨机锁 |
| 秒级精确定时 | 不实现 | 由 Agent 轮询驱动，精度≈轮询间隔 |
| `watch` 长驻监听 | 不实现 | 用 `check` / `check_schedule` 轮询替代 |
| cron 宏语法 | 不实现 | 只支持 `every` 秒数 + `start_at` + `max_runs` |
| 审批黑名单仲裁 | 不实现 | 只有白名单放行，没有「某些命令永不批准」的硬拒绝层（`lease_autonomy.deny_rules` 只作用于租约这一条路径） |
| 租约免审的鉴权强度 | 与 `actor_level` 同级 | 能写 `policy.json` 或伪造租约文件的进程都能自证；不是敌对环境下的安全边界 |
| BT / 磁力 | 支持（`torrent`） | 经 aria2c 下载，需单独安装；单文件 HTTP 多段并发仍不支持 |
| 指令包自动执行 | 不实现 | `pack` 只登记文本（全部 `pending`），不解析、不运行；必须逐条 `run` |
| 钩子类型 | 仅 `enc` | 目前只支持 `@enc` 编码转换钩子；不支持任意「成功后追加命令」的通用链 |
| 按日志文本自动停止 | 不实现 | 停止只依据退出码（含超时），不扫描 `stdout.log` / `stderr.log` 内容 |
| MCP 端点鉴权 | 仅本机回环 + Host/Origin 校验 | 无 token / 账户鉴权；开启开关等于授权本机任意进程执行命令，不是安全边界 |
| MCP 服务端推送 | 不实现 | 只做 Streamable HTTP 的 `POST` / `DELETE`；`GET /mcp` 回 405，不提供 SSE 推送流 |

---

## 15. 版本历史

| 版本 | 变更 |
| --- | --- |
| 2.9.1 | 新增 MCP 适配与本地控制台：`scripts/ats_mcp.py`（stdio，客户端拉起，16 个工具，协议 `2025-06-18` / `2025-03-26` / `2024-11-05`）、`scripts/ats_mcp_http.py`（控制台进程内的 Streamable HTTP 端点，默认关闭，`--mcp-port` 默认控制台端口 + 1，`confirm:true` 才允许开启，只绑 `127.0.0.1` 并校验 Host / Origin）、`scripts/ats_ui.py` 本地 Web 控制台（`--ensure` / `--status` / `--stop`）与 `scripts/ats_ui.cmd` 一键启动（修正为指向 `ats_ui.py`） |
| 2.8 | 新增「存活租约免审」`lease_autonomy`：`policy.json` / `ATS_LEASE_AUTONOMY` 配置 `enabled` / `max_level` / `rules` / `deny_rules`，`lease_file` / `ATS_LEASE_FILE` 指定租约路径（默认 `<root>/../temp/py_lease.json`，不存在则回落 `<root>/../.autopilot/temp/py_lease.json`）；`permission_gate` 与 `gate_operation` 在身份判定之后、审批之前插入该路径，放行写 `status=approved` 的留痕审批（`auto_approved=true`、`resolved_by=py_lease:<令牌>`）不投待办；判定为五道关（总闸 / 等级上限 / 租约存活 / 命令白名单 / 租约自带约束），任一不满足退回审批，坏配置一律回落关闭；`policy` 命令新增 `lease` 段报告租约状态；`py_consent.py` 第 4 题把授予范围变成用户可勾选项 |
| 2.7 | 新增 `@enc` 后缀钩子：命令文本末尾追加 `@enc <转换器参数>` 即把编码转换登记为任务的 `post_hook`，主命令退出码为 0 才执行（转换器 `tools/encoding_converter.py`，可用 `ATS_ENCODER_PATH` 覆盖）；钩子失败则任务 `failed` 且 `exit_code` 取钩子码，日志 `hook-enc.log`、事件 `task_post_hook`；`create` / `pack` 均支持，坏语法报 `bad_hook` / `bad_hook_item`；转换器仍可独立 CLI 使用 |
| 2.6 | 新增「关键任务失败即停」：Step 级 `critical: true` + Plan 级 `fail_on_critical: "stop"`，或 `run_plan` / `resume` 加 `--fail-fast`（临时视全部 Step 为 critical）；判定为**重试耗尽后仍失败**（`failed` / `timeout`），触发后立即 `taskkill /T /F` 终止同批兄弟进程树（`step.json` 记 `aborted_by="fail_fast"` + `aborted_at`，事件 `task_aborted`），当前 Stage 剩余 Step 标 `skipped(critical_failed)`、后续 Stage 标 `skipped`、`plan.status=failed`；修复 Plan 级 `fail_on_critical` 未写入 `plan.json` 与停止路径 `plan_end` 状态残留 `running` |
| 2.5 | 新增 `ats.py pack`「指令包」：一次登记多条命令（英文逗号分隔，支持 `\,` / `\\` 转义），各建独立 `pending` Task（`kind="pack"`、共享 `pack_id`、`pack` 元数据），**只传文本不执行**；`list --pack` 按包筛选；空包 / 空项分别报 `pack_empty` / `pack_empty_item` 且整包不落盘 |
| 2.4 | 新增 `ats.py torrent` + `scripts/ats_torrent.py`：磁力链 / `.torrent`（aria2c JSON-RPC），`*.aria2` 断点续传、做种时长、上下行限速、磁力 `followedBy` 自动换 gid；`kind="torrent"` 复用 Task 全链路；supervisor 分离启动新增 WMI 兜底（`task.json` 的 `detach_mode`） |
| 2.3 | 新增 `ats.py download` + `scripts/ats_download.py`：HTTP(S) 单文件 / 批量清单（`.json` / `.txt` / `.csv`）、Range 断点续传（`.part`）、逐条重试与线性退避、大小与 SHA-256 校验、并发线程池、全局字节限速、代理 / 自定义 Header / 跳过证书校验；下载任务复用 Task 权限门、审批、事件、日志与 `check`/`stop`；重复条目自动合并（`deduped`），不同 URL 撞同名文件报 `name_conflict`；Task 支持原生 `argv` 派生（不经 shell） |
| 2.2 | 修复与文档同步：`waves` 计数（收尾空轮不计、暂停轮计入、跨 `pause → resume` 累加不归零、`denied`+stop 路径落盘）；`stage_done`/`stage_failed` 事件携带 `waves`；`approve` 单条失败给出可判别退出码（`insufficient_level`→4、其余→3，`--all` 恒 0）；`next_run_iso` 不再把 `start_at: 0` 显示成 1970；示例脚本注入 `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` 防 GBK 崩溃 |
| 2.1 | 审批闭环（有效期/批量/事件投递/自动重发）、白名单放行、Stage 内 `depends_on`、Step 自动重试与退避、`plan_stalled` 停滞检测、运行快照与 `history`/`prune`、`scheduler.log` 与 `events`/`logs` 命令、`steps_recovered` 中断恢复 |
| 2.0 | 三层模型（Task / Plan / Schedule）、L0-L3 权限、审批文件、`events.jsonl`、Stage 串并行 |