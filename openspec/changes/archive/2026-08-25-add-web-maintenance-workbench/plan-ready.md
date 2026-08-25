# 实现计划：add-web-maintenance-workbench

## 来源

- 项目配置：`openspec/config.yaml`
- 当前规格：`openspec/specs/`（decision / detection / reliability / reporting / safety / sync-execution）
- 提案：`openspec/changes/add-web-maintenance-workbench/proposal.md`
- 设计：`openspec/changes/add-web-maintenance-workbench/design.md`
- 规格：`openspec/changes/add-web-maintenance-workbench/specs/`
- 任务：`openspec/changes/add-web-maintenance-workbench/tasks.md`

## Project Context

- 目的：Branch Sync Agent 定期检测 RCIOS 代码库各分支合入 commit，判断是否需同步；在独立 Git worktree 中完成 cherry-pick、冲突处理、多型号编译验证、diff-patch 生成，输出 HTML 报告 + 邮件。Agent 只做复杂判断，Workflow 管流程、Tool 管执行。
- 技术栈：Python 3.13 + uv；LangGraph（单周期 = 单 thread_id = 单 checkpoint，SQLite checkpoint）；OpenAI 兼容 LLM 接口；pydantic 数据模型；PyYAML。
- 目标仓库：嵌入式 C（RCIOS），GitLab SSH；branch.md 同源矩阵；单 repo。
- 构建环境：Docker 容器内编译（`sudo docker run ... rcios-build-env:ubuntu24.04`），型号矩阵 required_models。
- 架构：主图只控流程与状态；3 个 Agent 子图只在复杂判断点介入；GitService/BuildRunner 确定性执行层；规则层先于 LLM；四态结论（NeedSync/AlreadyIncluded/ManualReview/OutOfScope）。
- 容错：节点级隔离、LLM 级（timeout/重试/降级安全默认）、Tool 级（timeout/幂等/git 真实状态为准）、周期级（单分支失败不拖垮整周期）。
- 测试：pytest + ruff；git 操作可 mock；已有 `tests/fixtures/mini_repo.py` 与 fake executor 基建。
- 约束：只生成 patch 不推送远程；只操作用独立 worktree；空检也发邮件。

## Applicable OpenSpec Rules

- 引用现有 spec 再描述新行为，不得凭空发明；每条需求必须有具体场景和验收条件。
- 保持既有架构模式（Workflow 管流程、Agent 管判断、Tool 管执行）。
- 每个实现任务以测试通过为完成条件；给出精确文件、测试、验证命令和回滚说明。
- 任何 node 出错不得让整个 workflow 崩溃，异常写入 state 并走报告；LLM 调用必须有 timeout+重试+降级安全默认；不把真实路径/密钥硬编码，用占位符 + 环境变量注入。
- 开发按 V1 阶段要求与契约执行：TDD（RED-GREEN-REFACTOR）、ruff、不破坏 V1 既有行为与五道闸门安全模型。

## Goal

V2 Web 分支维护工作台交付：维护人员浏览器登录后查看实时工作台、历史报告与详情，触发同步/重跑、处理人工项、在四道闸约束下人工触发推送并留痕；V1 新增分支级 sync/rerun、override、投影 CLI、全局 flock、运行中标记与 WAL，Agent 与平台均无自动推送。

## Non-Goals

- 不实现审批链、多仓库管理、平台内文件编辑、旧现场重建/重放。
- 不接公司 LDAP/SSO（初版 env 静态账号，接口留替换位）。
- 不做平台自动推送、不做 `--force` 推送。
- 不改造 V1 主图整周期行为（仅新增分支级入口与基础设施）。

## Source Coverage

