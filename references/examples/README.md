# examples — 可直接运行的最小样例

本目录是「复制即可用」的样例集。所有 JSON 都经过校验，所有命令都可直接跑（不依赖 npm / pytest，用 `echo` / `python -c` 保证跨平台可执行）。

目录定位：`<skill_root>/references/examples/`，脚本在 `<skill_root>/scripts/`。

---

## 0. 前置

```powershell
# 进入技能根目录（examples 的上两级）
cd <skills-root>/agent-task-scheduler

# 用独立的状态目录，避免污染项目里的 .ats
$env:ATS_ROOT = "$PWD\.ats-examples"

python scripts\ats.py policy --json
```

如果你在项目里使用，直接用 `--root`：

```powershell
python <skill>\scripts\ats.py --root D:\work\.ats run build1
```

---

## 1. 文件清单

| 文件 | 类型 | 演示什么 |
| --- | --- | --- |
| [task-spec.json](task-spec.json) | Task spec | `create --spec` 的完整字段（env / cwd / timeout / level） |
| [plan-oneshot.json](plan-oneshot.json) | Plan spec | 两个 Stage：serial 检查 + parallel 测试 |
| [plan-deps-retry.json](plan-deps-retry.json) | Plan spec | 同 Stage 内 `depends_on` + 失败传播 + `retry` + 退避 |
| [plan-approval-gated.json](plan-approval-gated.json) | Plan spec | L2 / L3 步骤触发审批暂停与恢复 |
| [plan-patrol.json](plan-patrol.json) | Plan spec | 供 recurring schedule 复用的巡检计划 |
| [schedule-recurring.json](schedule-recurring.json) | Schedule spec | `every` + `max_runs` + `start_at` |
| [download-spec.json](download-spec.json) | 下载清单 | `download --file` 的完整字段（并发 / 限速 / 重试 / 条目级 `headers`） |
| [policy-sample.json](policy-sample.json) | 策略 | `policy.json` 六个键的推荐写法 |
| [run-examples.ps1](run-examples.ps1) | 脚本 | 端到端走一遍：任务 → 计划 → 审批 → 周期 |
| [parse-result.py](parse-result.py) | 脚本 | 正确解析 JSON 与退出码，按 `status` 分类 |
| [wait-plan.py](wait-plan.py) | 脚本 | 按 `next_poll` 轮询 Plan 直到终态 |

---

## 2. 单条任务

```powershell
python scripts\ats.py create build1 --spec reference\examples\task-spec.json
python scripts\ats.py run build1
python scripts\ats.py check build1
python scripts\ats.py show build1 --lines 20
```

`task-spec.json` 里 `id` 与命令行参数重复时，**命令行优先**（`--spec` 的 `id` 只在没给位置参数时生效）。

---

## 3. 一次性计划

```powershell
python scripts\ats_plan.py submit_plan --spec reference\examples\plan-oneshot.json
python scripts\ats_plan.py run_plan ci-lint-test-a1
python scripts\ats_plan.py history ci-lint-test
```

预期：`status=done`，`counts` 里 3 个 `succeeded`。

---

## 4. 依赖与重试

```powershell
python scripts\ats_plan.py submit_plan --spec reference\examples\plan-deps-retry.json
python scripts\ats_plan.py run_plan dep-retry-a1
python scripts\ats_plan.py show dep-retry-a1
```

预期：

| step | 预期状态 | 原因 |
| --- | --- | --- |
| `flaky` | `failed` | 故意 `sys.exit(3)`，重试 2 次后仍失败（`attempts=3`） |
| `downstream` | `skipped` | 一级传播：`depends_on: "flaky"`，`blocked_by=flaky`、`dependency_status=failed` |
| `deep` | `skipped` | 二级传播：`depends_on: "downstream"`，`blocked_by=downstream`、`dependency_status=skipped` |
| `independent` | `succeeded` | 无依赖，照常跑 |

最终的 `counts` 应为 `{"failed": 1, "skipped": 2, "succeeded": 1}`。

`stages/fanout/stage.json` 应为 `{"status":"failed","waves":2}`：第 1 波执行 `flaky` 与 `independent`，第 2 波只做依赖跳过传播（把 `downstream`/`deep` 标 skipped），第 3 次进入时已无未终结 step，不计入。

看事件流验证重试：

```powershell
Get-Content .ats-examples\schedules\dep-retry\events.jsonl -Encoding UTF8 | Select-String "task_retry"
```

必须带 `-Encoding UTF8`：`events.jsonl` 是 UTF-8，PowerShell 5.1 的 `Get-Content` 默认按 ANSI 解码，中文会变乱码，甚至把 JSON 字符串截断导致 `ConvertFrom-Json` 报「未终止的字符串」。想完全避开编码问题，用 Python 读：

