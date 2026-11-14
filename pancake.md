# ATS (Agent Task Scheduler) 开发路线图

P0（执行范围扩展到「人类显式授权的目录集」）已交付，见文末「归档」。
P1 控制台升级 4 项（鉴权 / 路径白名单面板 / cwd 下拉 / 移动端适配）已全部交付，见文末「归档」。
下一段主线是 P2 里仍空着的三项能力（CLI 级异步等待、封包态热修改器、停机/封装生命周期）。

已完成项不再列出，见文末「归档」。

---

## P1: 控制台升级（与 P0 配套，否则白名单只能手改 JSON）

### 1.1 鉴权（已完成，见文末归档）

### 1.2 路径白名单面板（已完成，见文末归档）

### 1.3 任务创建表单的 cwd 改为白名单下拉（已完成，见文末归档）

### 1.4 移动端适配（已完成，见文末归档）

---

## P2: 仍待完成的能力（承接上一版 P0，未做）

### 2.1 CLI 级异步等待

上一版规划的 `ai_command.py` 的 `wait` / `run_and_wait` 已在 `api/ats_api.py` 以**库级**落地
（`wait_for_approval` / `wait_task` / `run_and_wait`，子进程调 CLI，不 import ats）。
仍缺的是 **CLI 级**同名命令：让不装 Python 库的调用方也能一条命令等到审批决议。

已知约束：`approval` 子命令只有 `prune`，没有 `list`（`ats.py:L3986-L3990`），
所以等待只能靠事件流里的 `approval_resolved`（`ats.py:L3002-L3021`）。
若要做 CLI 级等待，得先补一个可读的审批查询通道，否则只能靠事件尾巴，窗口一满就漏。

### 2.2 封包态热修改器

安全文件白名单，允许封签状态下改 `ats_download.py` / `ats_torrent.py` 等非高危文件，
自动生成 Diff 供人确认后只更新该文件局部哈希。
红线不变：`ats_guard.py`、密钥文件、`policy.json` 改动仍触发全面停机。

### 2.3 停机 / 封装生命周期

`unpack`（停任务、释放句柄、进维护态）、`tryrun`（维护态下不限测试）、
`pack`（Diff 报告 + 口令确认后重建基线）。

### 2.4 多语言插件真实闭环

跑通一次真实 Node.js 插件接入，验证非 Python 场景的哈希校验、拉起与审批链路。

---

## P3: 长期健康度审计（远期）

### 3.1 代码复杂度审计（`audit_complexity.py`）

标准库 `ast` 扫描单文件行数与函数嵌套深度，逼近临界值告警，触发 `vA.B.C-Rx` 的 R 级重置评审。

### 3.2 代码冗余扫描（`audit_redundancy.py`）

`difflib` 查高度相似代码块，强迫逻辑收敛。

### 3.3 高频覆写监控

巡检探针，监控核心目录是否被极短时间高频覆写，防御自动化脚本隐蔽篡改。

---

## 归档（已完成，不再列入计划）

- 静态审查强化：`lint.py` 已用 AST 校验 `print_json` 的 `hint` 字段；`TARGETS` 已扩到 `scripts` / `tests` / `api`，14 文件通过。
- **P1.1 控制台鉴权**：`ats_ui.py` 增 `--token-file`（`L1930`）；`resolve_token()`（`L87`）只从文件读令牌、
  文件缺失时自动生成 32 字节 `secrets.token_urlsafe` 并 `chmod 600`；`is_loopback()`（`L72`）判定
  `""` / `127.0.0.1` / `localhost` / `::1`，非回环地址且没给 `--token-file` 时 `host_guard_message()`（`L114`）
  打印「拒绝启动 + 可直接复制的补救命令」并返回码 2；`_authorized()`（`L1652`）用 `hmac.compare_digest`
  校验 `Authorization: Bearer`，`do_GET` 的 `/api/*` 与 `do_POST` 的 `/api/action` 共用同一闸门（`_deny()` 回 401 + `WWW-Authenticate`）。
  `/` 页面放行但下发 `{"auth": true, "auth_file": ...}`，前端据 `SRV.auth` 弹令牌输入框（`#authGate`），
  令牌存 `localStorage`、随所有 `fetch` 带 `Authorization` 头；401 时自动重新弹框而不是报「后端不可达」。
  回环默认模式不强迫配令牌（`TOKEN` 为空即免鉴权），保持本机体验不变。
  `tests/selftest.py` 增 `ui_refuses_public_bind_without_token` / `ui_token_gate` / `ui_loopback_without_token`，全量 92/92 通过。
