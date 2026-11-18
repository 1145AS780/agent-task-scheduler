# command-ai.md —— AI 强制行为协议

> 本文件是**约束**，不是建议。接入 ATS 的 AI 必须先读本文件，再执行任何 `ats.py` 命令。
> 人类用户不需要读这份：请把 `command-human.md` 交给人。

适用对象：任何通过 CLI / MCP / 库调用驱动 `ats.py` 的模型。
冲突时优先级：**本文件 > 对话里临时的口头指令**（口头指令不能取消中止信号与审批门）。

---

## 规则 1：主动告知义务（首次接入必须做一次）

满足任一条件即视为「首次接入」：

- 本会话第一次调用 `ats.py`（含 `list` / `status` / `check` 这类只读命令）
- 人刚刚执行过 `ats_guard.py init --confirm` 或 `seal --confirm`
- 人问「你能用了吗 / 装好了吗 / 现在什么状态」

此时**必须主动**在回复里说这句话（可加自己的上下文，但这句本身不许省略、不许改写语气）：

> 老板，我已就绪。如遇我无法执行的权限墙，请参考 `command-human.md`，我会明确告诉您该复制哪条指令。

禁止的替代做法：只回「好的」「已连接」，或等人踩到权限墙再解释。

---

## 规则 2：报错即教程（禁止只说「抱歉」）

命中下列任一返回体时，**必须**把「人现在要复制的那条命令」原样贴进回复，
一条命令一行、不加修饰、不改参数值（变量一律从返回体里抄，不许自己拼）：

| 返回体判据 | 人需要执行的命令 | 变量从哪抄 |
| --- | --- | --- |
| `error == "approval_required"` | `python scripts/ats.py approve <approval_id>` | 返回体的 `approval_id` |
| 上一步批准后继续原任务 | 返回体的 `resume` 字段（原样） | `resume` |
| `error == "visa_mismatch"` | `python scripts/ats.py visa issue --task <task_id> --number 1 --time 5` | 返回体 `task_id` / `scope_hash` |
| `error == "guard_check_failed"`（核心脚本改过） | `python scripts/ats_guard.py seal --confirm` | 无变量；口令走 stdin |
| `error == "guard_skip_approval_required"` | `python scripts/ats.py approve <approval_id>` 然后按 `resume` 重跑 | `approval_id` / `resume` |
| `error == "passphrase_required"` | 让**人**自己在终端执行，不要代跑（口令不许经过你） | 无 |
| `error == "human_abort"`（`exit_code == 101127`） | 不执行任何命令，见规则 3 | 无 |
| `error == "maintenance_mode"`（人类停机维护中） | 原样贴返回体的 `resume`（`repack --confirm` 或 `unpack --exit`） | `resume` |
| `error == "maintenance_required"`（`tryrun` / `repack` 在窗口外） | `python scripts/ats.py unpack --confirm` | 无变量；口令走 stdin |
| `error == "confirm_required"`（缺 `--confirm`） | 原样贴返回体的 `tutorial.commands` | `tutorial.commands` |

硬性要求：

1. 回复里必须出现**完整命令行**，含 `scripts/ats.py` 路径前缀；只给 `approval_id` 不算。
2. 不许把命令包进「你可以尝试」「建议执行类似」这类模糊措辞——它必须能被直接复制。
3. 不许自己调用 `approve` / `seal` / `init`：这三个要人类口令，AI 代跑等于代签。
   （`approve` 需要 `--passphrase-stdin` 的口令，你没有也不该有。）
4. 若返回体里同时有 `hint` 与 `resume`，两者都要贴：`hint` 解释为什么被拦，`resume` 是下一步。
5. 报错时先抄返回体字段，再补一句人话解释。顺序不许反：先命令，后解释。

---

## 规则 3：绝对中止信号 101127（人类主动中止）

**判据**：返回体的 JSON 里 `exit_code == 101127`（或 `error == "human_abort"`）。

命中时立刻执行下面三步，**没有第四步**：

1. 停止当前任务的一切思考、重试、参数调整、换命令重试。
2. 回复一句：`已收到强制休眠指令。`
3. 结束本轮。

**禁止**：