| OpenSpec 来源 | 验收点 | 对应 slice |
|---------------|--------|-----------|
| `reliability` / 全局执行串行化 | flock 串行 cron/sync/rerun/override/清理/推送；进程退出自动释放 | Slice 1 |
| `reliability` / 周期运行中标记 | cycle.json 运行中即写，结束覆盖终态 | Slice 1 |
| `reliability` / checkpoint 库并发读安全 | state.sqlite3 WAL，投影并发读不锁 | Slice 1, Slice 2 |
| `reporting` / 结构化状态投影 CLI | `bsa report <cycle> --json` 完整结构化输出；运行中只 `{status: running}` | Slice 2 |
| `reporting` / 投影为只读入口 | 投影只读不改状态，平台不解析 checkpoint 内部 | Slice 2, Slice 7 |
| `decision` / 覆盖判定 CLI | `bsa override` 写 judgments.json 持锁 | Slice 3 |
| `decision` / 覆盖生效时机 | 下次决策生效，不追溯 decisions.json | Slice 3, Slice 10 |
| `sync-execution` / 分支级同步 CLI | `bsa sync <src> <target>` 与 `--sha` 直同步 | Slice 4 |
| `sync-execution` / 分支级重跑 CLI | `bsa rerun` 保留现场续跑 / `--fresh` 重判重建；dirty 拦截 | Slice 5 |
| `sync-execution` / 超期即弃 | 超期分支只读+重同步，无续做/推送 | Slice 5, Slice 7, Slice 11 |
| `safety` / 平台推送四道闸 | SUCCESS/worktree 存在/非 forbidden_branches/status 干净 | Slice 11 |
| `safety` / 平台推送执行约束 | `git push origin HEAD:<target>`，禁 `--force`，受限执行器 | Slice 11 |
| `safety` / 超期即弃安全边界 | 超期分支无推送入口 | Slice 11 |
| `web-platform` / 认证与会话 | 登录/登出/会话过期/CSRF/cookie 安全/登录限速 | Slice 6 |
| `web-platform` / 认证后端可替换 | Authenticator 接口，env 静态账号 | Slice 6 |
| `web-platform` / 角色与授权 | 查看者只读、操作者操作、每端点校验 | Slice 6 |
| `web-platform` / 实时工作台首页 | 待办区/实时总览/快速操作/Agent 状态/运行中徽章/不缓存 | Slice 7 |
| `web-platform` / 历史报告与详情查看 | 历史按日期+结论筛选；详情独立路由按需加载 | Slice 8 |
| `web-platform` / 手动触发同步与重跑 | 异步任务/排队/并发拒绝/轮询进度 | Slice 9 |
| `web-platform` / 人工项处理 | 改判定/确认继续/放弃，操作留痕 | Slice 10 |
| `web-platform` / 推送操作 | 四道闸/确认框/HEAD:target/禁 force/回显/留痕/无自动推送 | Slice 11 |
| `web-platform` / 操作日志与审计 | 敏感操作留痕；append-only；V1 标识符关联聚合 | Slice 12 |
| `web-platform` / 数据保留分级 | L1 永久 / L2 30 天可配 / L3 仅当前周期 | Slice 13 |
| `web-platform` / 备份与恢复 | 每日备份 7 份；RPO≤1 天；恢复不依赖 worktree | Slice 13 |
| `web-platform` / 可观测性与部署 | 健康检查/结构化日志/logrotate/schema_version/8888 端口 | Slice 14 |
| `web-platform` / 平台自身数据存储 | 平台独立 SQLite；关联 V1 标识符；读经投影写经 CLI | Slice 6, Slice 9, Slice 12 |
| `tasks.md` A1–A7 | V1 基础设施与分支级命令 | Slice 1–5 |
| `tasks.md` B1–B2 | 平台骨架 + 认证角色 | Slice 6 |
| `tasks.md` C1–C2 | 工作台 + 历史详情 | Slice 7–8 |
| `tasks.md` D1–D5 | runner/触发/人工项/推送/审计 | Slice 9–12 |
| `tasks.md` E1–E3 | 保留/备份/可观测/端到端 | Slice 13–14 |

## File Responsibility Map

