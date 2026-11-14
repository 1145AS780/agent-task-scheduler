# 变更日志 — agent-task-scheduler

版本号遵循 `vA.B.C-Rx` 规范：`A` 安全与架构级、`B` 核心功能新增、`C` 修补、`Rx` 架构重置计数。
进位条件、发版纪律与 19 处版本号落点见 [references/versioning.md](references/versioning.md)。

本文件与 `SKILL.md` §15「版本历史」同源，**改版本时两处必须同步**，否则使用者从
GitHub Releases 和从 SKILL.md 会读到两套说法。

---

## 3.2.0-R0 — 2026-11-14

> ⚠️ **升级必读：拉取本版本后必须重新封签，否则看门狗会拒绝执行。**
>
> 本次改动了 `scripts/` 下的 `.py`，而 `ats_guard.py` 的 `core_files()` 取的是
> `scripts/` 下**全部** `.py`，任何一个变动都会让 `ats_guard_manifest.json` 失配，
> `ats.py` 启动时的准入握手随即失败。
>
> 重建基线的方式：在 `OCSKIS/` 目录下运行 `setup.bat`，输入与上次相同的口令
> （≥ 8 字符，屏幕上不显示是正常的）。口令只经 stdin 传递，不进 argv、不进进程列表、
> 不进命令历史。若上次的密钥已丢失，`init` 会因清单存在而拒绝重建——必须手动删除
> `.ats/ats_guard_key.json` 与 `.ats/ats_guard_manifest.json` 两个文件后重跑，
> 没有自动化重置入口（这是设计，不是缺陷）。
>
> 完成后的自检：`python scripts/ats_guard.py verify`，应返回 `"ok": true`、
> `"problems": []`。

### 新增

- **`extend` 扩展插件层**：插件是 `<ATS_ROOT>/extend/<extend_id>/` 下的自包含目录，
  用 `instruction.json` 声明入口与指令。`scripts/ats_extend.py` 的发现层**只扫描、
  不导入、不执行**；`extend run` 把命令交给普通 Task 全链路（权限门、预检、审批、日志、`stop`），
  不给插件开第二条执行路径。子命令 `list` / `help` / `check`（纯预演）/ `run`。
  四条约束：`gate_text` 与 `run` 的 `command` 逐字同源；`shell=false`，`--arg` 进 argv 不进 shell；
  等级不由插件决定（`EXTEND_LEVEL_FLOOR = "L2"`，低于 L2 抬到 L2，缺失或非法按 L3）；
  审批绑定入口文件内容，入口一改即 `mismatch`。
- **`scripts/ats_preflight.py` 静态预检**：入口绝对路径会进 `gate_text`，所以插件源码
  在预演阶段就会被扫到，而不是等执行时才检查。
- **`setup.ps1` 一键安装**：口令经 stdin 传入，不进 argv、不进进程列表、不进命令历史；
  密钥不存在时先 `init` 再 `seal`；失败按 `passphrase_too_short` / `passphrase_mismatch`
  给出可复制的下一步。
- **`torrent` 自动注入公共 Tracker**：`scripts/ats_torrent.py` 常量区新增
  `DEFAULT_TRACKERS`（138 条 udp / http / https 公共节点），`start_aria2` 构建 argv 时
  动态追加 `--bt-tracker=<列表>`。磁力链与 `.torrent` 共用同一参数——前者靠它取元数据，
  后者靠它找 peer。
- **`ATS_TRACKERS` 覆盖开关**：环境变量优先级高于内置表；设成空串表示显式关闭注入。
- **`sanitize_trackers` 统一清洗**：处理换行 / 制表 / 反引号 / 引号 / 多余空格 / 空项 / 重复项，
  只保留 `udp://` `http://` `https://`（`wss://` 等 aria2c 不支持的 scheme 静默丢弃），
  缺路径的地址自动补 `/announce`。
- **`aria2_start` 事件新增字段**：`trackers`（生效条数）与 `tracker_source`
  （`default` / `env:ATS_TRACKERS` / `env:ATS_TRACKERS(disabled)`）。变量里一条合法地址
  都没有时输出 `tracker_warning`，而不是崩溃。
- **[references/versioning.md](references/versioning.md)**：版本号规范入库，含进位条件、
  发布纪律与 19 处版本号落点清单。
- **[CHANGELOG.md](CHANGELOG.md)**：本文件，此前版本历史只写在 `SKILL.md` §15。

### 变更

