# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

Branch Sync Agent (BSA) 为 RCIOS（嵌入式 C 代码库）做**跨分支 bug fix 自动传播**：扫描业务分支上的修复 commit → 判定是否需要同步到主分支 → 在独立 worktree 里 `cherry-pick → 冲突消解 → 分型号 Docker 编译验证 → format-patch 交付物 → HTML 报告 + 邮件`。

**引擎永不 push。** 推送只走平台的受限 executor，人工二次确认触发。

分层架构（每一层不可越界）：**Workflow 管流程 / Agent(LLM) 管判断 / Tool 管执行 / SafetyEnforcer 永远压过 LLM**。

## 常用命令

```bash
uv sync                                  # 安装依赖（Python 3.13，uv 管理，项目环境在 .venv）
uv run pytest                            # 全量测试（约 100s，947 passed / 3 skipped）
uv run pytest tests/test_workflow.py     # 单文件
uv run pytest tests/web/ -k task_center   # 单目录 / 按名字筛选
uv run pytest tests/test_rules.py::test_xxx -x   # 单测试 + 首错即停
uv run ruff check src                    # lint（src 必须零告警）
uv run ruff check --fix .                # 自动修复
```

注意：

- `testpaths = ["tests"]`，`examples/` 是参考实现**不纳入**测试套件。
- `uv run ruff check .` 目前有 68 处告警，全部在 `tests/`(11) `scripts/`(21) `examples/`(36)。`src/` 是干净的——**新增/修改 `src/` 代码必须保持 `uv run ruff check src` 通过**。line-length 100，规则集 `E,F,I,UP,B`。
- 若 shell 里残留 `VIRTUAL_ENV=.../bsa-venv`，uv 会告警并忽略它，用 `.venv`——无害，别去"修"。
- 没有 Makefile / CI 配置，命令就是上面这些。

### V1 引擎 CLI（`bsa = bsa.cli:main`，cron/executor 用 `python -m bsa.cli`）

```bash
bsa validate-config                          # 校验 settings + rules（升级后先跑这个）
bsa run-cycle [--date YYYY-MM-DD] [--since ISO] [--until ISO] [--dry-run]
bsa manual-scan --since ISO --until ISO      # 独立扫描窗口，强制新建 checkpoint
bsa status                                   # 最近周期状态
bsa report <cycle> --json                    # 只读投影（平台消费这个）
bsa commits <src> [--limit N] [--refresh]    # 只读候选 commit 列表
bsa override <sha> [--is-bug-fix true|false] [--risk low|medium|high]
bsa sync <src> <target> [--sha ...] [--since] [--until]
bsa rerun <target> [--cycle <id>] [--fresh]
bsa cleanup-worktree <target> <cycle>
```

### V2 平台

```bash
.venv/bin/uvicorn bsa_web.app:create_app --factory --host 127.0.0.1 --port 8888   # Web
.venv/bin/python -m bsa_web.executor                                              # 执行守护进程
.venv/bin/python -m bsa_web.retention --log-dir logs --backup-dir bak --keep-days 30 --keep 7
curl -fsS http://127.0.0.1:8888/healthz      # {"status":"ok"}，DB 坏则 503
python scripts/build_demo_data.py            # 造 logs-demo/ 全状态假数据供 UI 走查
```

## 架构：两个进程，靠共享 SQLite + 文件系统通信

`src/bsa/`（V1 引擎，LangGraph）与 `src/bsa_web/`（V2 平台，FastAPI）**完全解耦**：Web 进程从不 spawn CLI、不持有执行线程，它只往 `tasks` 表写一行 `queued`，再读回状态。

一次用户操作的完整链路：

```
UI → enqueue_task() 写 tasks(queued)
   → bsa-executor 守护进程 claim 循环（0.2s 轮询 + 原子 UPDATE 防重复认领）
   → Popen("python -m bsa.cli <sub>", env={BSA_TASK_ID, BSA_CYCLE_ID, LOG_DIR})
   → 引擎的 task_reporter 凭 BSA_TASK_ID 直接把终态写回同一行
   → UI 每 2s 整页刷新轮询（无 HTMX / 无 SSE），并 tail logs/tasks/task-<id>.log
```

executor 只在引擎没写终态时（超时、配置错误早退）兜底写终态。三处 flock 保证单例：web 锁 `platform.sqlite3`、executor 锁 `executor.lock`、引擎全局锁 `bsa.lock`。`tasks` 表两个 partial unique index 保证「一个 target 一个活动任务」「同时只有一个 cycle 任务」。

### 引擎（`src/bsa/`）

