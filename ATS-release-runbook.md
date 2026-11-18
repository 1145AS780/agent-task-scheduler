# 发版手册（release runbook）— agent-task-scheduler

这份文档只回答一件事：**代码改完之后，到推上 GitHub 之间还差哪几步**。
版本号「该进哪一位」的规则在 [references/versioning.md](references/versioning.md)，本文不重复，
只讲操作顺序、封签范围、落点清单与踩过的坑。

文中所有数字都是 2026-11-18 的实测值（命令附在每一节，可随时重测）。**改完代码要重新测，
别照抄本文的数字**——上一轮就是因为抄旧表，把 19 处写成了 20 处。

---

## 0. 三条铁律

1. **先跑绿，再动版本号。** 顺序颠倒等于用一条红测试去封签：封完才发现代码有问题，
   你已经多背了一次「回滚 + 重封」，而且分不清红是代码坏还是版本号改坏。
2. **`scripts/` 下任何 `.py` 改动，必须重封签。** 这是机械约束，与「这次改动严不严重」无关。
   `core_files()` 收的是 `scripts/` 顶层全部 `.py`，摘要一变 `verify` 就失配，
   随后所有 `ats.py` 命令都会以 `guard_check_failed` 拒绝执行。
3. **已推送的 tag 不回改。** 拉过这个 tag 的人手上就是那份内容，改远端 ref 只会让仓库里存在
   两套事实，且事后无法分辨谁拿到哪一套。要改内容只能发新版本。

---

## 1. 按改动类型选最短流程

| 改动类型 | 进位 | 重封签 | 改版本号 | 最短流程 |
| --- | --- | --- | --- | --- |
| 只改 `.md`（README / SKILL / references） | `D` 或只换码 | 否 | 否 | 改 → lint → commit |
| 只改 `tests/`（`scripts/` 不动） | `D` 或只换码 | 否 | 否 | 改 → lint + selftest → commit |
| 改 `scripts/*.py`（修 bug / 加功能） | `D` / `C` / `B` | **是** | 是 | 见 §2 全流程 |
| 改权限边界、封签语义、签证语义 | `B`（攒够 3 项才进） | **是** | 是 | §2 + 安全警告段 + 主动通知使用者 |
| 新增 `scripts/*.py` | 同上一行 | **是** | 是 | 同上；不封会报 `unsealed_file` |
| 新增 `plugins/<id>/` 插件 | 只换码 | 只封插件 | 是 | `plugin seal <id>`，核心基线不动 |
| 新增 `extend/<id>/` 扩展插件 | 只换码 | 否 | 否 | 改 → `extend check <id>` |

版本位的取舍拿不准时一律**只换构建码**：码的承诺更弱，写错位的代价更小（依据见 versioning.md §2.1）。

---

## 2. 全流程（改了 `scripts/*.py` 的场合）

```
step 1   改代码
step 2   python tests/lint.py              → 必须 0 问题（当前 18 个文件）
step 3   python tests/selftest.py          → 必须全绿（当前 107 条，加了用例就更新这个数）
step 4   python tests/readme_parity.py     → 只在动过 README 时跑，必须 OK（当前 46 个结构块）
step 5   改版本号（按 §4 清单逐项替换，同一新串，不能只改一半）
step 6   CHANGELOG.md 新增版本节 + SKILL.md §15 新增表行（同源两处，必须同批改）
step 7   封签（最后一步做，封完再改 .py 就等于自己作废基线）
step 8   python scripts/ats_guard.py verify → 必须 "ok": true 且 "problems": []
step 9   重跑 lint + selftest（证明封签动作没在仓库里留下污染）
step 10  git commit → git tag → git push（分支与 tag 都要推）
```

封签怎么执行：

- **人手动发版**：在 `OCSKIS/` 下跑 `setup.bat`，口令走交互输入（不进 argv、不进命令历史）。
  首次（无密钥）它会先 `init` 再 `seal`。
- **已有密钥、只重封**：`python scripts/ats_guard.py seal --confirm --passphrase-stdin`，
  口令由 stdin 提供。**别把口令写进命令行参数**，也别写进 `.ps1` 字面量——那会进命令历史。
- **只想先试功能、不想动真身**：`python tests/mini.py new` 建一个固定沙箱
  （`temp/ats-mini/root`，固定口令），`mini.py ats <参数>` 跑真实 CLI，`mini.py seal` 在沙箱里重封。
  沙箱根与口令记在 `temp/ats-mini/mini.json`，`temp/` 已被忽略。**沙箱绿不等于真身绿**，
  两者 root 不同，别把沙箱结论当成发版依据。