| 文件 | 操作 | 责任 | 相关 slice |
|------|------|------|------------|
| `src/bsa/executor/lock.py` | create | 全局 flock 上下文管理器 | Slice 1 |
| `src/bsa/scheduler/cycle.py` | modify | flock 包裹、运行中标记 cycle.json | Slice 1 |
| `src/bsa/graph/workflow.py` | modify | checkpointer WAL + busy_timeout | Slice 1 |
| `src/bsa/report/projection.py` | create | 投影 CLI 逻辑（checkpoint → JSON） | Slice 2 |
| `src/bsa/commands/override.py` | create | override CLI（写 judgments 持锁） | Slice 3 |
| `src/bsa/graph/single_target.py` | create | 单 target 子图（复用节点） | Slice 4, 5 |
| `src/bsa/commands/sync.py` | create | sync CLI（源+目标 / --sha） | Slice 4 |
| `src/bsa/commands/rerun.py` | create | rerun CLI（保留现场 / --fresh） | Slice 5 |
| `src/bsa/cli.py` | modify | 注册 sync/rerun/override/report 子命令 | Slice 2–5 |
| `src/bsa_web/app.py` | create | FastAPI 装配 | Slice 6 |
| `src/bsa_web/settings.py` | create | env 配置（含 BSA_WEB_PORT=8888） | Slice 6 |
| `src/bsa_web/db.py` | create | 平台 SQLite + schema_version | Slice 6 |
| `src/bsa_web/auth.py` | create | Authenticator 接口 + env 实现 + 会话 | Slice 6 |
| `src/bsa_web/rbac.py` | create | 角色校验依赖 | Slice 6 |
| `src/bsa_web/projection.py` | create | 投影 CLI 子进程封装 | Slice 7 |
| `src/bsa_web/views/` | create | 工作台/历史/详情/操作日志/设置页 | Slice 7, 8, 12 |
| `src/bsa_web/api/` | create | 触发/轮询/人工项/推送 JSON 端点 | Slice 9, 10, 11 |
| `src/bsa_web/runner.py` | create | 异步任务状态机/子进程/排队 | Slice 9 |
| `src/bsa_web/push.py` | create | 推送四道闸 + 受限 push | Slice 11 |
| `src/bsa_web/audit.py` | create | append-only 审计 | Slice 12 |
| `tests/test_lock.py` 等 | create | V1 各新功能测试 | Slice 1–5 |
| `tests/web/test_*.py` | create | 平台测试 | Slice 6–13 |
| `deploy/bsa-web.service` 等 | create | systemd/nginx/env 示例 | Slice 14 |

## Implementation Slices

### Slice 1: V1 基础设施（flock / 运行中标记 / WAL）
- 来源：`reliability`（全局执行串行化、周期运行中标记、checkpoint 库并发读安全）+ tasks A1–A3
- 目标：为后续所有 V1 分支级命令与平台读取提供锁、运行状态可见性与并发读安全
- 依赖：无
- 改动文件：
  - Create: `src/bsa/executor/lock.py`, `tests/test_lock.py`
  - Modify: `src/bsa/scheduler/cycle.py`, `src/bsa/graph/workflow.py`
- TDD 计划：
  1. 先写 `tests/test_lock.py`（互斥/超时/进程退出释放）→ 实现 `lock.py`
  2. 写 cycle.json 运行中时序测试 → 改 `cycle.py` 开跑即写 running
  3. 写 WAL 并发读写测试 → 改 `workflow.py` `open_checkpointer`
- 验证命令：
  - `uv run pytest tests/test_lock.py tests/test_scheduler_cycle.py tests/test_workflow.py`
  - `uv run ruff check src/bsa/`
- 完成标准：cron 周期运行中可见、flock 互斥与自动释放、投影读取不锁死；既有周期测试全绿
- 风险/回滚：WAL 改动影响 checkpoint——还原 `workflow.py` 即可；运行中标记改变 cycle.json 时序——还原 `cycle.py`

### Slice 2: V1 投影 CLI
- 来源：`reporting`（结构化状态投影 CLI、投影为只读入口）+ tasks A4
- 目标：`bsa report <cycle> --json` 输出平台可消费的结构化状态
- 依赖：Slice 1
- 改动文件：
  - Create: `src/bsa/report/projection.py`, `tests/test_projection.py`
  - Modify: `src/bsa/cli.py`
- TDD 计划：先写完成/运行中/不存在周期 + 字段完整性测试 → 实现投影 → 注册 CLI
- 验证命令：`uv run pytest tests/test_projection.py && uv run ruff check src/bsa/`
- 完成标准：完成周期输出完整 branch_results/decisions/action_required；运行中仅 `{status: running}`；只读
- 风险/回滚：投影依赖 checkpoint 序列化结构——只读连接，改动可独立回滚

