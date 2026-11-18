# ATS 可用功能简述

> **快照日期**：2026-11-15
> **这是什么**：一页看完「现在能用什么」，不写计划、不写待办。
> 想看「还缺什么」去 [pancake.md](pancake.md)，想看「怎么用」去 [SKILL.md](SKILL.md)。

一句话：把「跑一条命令」升级成「可审批、可编排、可远程驱动、可事后追溯」的常驻任务通道；
零第三方依赖，全部状态落在一个 `ATS_ROOT` 目录里，不引入第二套数据源。

---

## 1. 能力一览

入口均在 `scripts/` 下，命令统一输出 JSON，失败体带 `hint` 或 `message`。

| 能力 | 入口 | 一句话说明 |
| --- | --- | --- |
| 单条任务 | `ats.py create` / `run` / `check` / `show` / `stop` / `delete` / `list` / `status` | 后台跑长命令，不占对话轮次；超时、退出码、日志尾部都能查 |
| 指令包 | `ats.py pack` + `list --pack <pack_id>` | 多条命令一次性登记为独立 Task，**只登记不执行**；失败时逐条定位 |
| 一次性编排 | `ats_plan.py submit_plan` / `run_plan` / `resume` / `rerun_task` / `rerun_plan` | Stage→Step 的 DAG，依赖失败自动跳过下游；支持重试与断点续跑 |
| 周期任务 | `ats_plan.py submit_schedule` / `check_schedule` / `pause` / `cancel` / `resume_schedule` | 靠 Agent 轮询驱动（精度取决于轮询间隔），带停滞检测 |
| 内置下载 | `ats.py download` | 断点续传、大小 / SHA-256 校验、并发与全局限速、多清单通配 |
| 种子下载 | `ats.py torrent` | 磁力链 / `.torrent`，依赖本机 aria2c；限速与做种时长可设 |
| 权限与审批 | `ats.py policy` / `approve` / `approval prune` | L0-L3 分级 + 正则规则；高风险命令返回 `approval_id` 等人点头 |
| 签证（免逐条审批） | `ats.py visa issue` / `list` / `peek` / `revoke` / `consume` | 一次签发限定配额与时限，绑定命令哈希或 Plan 指纹 |
| 看门狗 | `ats_guard.py init` / `seal` / `verify` / `auth` / `patch` | 核心脚本 SHA-256 基线 + 口令；口令只走 stdin，绝不进 argv |
| 维护态 | `ats.py unpack` / `tryrun` / `repack` | 改核心脚本时的停机窗口：停任务、窗口内自测不限次、重建基线 |
| 任务回执板 | 执行命令的 stderr 看板 + `telegraph/Receive.txt` | 每条终态一行 `[<task_id>]: true\|false`，行号即 `task.json` 的 `receive_line` |
| 扩展插件 | `ats.py extend list` / `help` / `check` / `run` | 自己的小工具放进 `extend/<id>/`，`check` 预演后再走同一套审批 |
| 本地控制台 | `ats_ui.py --open`（或 `--ensure`） | 只读监控 + 运行/停止/重试 + 审批 + 签证 + 路径白名单 + 窄屏适配 |
| MCP 接入 | `ats_mcp.py`（stdio）/ 控制台内 HTTP 端点 | 16 个工具覆盖 Task / 审批 / Plan / Schedule；HTTP 端点默认关闭 |
| 事件与追溯 | `ats.py events` / `logs`、`ats_plan.py history` | 全局事件流带游标；运行快照不可变，可回看每步退出码 |
| 转码钩子 | 命令末尾挂 `@enc` | 仅主命令成功时自动转码；失败 / 超时 / 被 stop 时不生成 |

**计数**（供核对，改动脚本后请重核）：`ats.py` 23 个子命令、`ats_plan.py` 15 个、
`ats_guard.py` 7 个动作、MCP 16 个工具。

---

## 2. 三条边界（不是功能限制，是别踩错）

- **不代批、不代签**：控制台与 MCP 都不替你审批，`approve` / `seal` 必须由持口令的人执行。
- **身份等级是声明式配置**，不是安全边界；需要不可篡改身份鉴权的场景不适用。
- **回执板只追加、不轮转不清理**：人工截断 `telegraph/Receive.txt` 会让既有
  `receive_line` 全部指错行。

---

## 3. 不适用

- 毫秒级、必须在同一事务内完成的原子操作。
- 需要真实分布式锁、多机协调的场景（本技能只做单机文件锁语义）。
- 需要秒级精确定时（靠 Agent 轮询驱动，精度取决于轮询间隔）。
- 需要不可篡改身份鉴权的场景（身份来自配置 / 环境变量，属声明式而非安全边界）。

---

## 4. 往下读哪里

| 想知道 | 去哪 |
| --- | --- |
| 60 秒上手、命令逐条说明 | [SKILL.md](SKILL.md) §3 上手 / §4 命令总表 |
| 权限模型、审批闭环、签证语义 | [SKILL.md](SKILL.md) §5 |
| 编排 spec（Plan / Stage / Step / Schedule） | [SKILL.md](SKILL.md) §6、[orchestration-spec.md](references/orchestration-spec.md) |
| 目录结构与每个文件的写者读者 | [storage-layout.md](references/storage-layout.md) |
| 状态机与字段 | [state-machine.md](references/state-machine.md)、[data-model.md](references/data-model.md) |
| 错误码与处置 | [error-taxonomy.md](references/error-taxonomy.md) |
| MCP 接入（stdio / HTTP） | [SKILL.md](SKILL.md) §12 |
| 路线图与已完成项 | [pancake.md](pancake.md) |