# 定制 cron 周期引擎流程说明（精简版，V3）

> 用途：说明当前定制分支 `feat/v3-eng-custom` 上**每日自动周期（cron）** 的实际引擎流程。
> 本分支相对主线的核心差异：`689d6cf`（cron 精简为 同步→编译→通知，去掉判断/解决类 LLM
> 节点）、`56ed054`（周期邮件收件人支持 PM 名单 + 失败追加合入人邮箱）与负责人映射接入。
> 内容与代码实现一致（`src/bsa/graph/workflow.py` `build_workflow`、`src/bsa/graph/nodes.py`、
> `src/bsa/scheduler/cycle.py`、`src/bsa/scheduler/recipients.py`）。

---

## 1. 系统定位

**BSA（Branch Sync Agent）** 解决：业务分支（source，如 `br_v4.34_develop_fttr_20260811`）上
合入的提交，逐个同步（cherry-pick + 编译验证 + 生成 patch）到同源主分支（target，如
`br_v4.34_develop_20260130`）。

**本分支 cron 的定位（关键差异）**：

- cron 每日周期只做**确定性同步链**：检测 → 全量冻结批次 → 编译验证 → 通知。**不做**：
  bug-fix/风险/相似度四态分类（`SyncDecisionAgent`）、冲突自动消解（`ConflictAgent`）、
  编译失败自动修复（`BuildAgent`）、失败关联判断（`fail_fast`）。
- 冲突或编译失败**不自动解决、不重试**：直接停本分支批，收尾生成 patch，出周期报告邮件。
- 平台手动同步（`bsa sync` / `bsa rerun`）走 `single_target.py` 的完整 agent 图，**不受影响**，
  保留全部判断/解决能力。

**两条入口分叉**（详见 §5 对比）：

| 入口 | 图 | 判断/解决 LLM | 场景 |
|---|---|---|---|
| cron 周期 | `workflow.py: build_workflow`（精简） | 无（仅检测时打标，不进门控） | 每日 0:00 自动 |
| 手动同步 | `single_target.py: build_single_target_workflow`（完整） | 有（resolve/fix/fail_fast） | 平台勾选 commit / rerun |

---

## 2. 同步拓扑（分支矩阵）

- 由 `CRON_BRANCH_FILE` 指向的分支文件（带「主分支 / 业务分支」标注的 section 标题）决定，
  **不从分支名推断**（不变量 #1）。
  - 含「主分支」的 section → **目标**（建 worktree、基线编译、逐 commit 收）
  - 含「业务分支」的 section → **源**（只扫不建/不编译）
  - 业务 → 主 单向；业务之间互不同步；主不回灌业务；未标注 section 不产生同步边（安全默认）。
- `CRON_BRANCH_FILE` 留空则回退 `BRANCH_FILE`（老部署零改动兼容）。
- **矩阵为空 = 响亮报错**：`detect_commits` 写 `errors`（`node="detect_commits"`），周期转
  action_required，**绝不静默空跑**。
- 实际矩阵（`CRON_BRANCH_FILE` 指向的标注文件，本部署为 `spec/branch-cron.md`）：产品线
  4.34，源 = 两个业务分支 `br_v4.34_develop_fttr_20260811`、`br_v4.34_MSG_develop_20260805`；
  目标 = 主分支 `br_v4.34_develop_20260130`。2 个「业务 → 主」同步对，1 个目标。

`BRANCH_FILE`（完整清单）不参与 cron 配对，只服务手动同步的编译型号解析与平台下拉
（`commands/sync.py`、`bsa_web/branches.py`）。

---

## 3. cron 精简图（节点与路由）