### Slice 3: V1 override CLI
- 来源：`decision`（覆盖判定 CLI、覆盖生效时机）+ tasks A5
- 目标：`bsa override <sha> [--is-bug-fix] [--risk]` 写 judgments.json（持锁）
- 依赖：Slice 1
- 改动文件：
  - Create: `src/bsa/commands/override.py`, `tests/test_override.py`
  - Modify: `src/bsa/cli.py`
- TDD 计划：先写写入格式/持锁/可被 SyncDecisionAgent 读取测试 → 实现
- 验证命令：`uv run pytest tests/test_override.py`
- 完成标准：override 写入 judgments.json 且人工覆盖优先；与周期决策写不竞争
- 风险/回滚：judgments 全量替换风险已由持锁规避；独立文件可回滚

### Slice 4: V1 分支级 sync CLI
- 来源：`sync-execution`（分支级同步 CLI）+ tasks A6
- 目标：`bsa sync <src> <target> [--sha ...]` 单 target 执行
- 依赖：Slice 1, 2
- 改动文件：
  - Create: `src/bsa/graph/single_target.py`, `src/bsa/commands/sync.py`, `tests/test_sync_command.py`
  - Modify: `src/bsa/cli.py`
- TDD 计划：先写 `--sha` 直同步、源+目标决策链路、非 NeedSync 停止测试 → 实现单 target 子图 → 注册 CLI
- 验证命令：`uv run pytest tests/test_sync_command.py && uv run pytest tests/test_workflow.py`（不破坏主图）
- 完成标准：单 target 同步结果写入 branch_results[target]，独立 manual-* cycle_id；主图回归全绿
- 风险/回滚：单 target 子图与主图共享节点——复用既有节点函数保证行为一致；新文件可独立回滚

### Slice 5: V1 分支级 rerun CLI
- 来源：`sync-execution`（分支级重跑 CLI、超期即弃）+ `safety`（超期即弃安全边界）+ tasks A7
- 目标：`bsa rerun <target> [--cycle] [--fresh]` 续跑 / 重建重同步
- 依赖：Slice 4
- 改动文件：
  - Create: `src/bsa/commands/rerun.py`, `tests/test_rerun_command.py`
  - Modify: `src/bsa/cli.py`
- TDD 计划：先写保留现场续跑、`--fresh` 重判（AlreadyIncluded 停止）、dirty 拦截测试 → 实现
- 验证命令：`uv run pytest tests/test_rerun_command.py`
- 完成标准：续跑按 git 状态重入并更新 patch；--fresh 先重判不产空 patch；dirty 拦截
- 风险/回滚：续跑依赖 git 真实状态判定——以 git 状态为准，不盲跑；独立文件可回滚

### Slice 6: 平台骨架 + 认证/角色/CSRF
- 来源：`web-platform`（认证与会话、认证后端可替换、角色与授权、平台自身数据存储）+ tasks B1–B2
- 目标：平台可启动、可登录、角色隔离、CSRF/会话安全、平台 DB 落地
- 依赖：Slice 2（投影可调用）
- 改动文件：
  - Create: `src/bsa_web/{app,settings,db,auth,rbac}.py`, `tests/web/test_app.py`, `tests/web/test_auth.py`
  - Modify: `pyproject.toml`（bsa-web 入口）
- TDD 计划：先写健康检查、登录/未登录/过期/CSRF/角色校验测试 → 实现骨架与认证 → 注册入口
- 验证命令：`uv run pytest tests/web/ && uv run ruff check src/bsa_web/`
- 完成标准：`BSA_WEB_PORT=8888` 启动；登录闭环；查看者操作被拒；CSRF 生效
- 风险/回滚：认证为初版过渡实现，接口化隔离；独立包可整体回滚

### Slice 7: 投影封装 + 实时工作台首页
- 来源：`web-platform`（实时工作台首页）+ `reporting`（投影为只读入口）+ `sync-execution`（超期即弃展示）+ tasks C1
- 目标：首页展示实时状态，运行中仅"进行中"，不缓存
- 依赖：Slice 6
- 改动文件：
  - Create: `src/bsa_web/projection.py`, `src/bsa_web/views/workbench.py`, `tests/web/test_workbench.py`