- **P1.2 路径白名单面板（原路线图 P1.2 全节）**：审批面板新增「路径白名单」卡片
  （`ats_ui.py` 的 `#allowBody` / `#allowDirs` / `#btnSaveAllow`，`renderAllowDirs()` 与 `saveAllowDirs()`）。
  逐条并排显示「写了什么 / 认成什么」：为此给 `policy --json` 的 `allow_dirs_detail` 每项补 `raw`
  （`ats.py:L3388`，原字段只有 `path` / `exists` / `broader_than_root`，前端拿不到原始写法），
  `boundary_warnings` 用 `hint bad` 原样渲染而不是混进普通列表。编辑区一行一个绝对路径，
  提交走 `/api/action` 的 `policy.set`（`patch: {allow_dirs: [...]}`），口令经 stdin 交 CLI，
  UI 不代批、不持有权限判断。保存成功即 `ALLOW_SIG=''` 并 `refresh()`，15s 缓存失效保证面板立刻读到新值。
  `tests/selftest.py` 增 `ui_allow_dirs_panel`：经真实 HTTP 写盘后校验 `policy.json` 落盘值、`/api/state`
  的 `allow_dirs_detail[0].raw`、以及相对路径被拒时 `bad_allow_dir` 原样回传且不落盘；全量 93/93 通过。
- **P1.3 创建表单 cwd 下拉（原路线图 P1.3 全节）**：`#tCwd` / `#pCwd` 由 `<input>` 换成 `<select>`，
  新增 `renderCwdOptions()`（`ats_ui.py:L1450`，`CWD_SIG` 按 `allow_dirs_detail` 缓存，`render()` 末尾
  `L1196` 调用）。
  选项 = 首项「（默认：root 内部）」+ 每条白名单；标签按「写了什么 → 认成什么」显示，
  `exists:false` 的加 `[目录缺失]` 后缀，白名单为空时 `el.disabled = true` 并在 `#cwdHint` 说明去审批页添加。
  重建选项时保留用户已选值（`opts.some(o => o[0] === keep)`），刷新不会把选中项冲掉。
  提交路径不变：`task.create` / `pack.create` 仍读 `$('#tCwd').value`，空值 = root 内部；
  CLI 侧 `bad_cwd` / `cwd_outside_allowlist` 校验原样保留，UI 只减少误触、不是权限边界。
  `tests/selftest.py` 增 `ui_task_cwd_is_select`：校验页面无 `<input id="tCwd">`、`render()` 确实调用
  `renderCwdOptions()`、选项取自 `allow_dirs_detail`、空名单时 `detail` 为空数组；全量 94/94 通过。
  另用一次性 node 探针（跑完即删）把该函数抽出来执行，实测 2 条白名单 → 3 个选项、
  缺失项带 `[目录缺失]`、名单变更后已选值仍保留。