- **`--root` 默认值口径全仓统一**：核心脚本（`ats.py` / `ats_plan.py` / `ats_guard.py`）
  默认 `<ats 本体目录>/.ats`（即 `OCSKIS/.ats`）；`ats_ui.py` 是唯一例外，默认 `<cwd>/.ats`
  并显式传给子进程，以保证 UI 与子进程 root 一致。文档与 `--help` 文本全部对齐，
  不再出现「文档写一个、代码跑另一个」。
- **版本号格式改用 `vA.B.C-Rx`**：全仓 19 处落点（`SKILL.md` 3 处、`references/*.md` 9 份首行、
  `ats.py` / `ats_plan.py` / `ats_ui.py` / `ats_torrent.py` 共 6 处）统一到 `3.2.0-R0`。
  旧 tag（`v3.1.1` 及更早）保持原样，不回改历史。
- **错误返回体批量补齐可复制 `hint`**，并由 `tests/lint.py` 第 5 项
  `check_actionable_error` 强制——字面量 `print_json({"error": ...})` 缺 `hint` / `message`
  直接判失败。这条规则是为了避免「只报错不给出路」：使用者看到 `guard_check_failed`
  却不知道下一步该做什么，等于把问题丢回给用户。

### 修复

- **`ats_guard.py` 的 `resolve_root()` 根路径**：从 `Path.cwd() / ".ats"` 改为
  `SCRIPT_DIR.parent / ".ats"`。修的是「人手从别的目录敲 `ats_guard.py` 时，清单被写进
  错误根、人却以为修好了」这个隐患。执行器→看门狗这条链路始终显式传 `--root`
  （见 `ats.py` 的 `_run_guard`），从未依赖 cwd 默认值，因此这不是「权限门被绕过」的补丁。
- **`ats.py` 的 `ats_root()`** 同口径修正。
- **`ats.py` 残留的陈旧版本号**：docstring 与 `build_parser` 的 `description` 两处
  仍写着 `(v2.7)`，本次一并改为 `(v3.2.0-R0)`。
- **版本历史引用不存在的小节**：原条目引用「§10.1 防呆设计原则」，该小节在文档中并不存在，
  已替换为实际落地的 lint 第 5 项规则与 `--root` 口径统一内容。

### 发版验证

- `tests/lint.py`：13 个文件通过静态检查，退出码 0
- `tests/selftest.py`：**68/68** 通过，退出码 0（全部在独立临时 `ATS_ROOT` 沙箱运行，
  不污染仓库运行态）
- 信任基线：`ats_guard.py verify` → `"ok": true`、`checked: 10`、`problems: []`

---

## 3.1.1

`download --file` 支持**通配与多份清单**：`--file` 改为可重复，值里含 `*` `?` `[` 时由
`ats.py` 用 `glob` 展开（仍不经 shell），展开结果按字典序并写入 `tasks/<id>/download.json`
的 `manifests` 字段；0 匹配报 `manifest_no_match:<pattern>`（不再把字面量当文件名）、
超 64 份报 `manifest_too_many:<pattern>`（`ATS_DOWNLOAD_MANIFEST_MAX` 可调）；多份清单
`outputs` 不一致且未写 `--out` 时报 `manifest_outputs_conflict:<path>`（写了 `--out`
以它为准，不猜）；清单类错误统一追加 `:<路径或模式>` 便于定位；`--file` 只读清单故
**不跳过符号链接**（与 `@enc` 的差别已写明），多个通配值可并存（每份清单是独立输入）；
新增 3 条用例（`download_manifest_glob_merges` / `download_manifest_glob_no_match` /
`download_manifest_outputs_conflict`，全量 52 条）

## 3.1.0

`@enc` 钩子支持**通配路径**（一条任务覆盖多个文件）：钩子不经 shell、`*` 不会自动展开，
改由 `ats.py` 用 `glob` 按任务 `cwd` 展开；只展开位置参数里的 `*` `?` `[`，flag 及其值不动
（`--include *.log` 原样传给转换器）；两个通配或通配与手写路径混写报 `hook_glob_ambiguous`，
0 匹配报 `hook_glob_no_match`（不再把字面量当文件名交给转换器，避免凭空造文件），
超 256 个报 `hook_glob_too_many`（`ATS_HOOK_GLOB_MAX` 可调），符号链接跳过并计数；
展开后按字典序**逐条调用**转换器、遇非 0 即停，`post_hook` 新增
`glob`/`expanded`/`skipped_symlinks`/`runs`/`failed_on`，`hook-enc.log` 头部写展开清单可事后核对；
`timeout` 为每次调用超时；同时修正参数角色判定：以 `-` 开头的 token 永不被当成「值」，
否则 `--dry-run --from gbk --to utf-8 logs/*.log` 会把 `--from` 记成值、`gbk` 记成位置参数
而误报 `hook_glob_ambiguous`；新增 5 条用例（`hook_glob_expands_multiple_files` /
`hook_glob_no_match_fails_loudly` / `hook_glob_rejects_mixed_paths` /
`hook_glob_skips_flag_values` / `hook_glob_survives_valueless_flags`，全量 49 条）