- TDD 计划：先写首页渲染/运行中处理/实时读取测试 → 实现投影封装与路由
- 验证命令：`uv run pytest tests/web/test_workbench.py`
- 完成标准：待办区/实时总览/快速操作/Agent 状态齐全；每次请求实时读
- 风险/回滚：子进程调用投影——超时与失败兜底；独立模块可回滚

### Slice 8: 历史报告与详情页
- 来源：`web-platform`（历史报告与详情查看）+ tasks C2
- 目标：历史按日期+结论筛选；详情独立路由按需加载
- 依赖：Slice 7
- 改动文件：
  - Create: `src/bsa_web/views/history.py`, `src/bsa_web/views/detail.py`, `tests/web/test_detail_pages.py`
- TDD 计划：先写历史筛选、详情字段、按需加载测试 → 实现
- 验证命令：`uv run pytest tests/web/test_detail_pages.py`
- 完成标准：历史三区块+筛选；详情含 patch/diff/编译日志/Agent 修复记录
- 风险/回滚：大文件按需加载防首页变慢；独立模块可回滚

### Slice 9: 异步任务 runner + 触发/重跑 API
- 来源：`web-platform`（手动触发同步与重跑）+ tasks D1–D2
- 目标：任务状态机、排队、并发拒绝、轮询；触发 sync/rerun
- 依赖：Slice 4, 5, 6
- 改动文件：
  - Create: `src/bsa_web/runner.py`, `src/bsa_web/api/operations.py`, `tests/web/test_runner.py`, `tests/web/test_operations.py`
- TDD 计划：先写状态机/超时/并发拒绝/排队测试 → 实现 runner → 触发端点与轮询
- 验证命令：`uv run pytest tests/web/test_runner.py tests/web/test_operations.py`
- 完成标准：queued→running→succeeded/failed 闭环；同 target 并发拒绝；排队可见
- 风险/回滚：子进程超时与清理——communicate(timeout)+退出回收；独立模块可回滚

### Slice 10: 人工项处理
- 来源：`web-platform`（人工项处理）+ `decision`（覆盖生效时机）+ tasks D3
- 目标：改判定/确认继续/放弃端点
- 依赖：Slice 3, 9
- 改动文件：
  - Create: `src/bsa_web/api/manual_review.py`, `tests/web/test_manual_review.py`
- TDD 计划：先写改判定（调 override+留痕）、确认继续（复用 --sha）、放弃测试 → 实现
- 验证命令：`uv run pytest tests/web/test_manual_review.py`
- 完成标准：改判定经 override CLI 且审计留痕；确认继续走直同步；放弃标记
- 风险/回滚：确认继续复用直同步路径——不新增 override 机制；独立模块可回滚

### Slice 11: 推送执行链
- 来源：`safety`（平台推送四道闸、平台推送执行约束、超期即弃安全边界）+ `web-platform`（推送操作）+ tasks D4
- 目标：四道闸 + 确认框 + 受限 push + 回显 + 审计；无自动推送
- 依赖：Slice 7, 9, 12（审计先落表）
- 改动文件：
  - Create: `src/bsa_web/push.py`, `src/bsa_web/api/push.py`, `tests/web/test_push.py`
  - Modify: `src/bsa_web/audit.py`（确保审计先就绪）
- TDD 计划：先写四道闸各失败分支、dirty 拦截、push 成功/非 fast-forward 失败、审计记录测试 → 实现受限 push
- 验证命令：`uv run pytest tests/web/test_push.py`
- 完成标准：四道闸强制；`git push origin HEAD:<target>` 禁 `--force`；确认框展示 commit 范围；审计留痕
- 风险/回滚：推送是最高风险操作——受限执行器参数强校验 + 禁 force；代码审查确认无自动推送
- 回滚：禁用推送端点即可立即止血

### Slice 12: 操作日志与审计
- 来源：`web-platform`（操作日志与审计）+ tasks D5
- 目标：append-only 审计、操作日志页、V1 标识符关联
- 依赖：Slice 6
- 改动文件：
  - Create: `src/bsa_web/audit.py`, `src/bsa_web/views/audit_log.py`, `tests/web/test_audit.py`