```
START
  → detect_commits（fetch_all + 矩阵 + 窗口扫描 + classify 打标）
  → sync_decision（全量冻结 → batches + build_models）
  → next_branch（选下一个未完成目标；无 → report）
  → prepare_worktree（远端 tip 建/复用 worktree）
  → baseline_build（目标 tip 原始态逐型号 clean 全量编译）
       ├─ BASELINE_FAILED → 分支 FAILED(blocked) → 台账记 blocked → next_branch
       └─ BASELINE_OK →
            → next_commit（选批次内下一个未处理 commit；无 → generate_patch）
            → cherry_pick
                 ├─ CHERRY_PICK_CONFLICT → generate_patch（停批，不 resolve）
                 ├─ CHERRY_PICK_EMPTY    → next_commit（内容已应用，跳过编译）
                 ├─ CHERRY_PICK_FAILED   → report（基础设施失败，节点边界）
                 └─ OK → build
                      → build 多型号串行循环（_next_model）
                           ├─ BUILD_OK（全部型号过）→ next_commit
                           └─ BUILD_FAILED → generate_patch（停批，不 fix_build）
            → generate_patch（format-patch 原 tip..HEAD + 台账 synced/failed）
            → next_branch（…循环）
  → report（HTML + 邮件 + cycle.json/state.json）
```

图定义见 `workflow.py: build_workflow`（404–496）。cron 图**不注册** `resolve_conflict` /
`fix_build` / `fail_fast` 节点。

### 3.1 关键路由状态机（cron 专用路由）

cherry-pick 后停批路由 `_route_after_cherry_pick_cron`（workflow.py:372）：

| status | 去向 | 语义 |
|---|---|---|
| `FAILED` | report | 节点异常（node boundary） |
| `CHERRY_PICK_CONFLICT` | `generate_patch` | **冲突直接停批**（不 resolve_conflict），收尾 patch |
| `CHERRY_PICK_EMPTY` | `next_commit` | 内容已应用，跳过编译（不变量 #17） |
| `CHERRY_PICK_FAILED` | report | 非冲突基础设施失败 |
| 其他（OK） | `build` | 进编译 |

build 后路由 `_make_route_after_build_cron`（workflow.py:390）：

| status | 去向 | 语义 |
|---|---|---|
| `FAILED` | report | 节点异常 |
| `BUILD_OK` 且还有型号 | `build` | 多型号内循环继续 |
| `BUILD_OK` 且型号跑完 | `next_commit` | 本 commit 完成，下一个 |
| `BUILD_FAILED` | `generate_patch` | **编译失败即停批**（不 fix_build 自动修复），收尾 |

其余共享路由：

- `_route_after_decision`（sync_decision 后）：`FAILED` 或 `batches` 空 → report；否则 next_branch。
- `_route_after_next_branch`：`FAILED` 或 `current_target` 空 → report；否则 prepare_worktree。
- `_route_after_prepare`：`FAILED` → report；否则 baseline_build。
- `_route_after_baseline`：`BASELINE_FAILED` → next_branch（跳过阻塞分支）；否则 next_commit。
- `_route_after_next_commit`：`FAILED` → report；无剩余 commit（`current_commit` 空）→
  generate_patch；否则 cherry_pick。
- `_route_after_patch`：`FAILED` → report；否则 next_branch。

**停批语义**：一个 commit 冲突或编译失败 → 该 commit 及批次剩余 commit 全部不再处理，
`generate_patch` 只对**已成功 cherry-pick 且 build 全 OK** 的 commit 记 `synced`，失败/未处理
的不入 `synced`，分支终态收敛为 PARTIAL/FAILED（见 §4.3）。

---

## 4. 各节点语义

### 4.1 detect_commits（检测）

- 窗口：每日 0:00 触发，默认前一日 **22:00–22:00**（24h + 2h 沉降缓冲）。手动 `manual-scan`
  / `run-cycle --since/--until` 可覆盖 `scan_since/scan_until`（`nodes.py: _derive_window`）。
- 用 **committer date**（合入时间），非 author date；`--first-parent` + patch-id 去重。
- `ctx.git.fetch_all()` 后按矩阵逐源分支 `commits_in_window` 取窗口内 commit。
- `classify_commit`（含人工覆盖 judgments.json）**只打标供报告展示**，不参与批次门控
  （这是精简点：分类结果仍记录，但 sync_decision 不再据此判断）。
- 源分支类型与型号解析：产品线由矩阵 section 决定。

### 4.2 sync_decision（全量冻结，精简核心）

`nodes.py: sync_decision`（407）：