## 3.0.1

修复 `@enc` 钩子绕过权限门（安全）：修复前钩子参数直接拼进 `shell=True` 的命令串执行，
`echo hi @enc x; del f` 会按 `echo hi` 登记成 L0 直通，钩子段却执行任意 shell；现在钩子参数
按空白分词（引号包裹段保持完整）后以 **argv 列表**交给转换器、不经 shell，且等级推导 /
预检 / 审批绑定统一改用「主命令 + `@enc` 参数」的完整原文（`hook_gate_text`），同一命令实测
被抬到 L2/L3 并命中 `denied_injection`；同时修正 `artifact_digests` 把钩子参数里的 `.py`
当执行体绑定、导致转码目标一改就误报 `artifact_changed` 的问题（现在只扫 `@enc` 之前的主命令）；
新增 3 条用例（`hook_args_do_not_run_through_shell` / `hook_args_enter_gate_text` /
`hook_target_not_bound_as_artifact`）；同时修正看门狗「密钥丢失」的下一步指引：`verify` 新增
`next_action: restore_key`（`seal` 在无密钥时必然 `key_missing`、`init` 又因清单存在而
`already_initialized`，原来只提示「重封签」是条跑不通的路），`ats.py` 准入失败提示同步改写；
新增 2 条用例（`guard_key_loss_points_to_restore` / `guard_manifest_loss_guides_to_seal`）；
并给 Plan 层补上钩子边界：Step 走 `shell=True` 直接执行、没有钩子后置，原来写 `@enc`
会被静默当普通参数传过去（实测该 Step 还报 `succeeded`，转换根本没发生），现在
`submit_plan` / `submit_schedule` 校验阶段报 `stage[..].step[..].hook_not_supported`
并整单拒绝；新增 1 条用例（`plan_step_rejects_hook`）；另补 1 条用例锁定「钩子段编码载荷也解码」
（`hook_args_decoded_payload_raises_level`，实测钩子参数里写
`powershell -EncodedCommand <b64>` 会被解出 `Remove-Item` 并抬到 L3，全量 44 条）

## 3.0.0

新增独立看门狗 `scripts/ats_guard.py`（零依赖、零 ctypes、零临时握手文件，只用子进程 +
stdout JSON 与退出码通信）：`ats.py` 每次启动先做准入握手，比对核心文件 SHA256 与
`ats_guard_manifest.json`，不符即退出、除 `approve` 外的子命令都不执行（`approve` 放行
以免自锁，仍要口令）；`ats.py` 同时自验 `ats_guard.py` 摘要，不把信任完全委托给看门狗；
子命令 `verify` / `init` / `seal` / `auth` / `self`；PBKDF2-HMAC-SHA256 口令闸门
（`ats_guard_key.json` 只存盐与摘要、密钥文件摘要入清单、`init` 在清单或密钥任一已存在时拒绝、
无重置入口），`approve` / `policy --set` / `init` / `seal` 一律要口令且只从
`--passphrase-stdin` 读（绝不进 argv）；运行期持核心脚本只读句柄并冻结 `policy.json`
（跨进程删除/替换被阻塞，同进程不持句柄以免 `os.replace` 自锁）；`--skip-guard` 仅限人类
紧急调试，强制 `__ats_guard_skip__` 的 L3 审批、`guard_skip_level` 只允许调高、跳过不降低
执行等级；`ats_plan.py` 同步启动准入；新增 16 条看门狗与口令闸门用例（全量 37 条）；
`tests/lint.py` 改用内存 `compile()` 避免 pyc 落盘触发杀软误报

## 2.9.1

新增 MCP 适配与本地控制台：`scripts/ats_mcp.py`（stdio，客户端拉起，16 个工具，协议
`2025-06-18` / `2025-03-26` / `2024-11-05`）、`scripts/ats_mcp_http.py`（控制台进程内的
Streamable HTTP 端点，默认关闭，`--mcp-port` 默认控制台端口 + 1，`confirm:true` 才允许开启，
只绑 `127.0.0.1` 并校验 Host / Origin）、`scripts/ats_ui.py` 本地 Web 控制台
（`--ensure` / `--status` / `--stop`）与 `scripts/ats_ui.cmd` 一键启动（修正为指向 `ats_ui.py`）

## 2.8