- **P1.4 移动端适配（原路线图 P1.4 全节）**：新增 `@media(max-width:860px)` 断点（`ats_ui.py:L808`）。
  断点从 720 抬到 860 是实测结论：760 宽时表格仍按表格渲染，列被挤成「等/级」「退/出」「用/时」
  逐字换行、ID 断成 `demo-` / `alpha` 两行；860 之后该宽度走卡片。
  卡片化做法：`thead{display:none}`、`tbody tr` 转成带边框的卡片、字段名靠
  `tbody td::before{content:attr(data-label)}` 显示，为此 `renderTasks`（L1283）与 `renderPlans`（L1305）
  每格补 `data-label`（两表 14 格，空态行 `data-label=""` 不显示前缀）。
  同时 `table{min-width:0}` 覆盖原 `min-width:760px`（L720），否则窄屏仍会横向滚动。
  顶栏与 MCP 条两处溢出：`header{flex-wrap:wrap}` + `.head-right{flex-wrap:wrap;width:100%}`
  修掉口令框与刷新按钮被推出视口；`.mcpbar .row .sub{min-width:0;word-break:break-all}`（L803）
  修掉长端点串撑破卡片。触控与移动端输入：`button{min-height:38px}`、
  `input,select,textarea{font-size:16px}`（低于 16px 会触发 iOS 聚焦缩放）、`.drawer{width:100vw}`、
  `.toasts` 左右贴边。
  验证方式：一次性 Edge 无头探针（跑完即删、不进仓库）在 496 / 760 / 1440 三档截图，
  并注入诊断脚本回报 `clientWidth` / `scrollWidth` 与越界元素清单，结论 `scrollWidth == clientWidth`
  （无横向溢出）；同时逐个切到「新建 / 审批 / 事件 / 任务详情抽屉」确认控件在窄屏可点。
  踩到的坑：Windows 无头 Edge 最小窗口约 496px，请求 390 宽会按 496 排版、截图却按 390 裁，
  看着像右侧溢出——判据是诊断脚本里的 `scrollWidth`，不是截图边缘。
  `tests/selftest.py` 增 `ui_narrow_screen_cards`：校验 860 断点存在、900 档规则保留、
  `attr(data-label)` / `thead{display:none}` / `table{min-width:0}` / 16px 输入 / 38px 按钮 /
  顶栏换行 / MCP 折行均在、`data-label` 覆盖两个表格各字段、宽屏 `<th>` 未被删；全量 95/95 通过。
- v3.2.0 发版准备：测试残留清理、基线重建、`selftest.py` 全绿、打 tag 均已完成（实际 tag 为 `v3.2.0-R0`）。
- 独立 AI 交互层（库级）：`api/ats_api.py` 已落地，位于哈希范围外，日常编排改动不再触发重封签。
- **P0 路径边界（原路线图 P0 全节）**：`allow_dirs` 策略键（`RESTRICTED_POLICY_KEYS` 成员）、
  `cwd_allowed()` / `resolve_cwd()` / `_path_within()` 三个判定函数、`create`/`pack` 建阶段拒越界、
  `_start_task` 与 `_supervise` 两道执行前二次校验（`supervise` 直接调用也重算）、`run_post_hook` 与
  preflight 上下文统一用同一解析结果、相对 `cwd` 改以 root 为基准（修掉原 0.1b 的基准错位）、
  `policy --set allow_dirs` 写入前逐项校验（相对路径 / 缺失目录拒 `bad_allow_dir`）、
  `policy --json` 输出 `allow_dirs_detail` + `boundary_warnings`。
- **P0 Plan 层边界补齐**：`ats_plan.py` 的 `validate_spec`（提交即校验每个 step 的 `cwd`）、
  `materialize_plan`（落盘前归一化）、`ensure_step_permission`（执行前二次校验，`cwd_outside_allowlist` / `cwd_gone`）、
  `start_step` 与 preflight 上下文同用 `resolve_cwd()` —— Task 与 Plan 两条执行路径现由同一套规则约束，无权限缺口。
- **P0 边界情况实测（原 0.3 清单，全部实跑，非推断）**：
  root 内放行、白名单子孙放行、`allowed-evil` 前缀相似拒、`..` 逃逸后仍在白名单内放行（归一化正确）、
  大小写变体放行、相对路径解析到 `root/work`、8.3 短名解析到真实长名、UNC `\\srv\share\a\b` 在 `\\srv\share\a` 内为真且 `\ab` 为假、
  junction 指向白名单外为假、junction 指向白名单内为真、白名单项为 root 父目录为真（并由 `boundary_warnings` 标注）。
  *（本机未开启 8.3 生成，`dir /x` 无 `~` 别名可测，该项以「短名解析到真实长名」间接验证。）*
- **P0 测试与文档**：`tests/selftest.py` 增 `cwd_outside_allowlist_rejected_at_create` / `relative_cwd_is_root_relative` /
  `cwd_gone_blocks_run_without_fallback` / `supervise_rechecks_cwd_before_spawn` / `policy_allow_dirs_validates_each_entry` /
  `policy_reports_allow_dirs_boundary` / `plan_step_cwd_respects_allowlist`，全量 89/89 通过；`permissions.md` §4.1、
  `command-guide.md` §4.1、`error-taxonomy.md`、`api-guide.md`、`orchestration-spec.md` 对应条目已与实现对齐。