```powershell
python -c "import io;[print(l.rstrip()) for l in io.open(r'.ats-examples\schedules\dep-retry\events.jsonl', encoding='utf-8') if 'task_retry' in l]"
```

应能看到两条 `task_retry`（attempt=1、attempt=2），以及一条最终的 `task_failed`。

---

## 5. 审批

```powershell
# 1) 跑计划，遇到 L2 步骤会暂停
python scripts\ats_plan.py submit_plan --spec reference\examples\plan-approval-gated.json
python scripts\ats_plan.py run_plan gated-a1
# → status: paused，approval.id 存在

# 2) 批准：批准者的身份必须 ≥ 该审批要求的等级
#    deploy 是 L2 → 当前身份必须是 L2 或 L3
#    用 L1 批准 L2 审批会直接 insufficient_level（退出码 4），不是审批失败，而是身份不足
$env:ATS_ACTOR_LEVEL = "L3"      # 观察成功路径
python scripts\ats.py approve <approval_id>

# 3) 恢复
python scripts\ats_plan.py resume gated --approval <approval_id>
```

审批的「要求等级」由**被拦的步骤**决定（本样例 `deploy` 是 L2、`purge` 是 L3）。批准者身份来自 `ATS_ACTOR_LEVEL`（或 `policy.json` 的 `actor_level`），它必须 ≥ 该步骤等级。所以：

| 被拦步骤 | 需要的最低批准者身份 |
| --- | --- |
| L0 / L1 | L1（默认身份即可） |
| L2 | L2 |
| L3 | L3 |

如果你把 `ATS_ACTOR_LEVEL` 设成 `L3`，`auto` 档（默认 L0/L1）的步骤会直接放行；但 `ask` 档（默认 L2/L3）的步骤仍会暂停并生成审批，与 `actor_level` 无关。

恢复后 `stages/release/stage.json` 的 `waves` 应为 **3**：`deploy` 被拦那次已消耗第 1 波，`deploy` 自己占第 2 波，`purge` 占第 3 波。暂停不重置计数（暂停时 `waves` 已落盘）。

（若按 §7 复制了 `policy-sample.json`，`max_level=L2` 会让 `purge` 直接 `denied_policy` 而不是执行；`denied` 发生在 `waves += 1` 之后，波次仍是 3。）

```powershell
$env:ATS_ACTOR_LEVEL = "L0"    # 模拟受限身份，便于观察审批流程
Remove-Item Env:\ATS_ACTOR_LEVEL   # 观察完清掉
```

---

## 6. 周期计划

```powershell
python scripts\ats_plan.py submit_schedule --spec reference\examples\schedule-recurring.json
python scripts\ats_plan.py check_schedule patrol-sample   # 首次立即触发 a1（start_at=0 只影响首轮）
Start-Sleep -Seconds 6
python scripts\ats_plan.py check_schedule patrol-sample   # 等满 every 后才触发 a2
Start-Sleep -Seconds 6
python scripts\ats_plan.py check_schedule patrol-sample   # 再等满 every 后触发 a3 → status=done
```

预期：`every: 5`、`max_runs: 3`，三次触发后 `status=done`、`next_poll=0`。

`submit_schedule` 返回 `next_run: 0`、`next_run_iso: null`——这里的 `0` 是「已到期、可立即触发」，不是 1970 年，也不是「永不触发」（「永不触发」才是 `null`）。

`start_at: 0` 表示「**首轮**立即触发」，不要漏写——漏写时默认是 `now + every`，要等一整个周期。
`next_run` 在每次触发后被设为 `now + every`，所以**连续两次调用不会各触发一次**：

```
提交后立刻调   → triggered:["patrol-sample-a1"], runs=1, next_run=now+5
紧跟着再调一次 → triggered:[], runs=1, next_poll≈5      （距上次触发不足 every，空转一轮）
睡满 6 秒后调  → triggered:["patrol-sample-a2"], runs=2, next_run=now+5
再睡满 6 秒    → triggered:["patrol-sample-a3"], runs=3, status=done, next_poll=0
```

上面代码块里的三次 `check_schedule` 之间都有 `Start-Sleep -Seconds 6`，所以每次都真的触发（对应你实测的 poll 1/2/3 → a1/a2/a3）。「空转一轮」只发生在两次调用间隔小于 `every` 时。

`run-examples.ps1` 里睡的是 `Start-Sleep -Seconds (min(next_poll, 6))`——此处 `next_poll=5`，所以实际睡 5 秒，同样够触发；它的 poll 2/3 就对应上面「睡满后」的两行。

