# 后台任务调度器

<p align="center">
  <img src=".github/assets/banner.svg" alt="后台任务调度器" width="100%">
</p>

[English](README.md) | **中文**

面向 AI Agent 的纯本地、无守护进程任务调度器。它把「命令」分成三层实体，用文件系统作为唯一状态源，由 Agent 在轮次之间主动推进。

- 不依赖外部服务、不依赖第三方库，仅用 Python 3 标准库
- 所有状态落在 `BTS_ROOT`（默认 `<cwd>/.bts`）：纯文本，可手改、可版本控制、可异地备份
- 每个命令都以 JSON 输出，便于 Agent 解析；退出码语义稳定
- 零后台进程。没人让它跑，它就不跑

## 什么时候用

**适用**

- 耗时命令不想占住对话轮次：训练、构建、批量转码、长时间测试
- 按固定节奏反复跑同一套命令：巡检、轮询、定时抓取
- 多条命令有先后关系，其中一条失败后下游应自动跳过
- 失败可能只是偶发（网络抖动、锁竞争），需要自动重试与退避
- 高风险命令（删除、发布、动生产数据）需要人工点头才允许执行
- 事后回看「这次跑到哪一步、每步退出码是多少」
- 批量下载、断点续传、按限速抓取；磁力链与种子文件走 aria2c

**不适用**

- 毫秒级、必须在同一事务内完成的原子操作
- 真实分布式锁、多机协调——这里只有单机文件锁语义
- 秒级精确定时——靠 Agent 轮询驱动，精度取决于轮询间隔
- 不可篡改的身份鉴权——身份来自配置或环境变量，属声明式而非安全边界

## 三层模型

| 层 | 载体 | 职责 | 生命长度 |
| --- | --- | --- | --- |
| Task | `tasks/<task_id>/` | 单条命令 + 权限 + 超时 | 一次执行 |
| Plan | `schedules/<sid>/plans/<pid>/` | 一次编排：多个 Stage，每个 Stage 多个 Step | 一次编排 |
| Schedule | `schedules/<sid>/schedule.json` | 周期或一次性触发 Plan | 长期 |

Stage 内的 Step 可以有单层 `depends_on` 依赖。跨 Stage 的先后关系用 Stage 切分表达：前一个 Stage 全部结束才进下一个。

```
Schedule（周期/一次性）
  └─ Plan（第 N 次尝试，id = <sid>-a<N>）
       ├─ Stage s1（serial | parallel）
       │    ├─ Step one  ──→ 可 depends_on 同 Stage 内其它 Step
       │    ├─ Step two
       │    └─ Step three
       └─ Stage s2
```

每个 Step 本质上是「带权限门的 Task」，直接复用 Task 层的审批、事件与日志能力。

<p align="center">
  <img src="assets/bts-architecture.svg" alt="三层实体模型与权限闸门安检链" width="100%">
</p>

## 环境要求

- Python 3（仅标准库）
- `aria2c` 在 `PATH` 中——仅当需要种子/磁力下载时

## 快速上手

```bash
# 创建任务（权限等级默认 L0）
python scripts/bts.py create build --command "gradle build" --level L1

# 运行并等待
python scripts/bts.py run build --wait

# 查看
python scripts/bts.py status build --json
python scripts/bts.py show build --lines 50
```

编排、调度与下载：

```bash
python scripts/bts_plan.py create --spec references/examples/plan-deps-retry.json
python scripts/bts_plan.py run <plan-id> --wait
python scripts/bts.py download --url https://example.com/file.bin --out ./dl --wait
python scripts/bts.py torrent --magnet "magnet:?xt=urn:btih:..." --out ./dl --wait
```

## 权限分级

| 等级 | 含义 | 是否需审批 |
| --- | --- | --- |
| L0 | 只读与无害操作 | 从不 |
| L1 | 本地写入、构建 | 取决于策略 |
| L2 | 网络、装包 | 通常需要 |
| L3 | 删除、发布、生产数据 | 总是需要 |

审批带有效期，支持批量批准，并支持白名单放行。高风险命令没有有效审批时以退出码 4 拒绝执行。

退出码：`0` 正常，`2` 参数或确认缺失，`3` 不存在或状态冲突，`4` 权限不足。

## 仓库结构

```
SKILL.md                    入口：触发条件与完整工作流
references/                 按需加载的规格文档
├── orchestration-spec.md   Stage、Step、依赖与失败传播
├── permissions.md          L0-L3 分级、审批流、白名单
├── data-model.md           task/plan/schedule 的全部字段
├── storage-layout.md       BTS_ROOT 下的目录树
├── state-machine.md        状态迁移与终态
├── error-taxonomy.md       错误分类与重试矩阵
├── command-guide.md        命令书写与调用规范
├── anti-patterns.md        常见错误用法及原因
└── examples/               可运行的最小样例
scripts/                    调度器本体
├── bts.py                  Task 层：创建、运行、下载、种子、审批
├── bts_plan.py             Plan 与 Schedule 层
├── bts_download.py         HTTP 下载器，断点续传与完整性校验
└── bts_torrent.py          种子与磁力的 aria2c 封装
```

## 目录约定

- `SKILL.md` 是唯一入口；YAML 的 `description` 决定触发时机，正文负责路由到细分文档
- `references/` 用复数且平铺，主题文件直接放在其下，不再嵌套层级
- `scripts/` 只放可执行件，不放说明文字；脚本自行解析路径，不硬编码绝对路径
- 样例放在 `references/examples/`，因为样例属文档而非工具
- 不提交构建产物：`__pycache__/`、`.bts/`、日志与缓存

## 说明

- 调度器不往 `BTS_ROOT` 之外写任何东西，删除该目录即重置全部状态。
- 状态刻意用纯文件：不需要专用工具就能查看、编辑、diff 与备份。
- 文档中的路径一律用 `<cwd>` 这类占位符，不对应任何具体工程。

## 许可

本项目基于 [MIT 许可](LICENSE) 发布，可自由使用、修改与再分发，保留版权声明即可。