- **全量冻结**：凡矩阵内业务 source 检测到的 commit，一律 `NeedSync` 批入同源主 target，
  不再调 SyncDecisionAgent / 快照 / 相似度四态。
- 保留**两条确定性短路**（不变量）：
  - **forbidden 分支**：`safety.check_sync_branch(target)` 失败 → `OutOfScope`，不入批。
  - **台账幂等**：`(patch_id, target)` 命中 `synced` → `AlreadyIncluded`，跳过（跨天幂等）。
- 产品线 → 编译型号：每目标解析一次（`resolve_build_models`）；未配置/未知产品线 →
  `errors` + 移出批次（`BuildConfigError` → action_required），绝不静默用错脚本。

### 4.3 分支/commit 终态收敛

- **commit 成功判据**（`_commit_ok`）：`cherry_pick ∈ {OK, EMPTY}`，且（EMPTY 直接算成功）
  非 EMPTY 时所有型号 `build` 全 OK。
- **分支终态**（`_final_branch_status`）：全部 commit OK → `SUCCESS`；全不 OK → `FAILED`；
  混合 → `PARTIAL`。
- **停批分支**：冲突/编译失败停批 → `generate_patch` 只对已成功 commit 记 `synced`，
  失败 commit 记 `failed`（`ledger.record_status`）；未处理 commit 不入台账。
  分支 stop_reason 记录停批原因（如 "conflict"），报告/邮件可见。
- **基线失败分支**：`baseline_build` 失败 → 分支 `FAILED` + stop_reason
  `baseline build failed on <model>`，**批次全部 commit 记 `blocked`**（`ledger`），
  跳下一分支，不做任何 cherry-pick。
- **周期终态**（`_cycle_terminal_status`）：有 errors → FAILED；任一分支 FAILED/MANUAL →
  FAILED；任一 PARTIAL → PARTIAL；否则 SUCCESS。

### 4.4 generate_patch 与台账

- `git format-patch 原tip..HEAD`，每分支一份 `patch/<cycle_id>_<target>.patch`。
- 逐 commit 按 `(patch_id, target)` 落台账：成功 → `synced`；失败 → `failed`
  （`ledger.record_synced` / `record_status`）。仅 `synced` 参与下周期幂等短路。

### 4.5 report 收尾

- `report` 图节点只装配 `Report` 模型（`report` 是精简图的 `_END_NODE`），设置周期终态
  `action_required`（= ManualReview + 节点 errors；本精简 cron 下 sync_decision 不产出
  ManualReview，故主要为节点 errors/产品线配置错误）。
- **HTML/decisions/审计/邮件不在图节点内做**：`graph.invoke` 返回后由
  `scheduler/cycle.py _execute_locked` 收尾——写 `state.json`、`render_html_report`、
  `write_decisions_json`、`write_agent_diffs`、解析收件人并发邮件（§6）、写 `cycle.json`。

---

## 5. cron 精简 vs manual 完整 agent 流程

| 维度 | cron（build_workflow） | manual（single_target） |
|---|---|---|
| 入口 | detect → sync_decision(全量冻结) | 跳过 detect/decision（用户勾选=人工已审） |
| 判定 | 全量冻结 + forbidden/台账短路 | conclude_pair 四态 + LLM/快照 |
| 冲突 | **直接停批**，不 resolve | ConflictAgent 3 轮消解 + 字节快照回滚 |
| 编译失败 | **直接停批**，不 fix | BuildAgent 归因 + 修复循环 |
| 失败后续 commit | 不处理，整批停 | fail_fast 三层关联判断（相关停批/无关继续） |
| 产物 | 每目标独立 patch | 同左 |

二者共用同一批节点函数（`prepare_worktree` / `baseline_build` / `cherry_pick` / `build` /
`generate_patch` / `report`）与记账逻辑，仅**图结构（节点集 + 路由）不同**，零实现冗余。

---

## 6. 邮件推送管理流程（周期报告收件人）