打 tag（注意全角冒号与引号）：

```powershell
git tag "v0.3.1.1：w17vbc"
git push origin master
git push origin "v0.3.1.1：w17vbc"
```

---

## 3. 封签范围（按 `core_files()` 实测）

`scripts/ats_guard.py` 的实现是 `sorted(p.name for p in SCRIPT_DIR.glob("*.py"))`——
**只扫 `scripts/` 顶层、非递归、只收 `.py`**。当前 13 个文件。

| 路径 | 进核心基线 | 依据 |
| --- | --- | --- |
| `scripts/*.py`（顶层，当前 13 个） | **是** | `core_files()` 的全部范围 |
| `scripts/` 下的子目录 | 否 | `glob` 非递归 |
| `scripts/*.cmd`、`*.bat` | 否 | 只收 `.py` |
| `api/ats_api.py` | 否 | 在 `scripts/` 之外；代价（自身不受封签保护）写在文件头 |
| `tests/*.py` | 否 | 不在 `scripts/` |
| `references/*.md`、根目录 `*.md` | 否 | 文档 |
| `plugins/<id>/`（插件代码） | 否 | 单独一份 `<root>/ats_guard_plugin_manifest.json` |
| `extend/<id>/instruction.json` | 否 | 只读清单，执行走子进程 Task 链路 |
| `.ats/` 运行态、密钥、两份清单 | 否 | 不是代码，且已被 `.gitignore` 排除 |

两个直接后果：

- **新增 `scripts/` 下 `.py` 必须重封**，否则 `verify` 报 `unsealed_file`（实测见过）。
- **插件换版不重封核心**：`python scripts/ats_guard.py plugin seal <plugin_id> --confirm --passphrase-stdin`
  只重算该插件，其余插件条目与其他核心文件一字不动。反过来说，插件自身也必须封签，
  因为它的 `Injection.py` 是在 ats 主进程里 `exec` 的，封签是它唯一的闸门。

---

## 4. 版本号落点（实测，勿抄旧表）

发版前先跑这三条，把实际命中补进本表：

```powershell
Get-ChildItem scripts -Recurse -Filter *.py | Select-String '0\.3\.1\.1：w17vbc'
Get-ChildItem references -Filter *.md | Select-String 'v0\.3\.1\.1：w17vbc'
Select-String -Path SKILL.md,CHANGELOG.md -Pattern '0\.3\.1\.1：w17vbc'
```

当前实测 **22 处 / 16 个文件**：

| 文件 | 位置 | 处数 | 性质 |
| --- | --- | --- | --- |
| `SKILL.md` | 第 4 行 `version:`、第 7 行标题 | 2 | 替换 |
| `SKILL.md` §15 | 警告段正文里的版本串 | 1 | 随内容改写 |
| `SKILL.md` §15 | 版本历史表 | 1 | 新增行 |
| `references/*.md` | 10 份各第 1 行（含 `versioning.md` 自身标题） | 10 | 替换 |
| `scripts/ats.py` | docstring + `build_parser` 的 `description` | 2 | 替换 |
| `scripts/ats_plan.py` | docstring + `description` | 2 | 替换 |
| `scripts/ats_torrent.py` | docstring | 1 | 替换 |
| `scripts/ats_ui.py` | docstring + `VERSION` 常量 | 2 | 替换 |
| `CHANGELOG.md` | 新版本节标题 | 1 | 新增节 |

与 versioning.md §5 记的「20 处」相差 2 处，差额是这里多算的两行：**SKILL §15 警告段正文**与
**CHANGELOG 新节标题**。口径没变，是原来那张表没把它们列进去。

三条容易改错的边界：

- `scripts/ats_ui.py` 的 `HTTP_VERSION = VERSION.replace("：", "-")` 是**派生**常量，
  只改 `VERSION`，**永远不要手改它**——HTTP 响应头走 latin-1，全角 `：` 编不进去，
  手改会变成每条响应都抛 `UnicodeEncodeError`。
- `scripts/ats_mcp.py` 的 `SERVER_VERSION = "0.1.0"` 与 `scripts/ats_mcp_http.py` 的 `(v1.0)`
  是**模块自身**的版本号，不随项目进位（MCP 有独立的客户端兼容问题，跟着跳只会让客户端误判）。