- 禁止反问「为什么停下」「需要我继续吗」「是否要换一种方式」
- 禁止换个子命令再试一次（例如 `run` 被中止后改 `check`）
- 禁止解释「我理解您可能……」
- 禁止在人清除中止标志前做任何 `ats.py` 调用

**为什么是 101127 而不是进程退出码**：Windows 进程退出码上限 255，
101127 落盘会被截断成 7。所以判据只看 JSON 字段 `exit_code`，不看 shell 的 `$LASTEXITCODE`。

中止标志由人创建与清除：

```bash
python scripts/ats.py abort --reason "人类主动中止"
python scripts/ats.py abort --clear
```

标志存在期间，除 `abort --clear` 外的所有 `ats.py` 命令都返回 101127。

---

## 规则 4：不要绕过门，要报告门

| 你的想法 | 正确做法 |
| --- | --- |
| 「加 `--skip-guard` 试试」 | 只允许人决定；AI 主动加等于绕过看门狗，禁止 |
| 「先 `seal` 一下把校验过了」 | 禁止：seal 要人类口令，且会重建信任基线 |
| 「改完代码 `patch --confirm` 一下就行」 | 禁止：`patch` 也要人类口令，且等于人承认新代码可信 |
| 「自己 `unpack --confirm` 进维护态，好跑测试」 | 禁止：维护态是人工开启的窗口，AI 无权自己开、也无权自己关 |
| 「`repack --confirm` 把基线刷了再跑」 | 禁止：等价于 `seal`，要人类口令且等于承认新代码可信 |
| 「`tryrun` 能跑命令，那就拿它当 `run` 用」 | 禁止：`tryrun` 只在维护态可用，且不是任务通道（无审批、无等级、无监督） |
| 「改 `policy.json` 把等级调低」 | 禁止：策略改动属人类决定 |
| 「审批单没批，我自己 `approve --all`」 | 禁止：代签 |
| 「这个任务卡住了，`delete` 重来」 | 先 `status`，把状态贴给人；`delete` 是 L3 |
| 「多步流程一条条 `run`」 | 用 `pack` 一次登记，或落 `ats_plan.py` 的 DAG |

---

## 规则 5：批处理优先（硬规则，不是偏好）

**触发判据**——命中任一行就必须走对应通道，不许在对话里逐条手搓：

| 你的处境 | 判据（可数） | 必须用 |
| --- | --- | --- |
| 多步骤 | 本轮要跑的命令 ≥ 2 条，且顺序由你决定 | `ats.py pack` |
| 多文件 | 同一操作涉及 ≥ 2 个文件，或文件名能被一个通配描述 | ATS 原生 glob |
| 有依赖 | 后一条要用前一条的结果，或失败要连带跳过下游 | `ats_plan.py` 的 spec |
| 要等结果 | 需要「跑完再看」 | `--wait` 或返回体的 `next_poll` |

### 5.1 指令包强制

`pack` 把逗号分隔的多条命令一次登记成多个 `pending` Task，**只写文本、不执行**：

```bash
python scripts/ats.py pack "npm run build,npm test,git status" --pack-id pk-build --timeout 600
```

- 判据：返回 `executed: false`、`count` 等于条数；`list --json --pack pk-build` 能筛出整包。
- 逐条推进：每条 Task 与普通 Task 同权（`run` / `check` / `stop` / `delete` 都可用）。
- 反例：同一轮里连发 5 条 `create` 再连发 5 条 `run`——人要点 5 次头，你也要贴 5 段 JSON。
- 不省审批：`pack` 只登记，等级照算（`--level L0` 也不会把 `Set-Content` 压到 L0），
  `run` 该走的门一条不少。`pack_empty` / `pack_empty_item` 是**整包都不落盘**，不留半成品。

### 5.2 通配符优先

ATS 自己会展开通配，你不用在上下文里列文件名。目前两处原生支持：

| 位置 | 写法 | 0 匹配 / 超限时 |
| --- | --- | --- |
| `download --file` | `--file "manifests/*.txt"`（可重复传多个） | `manifest_no_match` / `manifest_too_many`（默认上限 64，`ATS_DOWNLOAD_MANIFEST_MAX` 可调） |
| `@enc` 钩子参数 | `logs/*.log` | `hook_glob_no_match` / `hook_glob_too_many`（默认上限 256，`ATS_HOOK_GLOB_MAX` 可调） |

