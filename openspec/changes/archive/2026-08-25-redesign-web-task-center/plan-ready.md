# 实现计划：redesign-web-task-center

## 来源

- 项目配置：`openspec/config.yaml`
- 当前规格：`openspec/specs/`（decision / detection / reliability / reporting / safety / sync-execution / web-platform）
- 提案：`openspec/changes/redesign-web-task-center/proposal.md`
- 设计：`openspec/changes/redesign-web-task-center/design.md`
- 规格：`openspec/changes/redesign-web-task-center/specs/`
- 任务：`openspec/changes/redesign-web-task-center/tasks.md`

## Project Context

- 目的：Branch Sync Agent 定期检测 RCIOS 分支合入 bug-fix，在独立 Git worktree 中完成 cherry-pick、冲突处理、多型号编译验证、diff-patch 生成，输出 HTML 报告 + 邮件。Agent 只做复杂判断，Workflow 管流程、Tool 管执行。
- 技术栈：Python 3.13 + uv；LangGraph（单周期 = 单 thread_id = 单 checkpoint，SQLite checkpoint）；FastAPI + Jinja2 SSR；pydantic；pytest + ruff；uvicorn。
- 目标仓库：嵌入式 C（RCIOS），GitLab SSH；branch.md 同源矩阵（RCIOS 仓库路径 `rcios`）；单 repo。
- 架构：主图只控流程与状态；3 个 Agent 子图只在复杂判断点介入；GitService/BuildRunner 确定性执行层；规则层先于 LLM；四态结论（NeedSync/AlreadyIncluded/ManualReview/OutOfScope）。
- 平台：独立进程，读经投影 CLI（`bsa report`/`bsa commits`）、写经 V1 CLI 子进程；本地 SQLite（会话/审计/tasks）；四道闸推送（禁 `--force`）；全局 flock 串行化；超期即弃（worktree 仅当前周期）。
- 测试：pytest + ruff；git 操作可 mock；tests/web 用 TestClient + fake projection + FakeExecutor。
- 约束：Agent/平台永不自动推送；不硬编码真实路径/密钥（env 注入）；人类可读文案中文、代码标识符英文。

## Applicable OpenSpec Rules

- 引用现有 spec 再描述新行为；每条需求有具体场景和验收条件。
- 保持既有架构模式（Workflow 管流程、Agent 管判断、Tool 管执行；平台读经投影/写经 CLI）。
- 每个实现任务以测试通过为完成条件；给出精确文件、测试、验证命令和回滚说明。
- 推送四道闸 + 禁 `--force` 硬约束；WebSSH 受限（仅 worktree、禁 sudo/出目录、复用鉴权、审计）。
- 开发按 V1 阶段要求与契约执行：TDD（RED-GREEN-REFACTOR）、ruff、不破坏既有行为与安全模型。

## Goal

将 Web 平台信息架构重构为"B 新建同步（纯发起）/ A 任务中心（唯一状态与处理端）"：B 区引导式发起（源分支→候选 commit 多选→目标分支，用户手动=无需决策）并自动衔接进 A；A 区按分支任务面板展示最近一个周期内的自动+手动任务，任务详情页聚合重跑/推送/放弃·恢复/人工项处理，并为失败/停批且有现场的任务提供受限 WebSSH 在线处理；V1 新增只读 `bsa commits <src>`。

## Non-Goals

- 不提供平台内文件编辑器（WebSSH 终端替代）。
- 不做审批链、多仓库管理、自动推送、`--force`。
- 不改变 V1 周期检测/决策/同步核心行为（仅新增只读 commits CLI）。
- 任务中心仅列最近一个周期窗口内的任务；完整历史由历史报告页承担。

## Source Coverage