- `references/versioning.md` 内部还有多处版本串（§3 示例表、§5 清单、§6/§7 历史判定）——
  那些是**举例与历史记载**，不随发版替换。改错了就是篡改历史，反而比漏改更糟。

---

## 5. 常见坑（本项目实际踩过的）

- **半角冒号打不出 tag**：git ref 名不允许半角 `:`，必须用全角 `：`。
  先自检：`git check-ref-format "refs/tags/v0.3.1.1：w17vbc"`，半角版本会失败。
- **CHANGELOG 与 SKILL §15 不同源**：两处是同一份内容的两处落点，必须同批改，
  否则从 GitHub Releases 和从 SKILL.md 会读到两套说法。
- **改了 `scripts/` 忘了封签**：下一步所有 `ats.py` 命令全部 `guard_check_failed`，
  看起来像「代码坏了」，实际只是基线过期。
- **先改版本号再跑测试**：测试红了分不清是代码坏还是版本号改坏（铁律 1）。
- **封签产物不进仓库**：`.ats/`、`ats_guard_key.json`、两份 `*_manifest.json` 都在 `.gitignore` 里。
  所以别人 clone 下来**必须自己跑一次 `setup.bat`**，不能指望仓库里带一份现成基线。
- **文件结尾缺换行**：`tests/lint.py` 会直接判 FAIL（实测一次踩了 4 个文件）。
  改完脚本顺手检查一下最后一行有没有换行。
- **只在沙箱验证不跑真身**：`mini.py` 的 root 是 `temp/ats-mini/root`，真身是 `OCSKIS/.ats`。
  沙箱上跑出来的绿，是沙箱的绿。
- **用签证跳过封签后又忘了重封**：签证只是「改了核心脚本、还没封，但要先跑一轮测试」的临时通行证，
  配额扣完自毁；它不是发版流程的替代品，发版仍然要 `seal`。

---

## 6. 已推送但没有 Release 的 tag 怎么算（`v4.0.0-R0` → `v0.3.1.1：w17vbc`）

先把事实核清楚，别凭印象：

```powershell
git ls-remote origin "refs/tags/v4.0.0-R0"
# → faeacaf3d9ad505e69d0612620e6e5589a240116  refs/tags/v4.0.0-R0

Invoke-RestMethod "https://api.github.com/repos/1145AS780/agent-task-scheduler/releases"
# → v3.1.1 / v2.9.1 / v2.7.0
```

结论：`v4.0.0-R0` 的 **tag 推送成功了**（远端确有该 ref），没成功的是 **GitHub Release 的创建**。
两件事要分开看，因为「tag 存在」与「对外发布了一个版本」在 GitHub 上不是同一件事。

因此「把 `4.0.0-R0` 合并到 `0.3.1.1：w17vbc`」只能在**口径层面**做，落地规则是：

- `0.3.1.1：w17vbc` 承接 `4.0.0-R0` 的**全部内容与承诺**，包括那条 `cwd` 边界收窄的升级警告；
  数字沿用「GitHub 已发布 Release 的最新版 `3.1.1`」续号，理由见 versioning.md §7。
- `4.0.0-R0` 降为**历史 tag**：不再作为对外版本号、不补 Release、不在 CHANGELOG 里当发布节点。
- **远端 tag 不删**。删除远端 ref（`git push origin :refs/tags/v4.0.0-R0`）与铁律 3 冲突；
  且已经 fetch 过的人手上仍有这个 tag，删了只会让仓库里多出一套事实。
- 若确实要删，那是人的决定：删完必须 `git ls-remote --tags origin` 复核，
  并在 CHANGELOG 里写明「删了哪个 tag、为什么、什么时候」。

同一规则适用于任何「tag 推了但没发 Release」的情况：**先查 Release 列表，再决定口径**，
不要用「推送失败」去解释一个其实推送成功的 tag。

---

## 7. 回滚

| 状态 | 做法 |
| --- | --- |
| 未封签、未提交 | `git checkout -- <file>` |
| 已封签、未提交 | `git checkout -- <file>` → **重封签**（封签绑的是文件内容，文件回滚就得再封一次） |
| 已提交、未打 tag | `git revert <sha>` → 重封签 |
| 已打 tag、未推送 | `git tag -d "v…：…"` → 重封签 → 重新打 |
| 已推送 | **不回改**，发新版本（新码或进位），CHANGELOG 里说明上一版哪里错了 |
| 只是运行中的任务失控 | 用 `ats.py abort` 急停、`unpack` 进维护态，不要用回滚代码来解决运行态问题 |

---