> 谁在什么条件下收到周期邮件、收件人从哪来。统一约定：**主送（To）= 成功只发管理收件人
> （PM）；失败/报错在 PM 之外追加两类"相关责任人"——合入人 与 模块负责人**；另设固定
> **抄送（Cc）= SSE 等需知情但非主责的名单**，To/Cc 全部去重保序。
> 收件人解析实现在 `scheduler/recipients.py: resolve_report_recipients`，Cc 直配
> `mail_cc_recipients`，发送在 `scheduler/cycle.py _execute_locked`（§6.4）。

### 6.1 收件人规则总览

周期报告是**同一封邮件**，To 随周期结果不同（`resolve_report_recipients`），Cc 固定：

| 周期结果 | To（主送） | Cc（抄送） |
|---|---|---|
| 全 SUCCESS | **仅 PM 名单** | `mail_cc_recipients`（如 SSE） |
| 有分支失败/报错 | **PM + 失败 commit 合入人 + 失败 commit 改动模块的负责人** | 同左（固定，不随失败追加） |
| 仅节点 `errors`（无失败 commit） | **仅 PM 名单** | 同左 |

To 基础名单：`mail_pm_recipients`（项目经理，正式收件人）非空用之，空则回退 `mail_recipients`
（老部署收件人）。**基础名单永远保留**，不被任何追加挤掉。

Cc 名单：`mail_cc_recipients`（通常 SSE/管理层），固定抄送，**不参与失败追加**——追加的
合入人/负责人进主送 To，不重复进 Cc。空则不抄送。

失败 commit 判据（与报告正文 `_branch_failure_lines` 对齐）：非 `SUCCESS` 分支上
`cherry_pick ∉ {OK, EMPTY}`，或任一型号 `build == FAILED` 的 commit。

### 6.2 失败时追加的两类相关责任人

两者**并存追加**，不互相替代，全部去重保序：

**① 合入人**（把失败 commit 带进业务分支的人，`git` 反查）：
- commit 自身 committer（`git.committer_email`，直提场景 author==committer 即合入人）；
- 若是经个人分支 merge 引入，追引入它的 merge 的 committer
  （`git.merge_committer_emails`，沿 `sha..branch` first-parent 找「第二父含 sha、第一父不含」
  的 merge）。
- git 追溯取不到邮箱 → 不追加也不崩（收件人宁缺不崩）。

**② 模块负责人**（改到谁的模块谁负责，`module_owner_map.json` 驱动）：
- 对每个失败 commit 取 `git.changed_files(sha)` 的改动文件路径，逐路径做**最长目录前缀匹配**：
  `path == key 或 path.startswith(key + "/")` 即命中（**目录边界**，防 `security` 误配
  `securityDomain`）；父目录与子目录都有 key 时取**最长**那个。
- 命中 key 的全部负责人邮箱都追加——**改动跨多个负责人模块时，所有命中负责人都收信**。
- owner map 未配置（`module_owner_map_path` 留空）→ 负责人维度整体关闭，只保留 ①，行为同老部署。

### 6.3 配置

（`src/bsa/config/settings.py`）：

| 字段 | env | 默认 | 说明 |
|---|---|---|---|
| `mail_pm_recipients` | `MAIL_PM_RECIPIENTS` | `[]` | 项目经理名单（To 正式收件人）；空则回退 `mail_recipients` |
| `mail_cc_recipients` | `MAIL_CC_RECIPIENTS` | `[]` | 抄送名单（如 SSE，固定抄送，不参与失败追加）；空则不抄送 |
| `mail_recipients` | `MAIL_RECIPIENTS` | 必填 | 回退名单 / 老部署收件人 |
| `module_owner_map_path` | `MODULE_OWNER_MAP_PATH` | `""` | 模块负责人映射 JSON 路径；空 = 负责人维度关闭 |
| `mail_phase` | `MAIL_PHASE` | `"test"` | 收件阶段过滤；正式对 PM 发信须 `prod` |
| `mail_test_allowlist` | `MAIL_TEST_ALLOWLIST` | `["yangfu@raisecom.com"]` | test/dev 阶段收件人硬帽白名单 |
| `mail_append_failure_related` | `MAIL_APPEND_FAILURE_RELATED` | `true` | 失败时是否追加合入人/负责人邮箱 |
| `mail_dry_run` | `MAIL_DRY_RUN` | `true` | true → 不真发，记 `skipped` |
| `mail_smtp_host` | `MAIL_SMTP_HOST` | `smtp.exmail.qq.com` | SMTP 主机（BSA 原生发信） |
| `mail_smtp_port` | `MAIL_SMTP_PORT` | `465` | SMTP 端口 |
| `mail_smtp_security` | `MAIL_SMTP_SECURITY` | `"SSL"` | `SSL` / `STARTTLS` / `PLAIN` |
| `mail_smtp_user` | `MAIL_SMTP_USER` | `""` | SMTP 登录账号；空则回退 `mail_sender` |
| `mail_smtp_pass` | `MAIL_SMTP_PASS` | `""` | SMTP 口令；空则回退通用 `SMTP_PASS` |
| `mail_from_name` | `MAIL_FROM_NAME` | `RCIOS 分支同步报告` | 发件人显示名 |
| `mail_timeout_sec` | `MAIL_TIMEOUT_SEC` | `20` | SMTP 超时（秒） |