| OpenSpec 来源 | 验收点 | 对应 slice |
|---------------|--------|-----------|
| `reporting` / 候选 commit 只读 CLI | `bsa commits <src>` 返回最近 N 条 sha/message/time，只读，失败容错 | Slice 1 |
| `web-platform` / 放弃·恢复 | abandons 持久化，任务列表过滤联动，可恢复，审计 | Slice 2 |
| `web-platform` / 新建同步候选数据源 | `GET /api/commits?src=` 调 `bsa commits`，加载失败提示 | Slice 3 |
| `web-platform` / 引导式新建同步 | 源分支→commit 多选→目标分支；用户手动=无需决策；勾选即直同步 | Slice 3 |
| `web-platform` / 发起后自动衔接 | 提交成功→自动刷新进 A 手动区块 | Slice 3 |
| `web-platform` / 任务中心 | 自动/手动分区、分支任务面板、最近一周期窗口、状态徽章、已放弃过滤 | Slice 4 |
| `web-platform` / 任务详情页承载操作 | 点击任务→详情页聚合重跑/推送/放弃·恢复/人工项/WebSSH | Slice 4 |
| `web-platform` / 重跑语义 | 瞬态直接重跑；SSH 处理后保留现场续跑；--fresh 重建重判；超期即弃 | Slice 4 |
| `webssh` / 受限终端入口 | 仅 worktree、禁 sudo/出目录、固定入口 cd worktree | Slice 5 |
| `webssh` / 开放范围 | 仅 FAILED/PARTIAL/MANUAL 且有 worktree 开放；ManualReview 不开 | Slice 5 |
| `webssh` / 鉴权与审计 | 复用登录态、会话级+脱敏命令审计、空闲超时 | Slice 5 |
| `web-platform` / 推送对手动 SUCCESS 开放 | 自动+手动 SUCCESS 现场均有推送键，四道闸不变 | Slice 6 |
| `tasks.md` A1–F2 | 全部实现任务 | Slice 1–6 |

## File Responsibility Map

| 文件 | 操作 | 责任 | 相关 slice |
|------|------|------|------------|
| `src/bsa/commands/commits.py` | create | `bsa commits <src>` 只读候选 commit CLI | Slice 1 |
| `src/bsa/cli.py` | modify | 注册 commits 子命令 | Slice 1 |
| `tests/test_commits_command.py` | create | 列表/limit/容错/只读 | Slice 1 |
| `src/bsa_web/db.py` | modify | abandons 表 + schema 迁移 | Slice 2 |
| `src/bsa_web/api/abandon.py` | create | 放弃/恢复端点 + 审计 | Slice 2 |
| `tests/web/test_abandon.py` | create | 放弃/恢复/唯一约束/权限/审计 | Slice 2 |
| `src/bsa_web/api/operations.py` | modify | `GET /api/commits` 候选加载；sync 支持 `shas` | Slice 3 |
| `src/bsa_web/views/workbench.py` | modify | B 区三步表单上下文；A 区任务中心聚合 | Slice 3, 4 |
| `src/bsa_web/templates/workbench.html` | modify | 三步表单 + 任务中心面板 | Slice 3, 4 |
| `src/bsa_web/templates/task_detail.html` | create | 任务详情页聚合操作 | Slice 4 |
| `src/bsa_web/views/task_detail.py` | create | 任务详情路由 `/task/<cycle>/<target>` | Slice 4 |
| `tests/web/test_new_sync.py` | create | commit 加载/提交/失败容错 | Slice 3 |
| `tests/web/test_task_center.py` | create | 聚合/窗口过滤/徽章/放弃过滤 | Slice 4 |
| `src/bsa_web/ssh.py` | create | WebSSH 鉴权/审计/生命周期 | Slice 5 |
| `deploy/` | modify | WebSSH 部署/环境（ttyd 或自研） | Slice 5 |
| `tests/web/test_webssh.py` | create | 授权/鉴权/审计/脱敏 | Slice 5 |
| `tests/web/test_push.py` | modify | 手动 SUCCESS 推送键 | Slice 6 |

## Implementation Slices

### Slice 1: V1 `bsa commits <src>` 只读 CLI
- 来源：`reporting`（候选 commit 只读 CLI）+ tasks A1
- 目标：B 区候选 commit 数据源
- 依赖：无
- 改动文件：
  - Create: `src/bsa/commands/commits.py`, `tests/test_commits_command.py`
  - Modify: `src/bsa/cli.py`