新增「存活租约免审」`lease_autonomy`：`policy.json` / `ATS_LEASE_AUTONOMY` 配置 `enabled` /
`max_level` / `rules` / `deny_rules`，`lease_file` / `ATS_LEASE_FILE` 指定租约路径（默认
`<root>/../temp/py_lease.json`，不存在则回落 `<root>/../.autopilot/temp/py_lease.json`）；
`permission_gate` 与 `gate_operation` 在身份判定之后、审批之前插入该路径，放行写
`status=approved` 的留痕审批（`auto_approved=true`、`resolved_by=py_lease:<令牌>`）不投待办；
判定为五道关（总闸 / 等级上限 / 租约存活 / 命令白名单 / 租约自带约束），任一不满足退回审批，
坏配置一律回落关闭；`policy` 命令新增 `lease` 段报告租约状态；`py_consent.py` 第 4 题把
授予范围变成用户可勾选项

## 2.7

新增 `@enc` 后缀钩子：命令文本末尾追加 `@enc <转换器参数>` 即把编码转换登记为任务的
`post_hook`，主命令退出码为 0 才执行（转换器 `tools/encoding_converter.py`，可用
`ATS_ENCODER_PATH` 覆盖）；钩子失败则任务 `failed` 且 `exit_code` 取钩子码，日志
`hook-enc.log`、事件 `task_post_hook`；`create` / `pack` 均支持，坏语法报 `bad_hook` /
`bad_hook_item`；转换器仍可独立 CLI 使用

## 2.6

新增「关键任务失败即停」：Step 级 `critical: true` + Plan 级 `fail_on_critical: "stop"`，
或 `run_plan` / `resume` 加 `--fail-fast`（临时视全部 Step 为 critical）；判定为**重试耗尽后
仍失败**（`failed` / `timeout`），触发后立即 `taskkill /T /F` 终止同批兄弟进程树
（`step.json` 记 `aborted_by="fail_fast"` + `aborted_at`，事件 `task_aborted`），当前 Stage
剩余 Step 标 `skipped(critical_failed)`、后续 Stage 标 `skipped`、`plan.status=failed`；
修复 Plan 级 `fail_on_critical` 未写入 `plan.json` 与停止路径 `plan_end` 状态残留 `running`

## 2.5

新增 `ats.py pack`「指令包」：一次登记多条命令（英文逗号分隔，支持 `\,` / `\\` 转义），
各建独立 `pending` Task（`kind="pack"`、共享 `pack_id`、`pack` 元数据），**只传文本不执行**；
`list --pack` 按包筛选；空包 / 空项分别报 `pack_empty` / `pack_empty_item` 且整包不落盘

## 2.4

新增 `ats.py torrent` + `scripts/ats_torrent.py`：磁力链 / `.torrent`（aria2c JSON-RPC），
`*.aria2` 断点续传、做种时长、上下行限速、磁力 `followedBy` 自动换 gid；`kind="torrent"`
复用 Task 全链路；supervisor 分离启动新增 WMI 兜底（`task.json` 的 `detach_mode`）

## 2.3

新增 `ats.py download` + `scripts/ats_download.py`：HTTP(S) 单文件 / 批量清单
（`.json` / `.txt` / `.csv`）、Range 断点续传（`.part`）、逐条重试与线性退避、大小与 SHA-256
校验、并发线程池、全局字节限速、代理 / 自定义 Header / 跳过证书校验；下载任务复用 Task
权限门、审批、事件、日志与 `check`/`stop`；重复条目自动合并（`deduped`），不同 URL 撞同名
文件报 `name_conflict`；Task 支持原生 `argv` 派生（不经 shell）

## 2.2

修复与文档同步：`waves` 计数（收尾空轮不计、暂停轮计入、跨 `pause → resume` 累加不归零、
`denied`+stop 路径落盘）；`stage_done`/`stage_failed` 事件携带 `waves`；`approve` 单条失败
给出可判别退出码（`insufficient_level`→4、其余→3，`--all` 恒 0）；`next_run_iso` 不再把
`start_at: 0` 显示成 1970；示例脚本注入 `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` 防 GBK 崩溃

## 2.1

审批闭环（有效期/批量/事件投递/自动重发）、白名单放行、Stage 内 `depends_on`、Step 自动重试
与退避、`plan_stalled` 停滞检测、运行快照与 `history`/`prune`、`scheduler.log` 与
`events`/`logs` 命令、`steps_recovered` 中断恢复

## 2.0

三层模型（Task / Plan / Schedule）、L0-L3 权限、审批文件、`events.jsonl`、Stage 串并行

---

更早的历史（1.x）未在版本历史表中保留，本文件不补造。