依赖自底向上：`executor/`（subprocess / fake / git 白名单 / flock）→ `git/service.py` → `rules/`（YAML 驱动的确定性规则）→ `agents/`（LLM）→ `graph/`（LangGraph）→ `build/` `ledger.py` `scheduler/` `commands/` `report/` `mail/`。

`graph/factory.py` 是唯一装配真实服务的组合根；`GraphContext`（`graph/nodes.py`）持有全部注入服务 + 可注入的规则函数，测试据此完整 mock 掉 LLM/git/build。

节点序列（`graph/workflow.py`）：

```
detect_commits → sync_decision → next_branch → prepare_worktree → baseline_build
  → next_commit → cherry_pick → resolve_conflict(CONFLICT 时) → build ⇄ fix_build
  → fail_fast(重试耗尽) → generate_patch → 回到 next_branch → … → report
```

**所有循环都是读 state 的条件边，绝不在节点里写 Python for 循环。** `state["status"]` 字符串是路由主干（`DETECTED / PREPARED / BASELINE_OK / CHERRY_PICK_CONFLICT / BUILD_FAILED / FAILFAST_STOP / PATCHED / FAILED` …）。

`graph/single_target.py` 是给 CLI `sync`/`rerun` 用的子图：跳过 detect/decision（命令层已判完），复用 `workflow.py` 里同一批节点函数与路由，语义完全一致。

### 平台（`src/bsa_web/`）

- `app.py` — 组合根。`SECRET_KEY` 为空直接拒绝启动（随机 key 会破坏多 worker session）。单例 flock 必须在 `init_db` 之前拿，否则第二个实例的 `_recover_stale_tasks` 会误杀正在跑的任务。
- `db.py` — 单个 `platform.sqlite3`（WAL + `busy_timeout=5000`，因为 web/executor 是两个进程）。`autocommit=True` 是刻意的：一条连接跨请求线程共享。迁移是手写幂等 DDL（`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`），没有迁移框架。
- `views/` 是 Jinja 服务端渲染页面，`api/` 是 JSON 端点。**没有 HTMX**，`_*.html` 只是 `{% include %}` 片段，交互用原生 `fetch()`。唯一的实时通道是 WebSSH 的 WebSocket。
- `projection.py` — 每次渲染都 `subprocess` 调 `bsa report <cycle> --json`，**无缓存**，任何失败返回 `None` 降级。
- `push.py` — 四道闸门（分支 SUCCESS / worktree 三重绑定校验 / 不在 forbidden_branches / worktree clean），`PushExecutor` 只允许 `git push origin HEAD:<target>` 这一种 argv 形状。
- `ssh.py` — 失败分支的救援终端，按需拉起 ttyd（随机端口、仅 loopback、单客户端、30 分钟 idle 上限、锁死在该 worktree）。

## 硬性不变量（写死语义，不可违反）

1. **cron 周期的同步拓扑由 `CRON_BRANCH_FILE` 指向的分支文件章节标题写死**，不从分支名推断：标题含「主分支」= target（建 worktree、基线编译、收 commit），含「业务分支」= source（只扫描判定，永不编译/建 worktree）。**业务 → 主 单向；业务之间不同步；主不回灌业务；未标注的章节不产生任何同步边（安全默认）。** 该语义只有两个消费者，都在 cron 链路：`nodes.py:_load_matrix` 与 `detect_commits`。
   **`BRANCH_FILE` 是完整分支清单**，服务手动同步的编译型号解析（`commands/sync.py:resolve_target_models`）与平台工作台下拉（`bsa_web/branches.py`），**不参与 cron 配对**。手动 `bsa sync` / `bsa rerun` 从不构造同源矩阵，用合成 `BranchRef(section="manual")` 走 `conclude_pair` 的通用关联性判断，因此可同步完整清单里的任意合规分支对。`CRON_BRANCH_FILE` 留空则回退 `BRANCH_FILE`（老部署零改动兼容）；矩阵解析为空时 `detect_commits` 写 `errors`，绝不静默空跑。