- TDD 计划：先写 append-only 约束、留痕内容、页面渲染测试 → 实现
- 验证命令：`uv run pytest tests/web/test_audit.py`
- 完成标准：敏感操作全留痕；应用内无更新/删除入口；日志页可查
- 风险/回滚：审计先行（Slice 11 依赖）；独立模块可回滚

### Slice 13: 数据保留与备份
- 来源：`web-platform`（数据保留分级、备份与恢复）+ tasks E1
- 目标：L2 日志清理 + 每日备份 job
- 依赖：Slice 6
- 改动文件：
  - Create: `src/bsa_web/retention.py`, `src/bsa_web/backup.py`, `tests/web/test_retention.py`
- TDD 计划：先写清理只动 L2、备份内容与 7 份滚动测试 → 实现
- 验证命令：`uv run pytest tests/web/test_retention.py`
- 完成标准：L1 永不删；L2 30 天可配；备份含平台 DB+state.sqlite3+judgments+cycle.json
- 风险/回滚：清理 job 误删——只匹配 L2 模式，禁止触碰 audit 路径；独立模块可回滚

### Slice 14: 可观测性 + 部署配置 + 端到端验证
- 来源：`web-platform`（可观测性与部署）+ tasks E2–E3
- 目标：健康检查、结构化日志、systemd/nginx/env 示例、端到端冒烟
- 依赖：Slice 6–13
- 改动文件：
  - Create: `deploy/bsa-web.service`, `deploy/nginx.conf`, `.env.example`, `docs/` 部署说明
  - Modify: `src/bsa_web/app.py`（健康检查完善）
- 验证命令：
  - `uv run pytest && uv run ruff check src/`
  - 手动冒烟：登录→工作台→触发同步→轮询→（mock）推送→审计页
  - 代码审查：平台无自动推送逻辑、无 `--force`
- 完成标准：全量测试绿；健康检查可用；8888 端口 + nginx 443 反代配置就绪；冒烟通过
- 风险/回滚：部署配置不影响既有 V1 cron；配置错误回滚 deploy 目录即可

## Verification Plan

- 单元/集成：`uv run pytest`（V1 `tests/` + 平台 `tests/web/`），覆盖 flock/运行中标记/WAL/投影/override/sync/rerun/认证/角色/CSRF/任务/推送四道闸/审计/保留
- 静态：`uv run ruff check src/`
- 手动：登录→工作台→触发同步→轮询→（mock）推送→审计页；代码审查确认无自动推送、无 `--force`

## Blockers / Clarifications

- 无

## Superpowers Handoff

- `writing-plans` 必须基于本文件生成 `docs/superpowers/plans/YYYY-MM-DD-add-web-maintenance-workbench.md`
- 详细实现计划必须把本文件的 `## Project Context` 与 `## Applicable OpenSpec Rules` 复制/压缩到 plan header 或专门的 `## 项目规则` 章节，使后续 `executing-plans` 不需要重新读取 OpenSpec 也能遵守项目规范
- 详细实现计划必须遵循 `openspec/config.yaml` 的 `language.artifacts: zh-CN`：自然语言标题、段落、任务名和步骤说明使用中文（模板骨架中文化：`目标`、`架构`、`技术栈`、`文件结构`、`项目规则`、`任务`、`步骤`、`自检`），代码标识符、路径、命令、事件名、OpenSpec schema 标题和协议关键字保持原文
- 详细实现计划必须包含 Superpowers plan header 对应信息（目标/架构/技术栈）+ 文件结构 + 2-5 分钟 checkbox 步骤 + RED-GREEN-REFACTOR 测试节奏 + 精确验证命令 + 自检
- 详细实现计划不得出现 TBD/TODO/"适当处理"/"类似上一步"等占位话术；每个 slice 展开为 checkbox 步骤
- 详细实现计划不得省略 Source Coverage 中的任何验收点
- 端口契约：平台监听 `BSA_WEB_PORT`（默认 **8888**），nginx 对外 443 反代到 127.0.0.1:8888