- TDD 计划：
  1. 先写测试（列表结构/limit/失败容错/只读）→ 确认 FAIL
  2. 实现 `bsa commits <src> [--limit N]`（git log origin/<src>，JSON 输出 sha/message/committed_at）
  3. 注册 CLI
- 验证命令：
  - `uv run pytest tests/test_commits_command.py -v`
  - `uv run ruff check src/bsa/`
- 完成标准：返回最近 N 条（默认 50）平铺，只读，src 不存在返回非零+错误
- 风险/回滚：独立文件，可整体回滚

### Slice 2: 放弃/恢复
- 来源：`web-platform`（放弃/恢复）+ tasks B1–B2
- 目标：放弃持久化 + 任务列表联动过滤
- 依赖：无（平台既有 tasks/审计基建）
- 改动文件：
  - Modify: `src/bsa_web/db.py`（abandons 表 + 迁移）
  - Create: `src/bsa_web/api/abandon.py`, `tests/web/test_abandon.py`
  - Modify: `src/bsa_web/views/workbench.py`（列表过滤）
- TDD 计划：
  1. 写测试（放弃/恢复/唯一约束/重复幂等/权限/审计；过滤联动）→ FAIL
  2. 实现 abandons 表 + API + 过滤
- 验证命令：`uv run pytest tests/web/test_abandon.py`
- 完成标准：放弃项不再进待处理/可推送，恢复后重新出现；审计留痕
- 风险/回滚：db 迁移用 PRAGMA table_info + ALTER，兼容旧库

### Slice 3: B 区新建同步 + 自动衔接
- 来源：`web-platform`（引导式新建同步、候选数据源、自动衔接）+ tasks C1–C2
- 目标：三步引导表单，用户勾选=无需决策，发起后自动进 A
- 依赖：Slice 1（bsa commits）
- 改动文件：
  - Modify: `src/bsa_web/api/operations.py`（`GET /api/commits?src=`、sync 支持 shas）
  - Modify: `src/bsa_web/views/workbench.py`, `templates/workbench.html`（三步表单 + JS）
  - Create: `tests/web/test_new_sync.py`
- TDD 计划：
  1. 写测试（commit 加载端点/提交参数/失败容错/权限/重定向）→ FAIL
  2. 实现候选端点 + 表单交互 + 提交（POST /api/sync {src,target,shas} 直同步）+ 自动跳转
- 验证命令：`uv run pytest tests/web/test_new_sync.py tests/web/test_operations.py`
- 完成标准：源分支→commit 多选→目标分支→发起→自动进 A 手动区块
- 风险/回滚：表单/JS 独立，端点新增不破坏既有

### Slice 4: A 区任务中心 + 任务详情页聚合
- 来源：`web-platform`（任务中心、任务详情页承载操作、重跑语义）+ tasks D1–D2
- 目标：首页任务中心 + 详情页聚合全部操作
- 依赖：Slice 2（放弃过滤）、Slice 3（手动任务进入 A）
- 改动文件：
  - Modify: `src/bsa_web/views/workbench.py`, `templates/workbench.html`（任务中心两区块/面板）
  - Create: `src/bsa_web/views/task_detail.py`, `templates/task_detail.html`
  - Create: `tests/web/test_task_center.py`
- TDD 计划：
  1. 写测试（自动/手动聚合、窗口过滤、徽章、放弃过滤、详情页操作入口按状态显示）→ FAIL
  2. 实现任务中心聚合（手动 = tasks + manual 投影；自动 = 最新周期投影展开）+ 详情页复用分支详情 + 操作按钮
- 验证命令：`uv run pytest tests/web/test_task_center.py tests/web/test_workbench.py`
- 完成标准：任务中心最近一周期自动+手动分区；详情页按状态/权限显示重跑/推送/放弃/恢复/人工项/WebSSH
- 风险/回滚：workbench 首页重构较大，独立提交、保留历史页

### Slice 5: WebSSH（最大新项，独立评审）
- 来源：`webssh`（受限终端、开放范围、鉴权与审计）+ tasks E1
- 目标：失败/停批且有现场任务的可信受限终端
- 依赖：Slice 4（详情页入口）
- 改动文件：
  - Create: `src/bsa_web/ssh.py`（会话 token/审计/生命周期）
  - Modify: `deploy/`（WebSSH 组件部署配置）
  - Create: `tests/web/test_webssh.py`