2. **Agent/平台永不 push。** 推送仅经平台受限 executor + 人工二次确认；`--force` 禁止；non-fast-forward 必须干净失败。
3. **SafetyEnforcer 压过 LLM。** `forbidden_paths` / `forbidden_branches` / `max_single_edit_lines` / `required_models` 由代码在 LLM 产出 diff **之后**强制执行。
4. **git 白名单**（`executor/whitelist.py`）：只放行 fetch/checkout/cherry-pick/log/diff/show/format-patch/worktree/merge-base/status/add/rev-parse/diff-tree。docker 命令**刻意绕过**白名单（走裸 SubprocessExecutor），两条链路不可混用。
5. **四态判定 `NeedSync / AlreadyIncluded / ManualReview / OutOfScope` 只出自确定性规则**，LLM 从不产出四态、从不覆盖规则层的 risk。优先级：人工覆盖（`judgments.json`，按 fingerprint 匹配）> 规则 > LLM。**人工覆盖作用于 classify 层的分类与风险（`is_bug_fix`/`risk`）**，压过规则与 LLM；**四态客观结论（已包含/不同产品线）由规则层按 git 快照独立判定，不接收人工覆盖**——人工对四态的拍板走「确认继续（直同步）/放弃」，而非 override。`is_bug_fix` 键缺失的 judgment（只写 risk）不覆盖 `is_bug_fix`，继续走机器规则/LLM。
6. **LLM 不可用 = 节点失败 = 转人工**，统一约定，工作流层没有"重试恢复"语义。未判定的 commit 一律落 ManualReview，绝不自动同步。
7. **回滚先行。** ConflictAgent（字节级快照）与 BuildAgent 在任何 LLM 改动前必先快照、失败必还原，worktree 永不留半成品。
8. **基线编译先行。** `prepare_worktree → baseline_build` 先对未改动的目标 tip 做一次 clean 全编译；失败则整个分支 `FAILED` + 批次全部记 ledger `blocked`，不做任何 cherry-pick。这消灭了 `pre_existing` 这一归因类别。
9. **双身份。** 别只信 SHA：`patch_id`（`git patch-id --stable`，rebase 后仍稳定）驱动 ledger 幂等与 AlreadyIncluded；`fingerprint = sha256(subject+body+patch_id)` 驱动人工覆盖跨 SHA 漂移存活（内容一变 fingerprint 就变，覆盖自动失效）。
10. **台账幂等。** append-only `ledger.json`，`(patch_id, target_branch)` 命中 `synced` 就在建快照/调 LLM **之前**短路成 AlreadyIncluded。
11. **互斥。** 全局 `bsa.lock` flock 串行化 cycle / sync / rerun / override / cleanup / push。`locks/branch/` 的按分支锁已实现但**未接入工作流**（全局锁在，冗余）。
12. **节点异常永不冒泡。** `node_wrapper` 把任何异常转成 `state.errors` + `status=FAILED` 并路由到 report，工作流不许崩。
13. **绝不静默猜测。** 产品线未配编译脚本 → `BuildConfigError` 停任务并把 target 移出批次，绝不猜脚本。
14. **`timeout ≠ 操作没发生。`** rebase/cherry-pick/build 超时后必须查真实 git/产物/容器状态再决定是否重试。
15. **超期即弃。** worktree 只在当前周期有效，没有从 patch 重建旧 worktree 的机制；过期分支只读 + `bsa rerun --fresh`。
16. **扫描窗口固定 22:00–22:00**（+2h 沉淀缓冲），用 committer date 而非 author date；merge commit 经 `--first-parent` 展开后按 patch-id 去重。
17. **cherry-pick EMPTY = 已应用 = 跳过编译，不是错误。** commit 顺序永远按 merge 时间升序，与用户勾选顺序无关。
18. **编译修复只落在本分支的 patch 里**，绝不作为 bug-fix 跨分支同步；每次 LLM 改文件都要产出独立 diff 存审计。

## log_dir 磁盘布局

```
log_dir/
├── state.sqlite3            # LangGraph checkpoint（thread_id == cycle_id）
├── platform.sqlite3         # tasks/sessions/audit_log/abandons/ssh_sessions（与 bsa_web 共享）
├── ledger.json              # append-only (patch_id, target) 同步台账
├── judgments.json           # 人工覆盖 {sha: {...}, "fp:<fingerprint>": {...}}
├── bsa.lock / executor.lock / locks/branch/<b>.lock
├── patch/<cycle_id>_<target>.patch
└── <cycle_id>/              # cycle-YYYY-MM-DD | scan-<since>-<until> | manual-<ts>-<pid>
    ├── cycle.json           # 周期终态、report_path、mail_status
    ├── state.json           # 结构化投影，平台读这个
    ├── decisions.json / report.html / run.log
    ├── audit/<target>/<sha>_conflict.diff, <sha>_<model>_fix.diff
    └── build/<target>/{baseline/<model>.log, <sha>/build.log}
```

## 概念对齐