`module_owner_map_path` 指向的 JSON（本部署样例 `spec/module_owner_map.json`）：
- `module_owner_map`：**目录前缀 → [负责人邮箱]**（130+ 条），如
  `"datapath/kernel/rcios/forward": ["liupengbin@raisecom.com"]`。这是收件人路由消费的唯一段。
- `group_owner_emails`：**模块组名 → 负责人邮箱**，是 `module_owner_map` 的**同源索引**
  （如「安全模块」→ wanghongbin，对应下方整组 security 系路径），**替代关系而非叠加**：
  收件人解析只走 `module_owner_map` 路径级匹配，不消费 `group_owner_emails`。改负责人只改
  一处即可同时影响组索引与路径映射。
- `git_blame_enabled` / `git_email_domain`：保留字段，面向行级 blame 归因类消费者，**本
  周期邮件链路不使用**。
- 文件缺失/解析失败只告警并关闭负责人维度，不崩（`recipients.py` 内记 warning）。
- 该文件路径不入 BSA git 追踪语义：BSA 进程按 `MODULE_OWNER_MAP_PATH` 在运行期读取，改完
  负责人下个周期即生效，无需重启引擎（文件每次读取经 `lru_cache` 缓存）。

### 6.4 发送链路

`_execute_locked` 收尾：`report is not None` 时，若 `not mail_dry_run` 构造 **BSA 原生** sender
（`make_smtp_sender(settings=settings, output_dir=cycle_dir, mail_to=recipients,
mail_cc=settings.mail_cc_recipients)`），经 `MailService.send_report` 发出；**失败不抛异常**，
记 `mail_status`，并把结果落盘 `bsa_mail_send_result.json`（周期目录内）便于审计。日志记录最终
收件人列表便于核对。

发信由本仓 `bsa/mail/smtp_sender.py` 用 `smtplib` 直发，**不依赖任何外部 bridge / 插件**
（历史上曾借道 pon-gw 的 BMA `branch_maintenance` bridge 再转 release-integration 插件，
导致邮件被改成 BMA 品牌、排障跨仓跨用户目录）。主题/正文由引擎给出，呈现 BSA 自己的品牌。

**重要**：`mail_phase` 默认 `test` 时，收件人会被硬帽过滤到白名单
（`mail_test_allowlist`，默认 `["yangfu@raisecom.com"]`），cc 同样过滤。因此**正式对非测试
收件人（PM / 合入人 / 负责人）发信必须配 `MAIL_PHASE=prod`**，否则配了也会被滤掉。开发/联调
阶段保持 `test` 可只发 yangfu。

---

## 7. 与 `docs/engine-flow-v2.md` 的关系

- `engine-flow-v2.md` 描述的是 V2/主线的**完整同步内核**（含 resolve_conflict / fix_build /
  fail_fast 全部 agent 能力），其中「cron 入口」段落与 689d6cf 之后的实现**已不一致**。
- 本文档只讲**本分支定制的 cron 精简链路**，是最新 cron 行为的权威说明。
- 若需 manual 完整 agent 流程细节，仍以 `engine-flow-v2.md` + `single_target.py` 为准。