- TDD 计划：
  1. 选型确认（ttyd vs xterm.js+websocket+pty，实施前与用户确认）
  2. 写测试（授权：非失败任务无入口；鉴权拒绝；审计写；脱敏）→ FAIL
  3. 实现受限终端（入口 `cd <worktree> && exec bash`、清 PATH sudo、短期 token 换连接、空闲超时）
- 验证命令：`uv run pytest tests/web/test_webssh.py && uv run ruff check src/bsa_web/`
- 完成标准：仅 FAILED/PARTIAL/MANUAL 且有 worktree 任务有 SSH 入口；复用登录态；会话+脱敏命令审计
- 风险/回滚：新组件引入——独立评审；回滚禁用 SSH 入口即可

### Slice 6: 推送对手动 SUCCESS 开放 + 端到端
- 来源：`web-platform`（推送操作）+ tasks F1–F2
- 目标：手动同步 SUCCESS 现场可推送；全量验证
- 依赖：Slice 3, 4
- 改动文件：
  - Modify: `tests/web/test_push.py`（手动 SUCCESS 推送键）
  - 修改点视测试结果（推送键渲染逻辑已支持任何 SUCCESS 投影）
- 验证命令：
  - `uv run pytest && uv run ruff check src/`
  - 手动冒烟：登录 → B 区选源加载 commit → 勾选 → 目标 → 发起 → 自动进 A → 详情 → 失败见 WebSSH →（mock）重跑/推送/放弃恢复
  - 代码审查：无自动推送、无 --force、WebSSH 受限
- 完成标准：自动+手动 SUCCESS 均有推送键；全量测试绿；冒烟通过
- 风险/回滚：无新增安全面

## Verification Plan

- 单元/集成：`uv run pytest`（V1 `tests/` + 平台 `tests/web/`），覆盖 commits CLI/放弃恢复/新建同步/任务中心/任务详情聚合/WebSSH 授权鉴权审计/推送开放。
- 静态：`uv run ruff check src/`
- 手动：登录→B 区引导发起→自动进 A→任务详情→失败任务 WebSSH→（mock）重跑/推送/放弃恢复；代码审查确认无自动推送、无 --force、WebSSH 受限。

## Blockers / Clarifications

- WebSSH 组件选型（ttyd vs 自研 xterm.js+websocket+pty）在 Slice 5 实施前与用户确认。
- 任务中心"最近一个周期窗口"的具体窗口口径（如当前周期 + 上一周期，或按日期近 N 天）在 Slice 4 实施时按最新周期记录推断；如需精确口径在此记录。

## Superpowers Handoff

- `writing-plans` 必须基于本文件生成 `docs/superpowers/plans/YYYY-MM-DD-redesign-web-task-center.md`
- 详细实现计划必须把本文件的 `## Project Context` 与 `## Applicable OpenSpec Rules` 复制/压缩到 plan header 或专门的 `## 项目规则` 章节，使后续 `executing-plans` 不需要重新读取 OpenSpec 也能遵守项目规范
- 详细实现计划必须遵循 `openspec/config.yaml` 的 `language.artifacts: zh-CN`：自然语言标题、段落、任务名和步骤说明使用中文（模板骨架中文化：`目标`、`架构`、`技术栈`、`文件结构`、`项目规则`、`任务`、`步骤`、`自检`），代码标识符、路径、命令、事件名、OpenSpec schema 标题和协议关键字保持原文
- 详细实现计划必须包含 Superpowers plan header 对应信息（目标/架构/技术栈）+ 文件结构 + 2-5 分钟 checkbox 步骤 + RED-GREEN-REFACTOR 测试节奏 + 精确验证命令 + 自检
- 详细实现计划不得出现 TBD/TODO/"适当处理"/"类似上一步"等占位话术；每个 slice 展开为 checkbox 步骤
- 详细实现计划不得省略 Source Coverage 中的任何验收点
- WebSSH 组件选型（ttyd vs 自研）为 Slice 5 的待确认项，进入实现前必须与用户确认，不得默认实现