- **cycle** = 一次完整 LangGraph 调用 = checkpoint thread_id。`cycle-YYYY-MM-DD`（每日）/ `scan-*`（manual-scan）/ `manual-*`（单目标 sync）/ `rerun-*`（保留现场续跑，走独立 thread 以免覆盖源周期投影）。
- **task** = `tasks` 表一行（`kind ∈ cycle|sync|rerun`），平台侧执行状态的唯一真相。桥梁是 `cycle_id`：登记时算一次，checkpoint thread、worktree 命名、报告路径全部对齐它。
- **终态分五层**：判定四态 → commit 级（cherry_pick OK/CONFLICT/EMPTY/FAILED，build OK/FAILED/SKIPPED）→ 分支级 `SUCCESS/PARTIAL/FAILED/MANUAL` → 周期级（有 errors 或任一分支 FAILED/MANUAL → FAILED；任一 PARTIAL → PARTIAL；否则 SUCCESS）→ 任务级二元（周期 SUCCESS 才算 `succeeded`，PARTIAL/FAILED 一律 `failed`）。

## 配置

全部走环境变量 + `pydantic-settings`，**真实路径/密钥绝不硬编码**，文档里一律用占位符。`.env.example` 是权威清单。

- V1（`bsa/config/settings.py`）：`REPO_PATH` `BRANCH_FILE`（完整清单）`CRON_BRANCH_FILE`（cron 专用标注文件，空则回退 `BRANCH_FILE`，见不变量 #1）`WORKTREE_ROOT` `LLM_*` `DOCKER_*` `BUILD_SCRIPT_DIR` `MAIL_*` `LOG_DIR` 等。
- V2（`bsa_web/settings.py`）：`SECRET_KEY`（必填）`BSA_USERS`（JSON，`username -> "bcrypt_hash:role"`，role ∈ admin|operator|viewer；**仅首启引导入 `users` 表，此后经管理员「设置」页管理**，admin 是 operator 超集）`BSA_SIGNUP_PASSWORD`（可选，自助登记共享口令，空则关闭）`BSA_SIGNUP_ADMIN_USERS`（可选，登记白名单建 admin）`BSA_WEB_PORT` `SESSION_TTL_SEC` `COOKIE_SECURE` `LOG_DIR`。
- 两者只在 `LOG_DIR` 和 `BRANCH_FILE` 重叠——两个进程必须指向同一个 `LOG_DIR`，所以两个 systemd unit 共用 `WorkingDirectory` 以读同一份 `.env`。
- 运行期注入（executor 设置，别自造）：`BSA_TASK_ID` `BSA_CYCLE_ID` `BSA_MANUAL_CYCLE_ID` `BSA_RERUN_THREAD_ID`。
- `required_models`（型号）**刻意不在 Settings 里**，唯一权威来源是 `safety_rules.yaml`。

⚠️ `docs/deploy-v2-web.md` 描述的是系统级部署（`/srv/bsa`、`sudo systemctl`、22:00 crontab），但实际 `deploy/*.service` 是**用户级** unit，且日构建由 executor 内建调度器在**本地 0:00** 触发（cron 已被取代）。以 `deploy/*.service` + `docs/v2-web-platform-requirements.md` 为准。

## 约定

- **语言**：面向人类的一切（文档、注释、docstring、CLI help、报错文案、openspec 产物、commit message）用中文；代码标识符、CLI 命令名、OpenSpec 结构标题、协议关键词保持英文原形。
- **commit**：Conventional Commits + 中文正文，scope 用 `engine|web|v1|v2|webssh|openspec|deploy|skill`，例：`fix(engine): SyncDecisionAgent fingerprint 漏接（§5.1 完整）`。
- **openspec 是本仓的变更流程**（`openspec/config.yaml`，`schema: spec-driven`）。每个变更产出 `proposal.md → specs/<capability>/spec.md`(delta) `→ design.md → tasks.md → plan-ready.md → workflow-status.md`，完成后归档到 `openspec/changes/archive/`。slash 命令在 `.opencode/commands/`：`/opsx-explore`（只思考）`/opsx-propose`（建变更+产物，只规划）`/opsx-update` `/opsx-apply`（落实现）`/opsx-sync`（delta 并回主 spec）`/opsx-archive`。当前 spec 在 `openspec/specs/{decision,detection,reliability,reporting,safety,sync-execution,web-platform,webssh}/spec.md`。
- **`examples/branch-maintenance/`** 是参考实现（前身项目），只读参考，不纳入测试、不受 lint 门禁约束。
- **`spec/branch.md`** 是完整分支清单（按产品线分章节，无主/业务标注）；**`spec/branch-cron.md`** 是 cron 专用的标注文件。两者都是占位样例，真实文件由 `BRANCH_FILE` / `CRON_BRANCH_FILE` 指定。
- **`build_rules.yaml` 的 `build_models_by_section` 键必须互不为子串**，且不得被同一个 section 同时命中：cron 侧解析出的是产品键（如 `4.34`），手动侧是章节全称（如 `1.1 组网产品分支`），匹配为双向子串。命中多个时 `resolve_build_models` 报错停止，绝不按书写顺序猜。