## 8. 附录：本机 GitHub 链路现状（2026-11-18 实测）

发版流程里唯一依赖外网的环节是「推代码 / 推 tag / 发 Release」。各链路在本机实测结果不同，
**不要把「配置里写了」当成「能用」**：

| 链路 | 实测命令 | 结果 | 结论 |
| --- | --- | --- | --- |
| `git push`（HTTPS） | `git push origin master` | `30c4514..fd4a45e  master -> master` | **可用**，推代码 / 推 tag 走这条 |
| 匿名 REST（公开读） | `GET api.github.com/repos/...` | `private=False`，`releases=3` | **可用**，查 tag / Release 用这条 |
| `gh` CLI | `gh auth status` | `You are not logged into any GitHub hosts` | **不可用**，本机无法完成登录 |
| MCP 本地 stdio | `search_repositories` / `get_file_contents` | 返回真实数据，26 个工具 | **可用**（2026-11-18 修好后） |
| MCP 官方远程插件 | `mcp.config…:github.github` | `Unauthorized` / `authentication failed` | **不可用**，走 Trae 账号 OAuth，与 PAT 无关 |
| `npx` 拉包 | `npm view …server-github version` | `version = '2025.4.8'` | **可用**（npm 已修，见下） |

### 8.1 MCP 本地链路修通前踩的四个坑

四条全部实测过，缺任何一条都起不来，且报错信息彼此掩盖：

| 坑 | 现象 | 实测根因 | 处置 |
| --- | --- | --- | --- |
| npm 自损 | `npm error Class extends value undefined is not a constructor or null` | nvm 目录里 npm 11.19.1 自带的 `minipass-flush@1.0.6` 与 `minipass@7.1.3` 链断 | 用官方同版本包整体替换，坏目录备份为 `npm.broken-20261118` |
| TLS 证书 | `fetch failed`，`cause=UNABLE_TO_VERIFY_LEAF_SIGNATURE` | node 自带 CA 库不读 Windows 证书库 | 给 server 的 env 加 `NODE_OPTIONS=--use-system-ca` |
| 令牌失效 | `401 Bad credentials` | 旧 `github_pat_` 已废 | 换新令牌，实测 `/user` 200、仓库 `admin/push/pull` 全 True |
| 服务重名 | 调本地名却落到远程插件，报 `Unauthorized` | 本地 `GitHub` 与官方远程 `github.github` 同名，本地被顶掉 | 本地改名 `github-local`，两者并存互不干扰 |

改完要**重载窗口**才生效：MCP 工具表是启动时快照的，改名后当次会话仍按旧名查找，会报 `Server … not found`。

### 8.2 Release 这一步为什么仍然只能手工做

`github-mcp-server 0.6.2` 的 26 个工具里**没有任何 Release 相关工具**（只有文件、分支、Issue、PR、搜索）。
所以「MCP 修好了」不等于「能发 Release」：

- 查：走匿名 REST，`GET /repos/{owner}/{repo}/releases`
- 发：`POST /repos/{owner}/{repo}/releases`，**必须带令牌**，无匿名路径
- 别在脚本里写 `gh release create` 或假设 MCP 能建 Release，本机这两条都不成立

顺带一个实测口径：远端现有 6 个 tag（`v2.7.0`…`v4.0.0-R0`），而 Release 只有 3 个
（`v3.1.1`、`v2.9.1`、`v2.7.0`）——`v3.2.0-R0`、`v3.3.0-R0`、`v4.0.0-R0` 三个 tag 都没有对应 Release。
这正是 §6「tag 存在 ≠ Release 已创建」的实证，不是个例。

### 8.3 三条操作纪律

- **令牌不进仓库、不进脚本、不进命令历史**。MCP 配置是 Trae 的用户级文件
  `%APPDATA%\Trae CN\User\mcp.json`，不属于本仓库，也不该被复制进任何 `.ps1` / `.bat`。
  另有一份同名配置在 `%USERPROFILE%\.marscode\IDEA.mcp.config.json`，两边令牌可能不同步，改动时以实测 `/user` 为准。
- **只读查询一律走匿名 REST**（公开仓库不需要令牌），命令见 §6。
- **修环境之后必须重跑本节的表**。上面每一条结论都是当时那套 `node` / `npm` / 令牌状态下的结果，
  不是版本承诺；换了任何一个，旧结论立即失效。

> 这一段只记录**当前环境**，不是版本承诺。改动时请重跑表里的命令，把新结果写回来，别沿用这里的旧结论。