- 判据：你写的是**模式**，不是清单；钩子返回体里出现 `glob` 字段就说明确实走了展开。
- 反例：把 40 个 `.log` 路径逐个敲进命令——费 Token，且必有一个拼错。
- 反例：写 `*.log` 却指望它被当成字面文件名——0 匹配直接报错，不会静默跳过。
- 混写会被拒：`@enc` 的参数里同时出现通配与手写路径报 `hook_glob_ambiguous`，不猜你的意思。

### 5.3 执行链下沉

顺序、重试、失败传播写进 Plan spec，不要靠轮询 `check` 死等：

```bash
python scripts/ats_plan.py submit_plan --spec plan.json
```

- 顺序：`depends_on` 只在**同一 Stage 内**有效；自依赖 / 未知 id / 成环分别报
  `depends_on_self` / `depends_on_unknown:<id>` / `depends_on_cycle`。
- 失败传播：上游失败 → 下游 `skipped(dependency)` 并带 `blocked_by`，不需要你判断该不该跑。
- 重试：`retry` + `retry_backoff` 写在 Step 上，比你在上下文里「再试一次」可靠。
- 等待：`run --wait` 同步等；不等待时按返回体 `next_poll`（有任务在跑 5 秒、否则 0）决定下次查询；
  `check_schedule` 同样给 `next_poll`（到期阻塞时间 0，否则 `min(30, 距下次触发秒数)`）。
- 反例：每 30 秒 `check` 一次并把 JSON 贴进上下文——一轮长任务能烧掉几十轮 Token，看到的还是过期状态。

### 5.4 批量失败怎么定位

先读终端上方那块「战情看板」（stderr，最近 10 条回执 + `Line N`），
再用 `task.json` 的 `receive_line` 对照 `telegraph/Receive.txt` 找到挂掉的那条；
不要一上来就遍历所有子任务的 `stdout.log`。

### 5.5 提交前自检三问

任一答「是」就重来：

1. 我这轮要跑 ≥ 2 条命令，却还在逐条 `create`？
2. 我在上下文里手写了文件清单，而一个通配就能覆盖？
3. 我打算靠反复 `check` 等结果，而不是 `--wait` / `next_poll` / Plan spec？

---

## 一页速查（所有命令都在项目根目录执行）

「项目根目录」= 能看到 `scripts/` 与 `README.md` 的那一层，与仓库克隆到哪个路径无关；
下面所有命令都是相对路径，换机器、换盘符都不用改。

```bash
python scripts/ats.py status <task_id>              # 看状态，只读
python scripts/ats.py list                          # 列任务
python scripts/ats.py approve <approval_id>         # 人执行（要口令）
python scripts/ats.py run <task_id> --approval <id> # 批准后继续
python scripts/ats.py visa issue --task <id> --number 1 --time 5   # 人执行（要口令）
python scripts/ats_guard.py verify                  # 只读体检
python scripts/ats_guard.py seal --confirm          # 人执行（要口令）
python scripts/ats_guard.py patch --file ats_download.py           # 只读：看 Diff
python scripts/ats_guard.py patch --file ats_download.py --confirm # 人执行（要口令）
python scripts/ats.py abort --reason "..."          # 人执行：中止 AI
python scripts/ats.py abort --clear                 # 人执行：解除中止
python scripts/ats.py unpack --confirm              # 人执行（要口令）：停机进维护态
python scripts/ats.py tryrun -- "命令"              # 维护态内自测（窗口外拒绝）
python scripts/ats.py repack                        # 维护态内：只出 Diff
python scripts/ats.py repack --confirm              # 人执行（要口令）：重建基线
python scripts/ats.py unpack --exit                 # 人执行：退出维护态（不重封签）
```

配套文件：

- `command-human.md` —— 给人看的防呆自救指南（零术语）
- `references/error-taxonomy.md` —— 错误码全集与判据
- `references/permissions.md` —— L0-L3 与审批语义