也就是说，`start_at` 只决定**第一轮**；从第二轮起，节奏完全由 `every` 决定。

---

## 7. 策略

复制到状态根目录：

```powershell
Copy-Item reference\examples\policy-sample.json .ats-examples\policy.json
python scripts\ats.py policy --json
```

注意：`policy.json` 只在**读取时**生效，无需重启任何东西。环境变量优先级更高，临时调试时可以直接覆盖：

```powershell
$env:ATS_MAX_LEVEL = "L2"
$env:ATS_STALL = "10"
```

样例策略把 `max_level` 设为 `L2`：因此 `plan-approval-gated.json` 里的 L3 `purge` 会返回 `denied_policy`（`stage_failed` + `error_type=policy_denied`）——这正是「策略上限不可被审批绕过」的现场演示。想看 L3 通过，把 `max_level` 改回 `L3`。

---

## 8. 辅助脚本

```powershell
# 端到端演示（会创建/运行/审批/清理）
powershell -ExecutionPolicy Bypass -File reference\examples\run-examples.ps1

# 单独解析一次调用结果
python reference\examples\parse-result.py run build1 --wait

# 轮询直到 plan 终态（自动按 next_poll 睡）
python reference\examples\wait-plan.py ci-lint-test-a1
```

三个脚本都以 `--root` / `ATS_ROOT` 为准，行为与手工命令完全一致。

---

## 9. 下载

`download-spec.json` 演示清单的全部常用字段。`outputs` 只在**没给 `--out`** 时生效。

```powershell
$root = ".ats-examples"

# 用清单（并发 2、限速 1 MiB/s）
python scripts\ats.py download --file reference\examples\download-spec.json --root $root

# 单文件 + 完整性校验（sha256/size 只允许单条目）
python scripts\ats.py download --url https://example.com/a.zip --out .ats-examples\dl `
        --sha256 <hex> --size 10485760 --header "Referer: https://example.com/"

# 调试：同步等完（退出码仍是 Task 语义，成败看 status）
python scripts\ats.py download --url https://example.com/a.zip --out .ats-examples\dl --wait
```

看结果：

```powershell
python scripts\ats.py check dl-1a2b3c
Get-Content ".ats-examples\tasks\dl-1a2b3c\stdout.log" -Encoding UTF8 | Select-Object -Last 1 | ConvertFrom-Json
```

最后一行是 `summary`：`total` / `succeeded` / `failed` / `bytes` / `elapsed` / `items`（逐条含 `attempts` 与 `error`）。

**中断后续传**：直接重跑同一命令即可——已完成的文件校验后跳过（`status: existing`），未完成的从 `<目标>.part` 续传（`resumed_from` 会出现在 `item_done` 里）。想强制重下加 `--overwrite`。

### 9.1 种子（磁力链 / `.torrent`）

`torrent` 需要 aria2c（`winget install --id aria2.aria2 -e`）。

```powershell
# 磁力链：下完即停（--seed-time 默认 0）
python scripts\ats.py torrent --magnet "<magnet:?xt=urn:btih:...>" --out .ats-examples\tor --root $root

# .torrent + 做种 5 分钟 + 上行限速 512 KiB/s
python scripts\ats.py torrent --torrent D:\dl\debian.torrent --out .ats-examples\tor `
        --seed-time 5 --max-upload 524288 --root $root
```

进度看 `stdout.log` 的 `torrent_progress`（约 5 秒一条）；`torrent_metadata` 表示磁力链元数据已拿到、gid 已切到真实内容。失败原因见 [error-taxonomy.md](../error-taxonomy.md) §4.4。

### 9.2 指令包（多条命令一次性登记，不执行）

```powershell
# 3 条命令 → 3 个 pending Task；只登记文本
python scripts\ats.py pack "echo A,echo B,echo C" --pack-id pk-demo --root $root

# 命令内部含英文逗号：写成 \,
python scripts\ats.py pack "echo a\,b,echo c" --pack-id pk-esc --root $root

# 按包查看；要跑哪条再单独 run
python scripts\ats.py list --pack pk-demo --root $root
python scripts\ats.py run <task_id> --root $root
```

返回 `{"pack_id", "count", "executed": false, "tasks": [...]}`——`executed: false` 是设计，不是失败。空包 / 空项分别报 `pack_empty` / `pack_empty_item`（exit 2），整包不落盘。

---

## 10. 清理

```powershell
# 看还剩什么
python scripts\ats.py list
python scripts\ats_plan.py list

# 清空样例状态（不影响项目其它 root）
Remove-Item -Recurse -Force .ats-examples
```

删除前确认没有 `running` 的任务；否则先 `stop`。
