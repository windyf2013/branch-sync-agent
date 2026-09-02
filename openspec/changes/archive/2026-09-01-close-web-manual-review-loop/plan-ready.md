# 实现计划：close-web-manual-review-loop

## 来源

- 项目配置：`openspec/config.yaml`
- 当前规格：`openspec/specs/`（web-platform）
- 提案：`openspec/changes/close-web-manual-review-loop/proposal.md`
- 设计：`openspec/changes/close-web-manual-review-loop/design.md`
- 规格：`openspec/changes/close-web-manual-review-loop/specs/`
- 任务：`openspec/changes/close-web-manual-review-loop/tasks.md`

## Project Context

- 目的：Branch Sync Agent V2 Web 工作台——维护人员登录后查看实时状态、审阅决策、执行同步与推送。平台独立进程，与 V1 解耦：读经投影、写经 CLI。
- 技术栈：Python 3.13 + uv；FastAPI + Jinja 服务端渲染；平台独立 SQLite（platform.sqlite3）；无 HTMX，交互走原生 fetch；唯一实时通道 WebSSH WebSocket。
- 架构：`views/` Jinja 页面、`api/` JSON 端点、`projection.py` 每次渲染 subprocess 调 `bsa report --json`（无缓存，失败降级 None）；平台从不 spawn CLI 长任务，只写 tasks 表 queued 由 executor 守护进程认领。
- 关键数据流：UI → enqueue_task 写 tasks(queued) → executor claim → Popen `python -m bsa.cli` → 引擎 task_reporter 回写终态 → UI 每 2s 整页刷新轮询。
- 测试：pytest + ruff；TestClient + fake projection + FakeExecutor 基建。
- 约束：Agent/平台永不 push（推送仅经受限 executor + 人工二次确认）；read 经投影、write 经 V1 CLI。

## Applicable OpenSpec Rules

- 引用现有 spec 再描述新行为，不得凭空发明；每条需求必须有具体场景和验收条件。
- 保持既有架构模式（Workflow 管流程、Agent 管判断、Tool 管执行）。
- 每个实现任务以测试通过为完成条件；给出精确文件、测试、验证命令和回滚说明。
- 不把真实仓库路径/密钥硬编码；平台读经投影、写经 CLI 的边界不可破坏。

## Goal

闭合「人工处理」管理闭环——让 decision 层 ManualReview（无 branch_results）从「不可达」变「可达」，并把状态/拓扑语义从「误导」变「如实」。**及格线：打开现有 `cycle-2026-09-01`（不重跑），工作台「待确认 4」可点、4 个 ManualReview 项能进详情页并完成「确认继续/放弃」产生任务/审计记录；状态徽章如实显示「完成 · 4 待人工」；存量数据缺字段时降级不 404、不误报漏扫。**

## Non-Goals

- 不写离线回填脚本补历史周期缺失字段——存量降级、增量完整。
- 不改引擎、不改任务状态机、不改推送四道闸。
- 不新增 override 机制，只复用 V1 `bsa override`（含 change1 的 `--clear`）。
- 不改 branch_results 结构、不改 `_target_detail.html` 条件渲染逻辑（只放宽 404 条件 + 注入缺省值）。

## Source Coverage

| OpenSpec 来源 | 验收点 | 对应 slice |
|---------------|--------|-----------|
| `web-platform` / decision 层 ManualReview 操作可达 | `/task/{cycle}/{target}` 不 404、渲染人工项操作区 | Slice 1 |
| `web-platform` / 达成度醒目标记 | SUCCESS+待人工显示「完成 · N 待人工」 | Slice 2 |
| `web-platform` / 工作台待办与总览（待确认可点） | 待确认 KPI 与 cycle_summary_row「待确认 N」可点 | Slice 2 |
| `web-platform` / 周期拓扑对照（ADDED） | 完整源/目标清单、零检出标注、字段缺失降级标注 | Slice 3 |
| `web-platform` / 按成因给对 override 控件 | pending→is_bug_fix、severity_gate/fix_missing→risk、其余→无 override | Slice 4 |
| `web-platform` / 清除 override | 调 `--clear`、回到未判定、可重跑 | Slice 4 |
| `web-platform` / 改判定（立即重跑入口） | override 成功后前端渲染「立即重跑该分支」 | Slice 4 |
| `web-platform` / 确认继续语义透明化 | confirm 前提示「发起独立手动同步」 | Slice 4 |
| `tasks.md` T1 | decision 层 ManualReview 操作可达 | Slice 1 |
| `tasks.md` T2 | 待确认可点 + 达成度 | Slice 2 |
| `tasks.md` T3 | 拓扑对照 | Slice 3 |
| `tasks.md` T4 | 按 cause 给控件 + 清除 + 重跑引导 + confirm 透明化 | Slice 4 |

## File Responsibility Map

| 文件 | 操作 | 责任 | 相关 slice |
|------|------|------|------------|
| `src/bsa_web/views/task_detail.py` | modify | branch 为 None 时若有人工项则注入缺省 branch dict，不 404 | Slice 1 |
| `src/bsa_web/views/workbench.py` | modify | 待确认 KPI 可点、达成度计数、拓扑对照数据 | Slice 2, 3 |
| `src/bsa_web/views/detail.py` | modify | 周期概览 ManualReview 项可点、拓扑对照渲染 | Slice 2, 3 |
| `src/bsa_web/templates/workbench.html` | modify | 待确认链接、period-bar「完成 · N 待人工」、拓扑区 | Slice 2, 3 |
| `src/bsa_web/templates/detail.html` | modify | 周期概览 ManualReview 项链接、拓扑对照区 | Slice 2, 3 |
| `src/bsa_web/templates/task_detail.html` | modify | 按 cause 给控件、清除按钮、重跑引导、confirm 透明化文案 | Slice 4 |
| `src/bsa_web/api/manual_review.py` | modify | 清除 override 端点（复用 `--clear`） | Slice 4 |
| `tests/web/test_detail_pages.py` | modify | decision 层 ManualReview 可达、拓扑对照 | Slice 1, 3 |
| `tests/web/test_workbench.py` | modify | 待确认可点、达成度标记 | Slice 2 |
| `tests/web/test_task_center.py` | modify | 拓扑对照零检出/降级标注 | Slice 3 |
| `tests/web/test_manual_review.py` | modify | 按 cause 给控件、清除、重跑引导、confirm 透明化 | Slice 4 |

## Implementation Slices

### Slice 1: decision 层 ManualReview 操作可达

- 来源：`web-platform` / decision 层 ManualReview 操作可达 + tasks T1
- 目标：`branch_results` 无该 target、但 `action_required` 存在该 target 的 ManualReview 项时，`/task/{cycle}/{target}` 降级渲染「仅人工项」视图，不再 404。
- 依赖：无（存量 `action_required` 已含 ManualReview 项，不依赖 change1 新字段）
- 改动文件：
  - Modify: `src/bsa_web/views/task_detail.py`
  - Test: `tests/web/test_detail_pages.py`
- TDD 计划：
  1. 先写失败测试：branch_results 空 + action_required 有 ManualReview 项时，GET `/task/{cycle}/{target}` 返回 200 且渲染人工项操作区；无人工项仍 404。
  2. 最小实现：`branch is None` 时先检查 `action_required` 是否存在 `kind==ManualReview and branch==target`，存在则注入缺省 branch dict（status=MANUAL、worktree_path=""、commits=[]、patch_path=None、baseline=None、stop_reason=None）继续渲染。
  3. 边界：缺省 branch 使 `_target_detail.html` 条件渲染空安全；`show_webssh`/`show_push` 自动 False（不暴露本不存在的操作）。
- 验证命令：
  - `uv run pytest tests/web/test_detail_pages.py -x`
  - `uv run ruff check src/bsa_web/views/task_detail.py`
- 完成标准：`/task/{cycle}/{target}` 对 decision 层 ManualReview 返回 200 且含「确认继续 / 放弃」操作；无人工项仍 404。
- 风险/回滚：只放宽 404 条件 + 注入缺省值，不改 branch_results 结构；回滚 = revert 该 slice。

### Slice 2: 待确认可点 + 达成度醒目标记

- 来源：`web-platform`（达成度醒目标记、工作台待办与总览）+ tasks T2
- 目标：工作台「待确认」KPI 与 cycle_summary_row「待确认 N」变为可点链接；SUCCESS+待人工时 period-bar 与状态徽章显示「完成 · N 待人工」。
- 依赖：Slice 1（可点目标 = 可操作详情页）
- 改动文件：
  - Modify: `src/bsa_web/views/workbench.py`、`src/bsa_web/templates/workbench.html`
  - Test: `tests/web/test_workbench.py`
- TDD 计划：
  1. 先写失败测试：SUCCESS+待人工时达成度标记「完成 · N 待人工」出现；待确认 KPI 渲染为链接。
  2. 最小实现：渲染时计算 `review_pending` 计数（复用 `_build_todo`），SUCCESS 且非空时模板显示达成度标记；KPI 改链接指向周期概览或第一个待处理 target 详情页。
  3. 边界：SUCCESS 且无待人工时维持纯「完成」；N 与待确认 KPI 数值一致。
- 验证命令：
  - `uv run pytest tests/web/test_workbench.py -x`
  - `uv run ruff check src/bsa_web/views/workbench.py`
- 完成标准：现有 `cycle-2026-09-01`（SUCCESS + 4 待人工）工作台显示「完成 · 4 待人工」，待确认 4 可点。
- 风险/回滚：纯呈现层，改计数/模板；回滚 = revert 该 slice。

### Slice 3: 周期拓扑对照

- 来源：`web-platform` / 周期拓扑对照（ADDED）+ tasks T3
- 目标：工作台与周期概览展示完整源/目标清单（含零检出源），区分「零检出」与「漏扫」。
- 依赖：change1 Slice 1（`sources`/`targets` 字段）；字段缺失时降级（不阻塞本 slice 先落地降级路径）
- 改动文件：
  - Modify: `src/bsa_web/views/workbench.py`、`src/bsa_web/views/detail.py`、`templates/workbench.html`、`templates/detail.html`
  - Test: `tests/web/test_task_center.py`、`tests/web/test_detail_pages.py`
- TDD 计划：
  1. 先写失败测试：含零检出源的 payload 显示「本期无新 commit（正常）」标注；字段缺失时显示「拓扑字段缺失，仅展示检出过 commit 的分支」降级标注。
  2. 最小实现：优先读 `payload.sources`/`payload.targets`；零检出源 = `sources 全集 - {c.source_branch}`；字段缺失时从 `detected_commits`/`decisions` 推导 + 降级标注。
  3. 边界：`detail.py` 已有 `sources`/`targets` 兜底（84-87 行），本 slice 在其上加零检出标注与降级标注。
- 验证命令：
  - `uv run pytest tests/web/test_task_center.py tests/web/test_detail_pages.py -x`
  - `uv run ruff check src/bsa_web/views/workbench.py src/bsa_web/views/detail.py`
- 完成标准：重跑后拓扑对照显示两个源（含零检出 `br_v4.34_MSG_develop_20260805` 标注「本期无新 commit」）；旧数据（sources 缺失）显示降级标注、不误报漏扫。
- 风险/回滚：纯呈现层；回滚 = revert 该 slice。

### Slice 4: override 按 cause 给控件 + 清除 + 重跑引导 + confirm 透明化

- 来源：`web-platform`（按成因给对 override 控件、清除 override、改判定立即重跑、确认继续语义透明化）+ tasks T4
- 目标：按 ManualReview 的 `cause` 精确渲染 override 控件；新增清除 override；override/清除后提供「立即重跑该分支」；confirm 前提示「发起独立手动同步」。
- 依赖：change1 Slice 4（`--clear`）、change1 Slice 3（`cause` 字段）；字段缺失时保守降级（不显示 override）
- 改动文件：
  - Modify: `src/bsa_web/templates/task_detail.html`、`src/bsa_web/api/manual_review.py`
  - Test: `tests/web/test_manual_review.py`
- TDD 计划：
  1. 先写失败测试：pending→「标记 bug fix」、severity_gate/fix_missing→「风险」下拉、其余 cause→无 override 仅确认/放弃；`is_bug_fix=false` 入口移除；清除 override 调 `--clear`；override 成功回调含重跑引导；confirm 前含透明化文案。
  2. 最小实现：review_items 渲染读 `cause`；`api_override` 增 clear 支持（body `clear:true` → CLI `--clear`）或新增 `/api/override/clear`；前端回调渲染重跑引导链接；confirm 触发前弹文案。
  3. 边界：存量无 `cause` → 不显示 override（保守默认），仅确认继续/放弃，闭环仍完整。
- 验证命令：
  - `uv run pytest tests/web/test_manual_review.py -x`
  - `uv run ruff check src/bsa_web/api/manual_review.py`
- 完成标准：增量周期 4 个 ManualReview（`cause=pending`）只显示「标记 bug fix」；override/清除后出现「立即重跑该分支」；confirm 前有「发起独立手动同步」提示；存量无 cause 时仅确认/放弃。
- 风险/回滚：纯平台呈现 + 复用 V1 CLI；回滚 = revert 该 slice。

## Verification Plan

- 单元/集成：`uv run pytest tests/web/test_detail_pages.py tests/web/test_workbench.py tests/web/test_task_center.py tests/web/test_manual_review.py`，再全量 `uv run pytest`。
- 静态：`uv run ruff check src`。
- 结果导向验收（web 操作合理性及格线）：
  - 存量（不重跑，直接打开 `cycle-2026-09-01`）：工作台「待确认 4」可点 → 周期概览 4 个 ManualReview 项可点进详情页 → 完成「确认继续/放弃」产生对应任务/审计记录；状态徽章「完成 · 4 待人工」；旧数据无 `sources`/`cause` 时显示降级标注、不显示 override、不 404、不误报漏扫。
  - 增量（重跑后，依赖 change1）：拓扑对照两源（零检出标注）；4 个 ManualReview `cause=pending` 只显示「标记 bug fix」；override/清除后「立即重跑该分支」；confirm 前「发起独立手动同步」提示。
- 依赖顺序：本 change 依赖 change1 的 `cause`/`sources`/`targets`/`--clear`；落地顺序 change1 → change2。

## Blockers / Clarifications

- 待实现阶段敲定（不阻塞 spec）：降级视图复用 `task_detail.html` + 缺省 branch dict（推荐）vs 新增独立模板；清除 override 端点复用 `/api/override` body `clear:true` vs 新增 `/api/override/clear`。

## Superpowers Handoff

- `writing-plans` 必须基于本文件生成 `docs/superpowers/plans/YYYY-MM-DD-close-web-manual-review-loop.md`
- 详细实现计划必须把本文件的 `## Project Context` 与 `## Applicable OpenSpec Rules` 复制/压缩到 plan header 或专门的「项目规则」章节，使后续 `executing-plans` 不需要重新读取 OpenSpec 也能遵守项目规范。
- 详细实现计划必须遵循 `openspec/config.yaml` 的 `language.artifacts: zh-CN`：自然语言标题、段落、任务名和步骤说明使用中文（模板骨架中文化：`目标`/`架构`/`技术栈`/`文件结构`/`项目规则`/`任务`/`步骤`/`自检`），代码标识符、路径、命令、事件名、OpenSpec schema 标题和协议关键字保持原文。
- 详细实现计划必须包含 Superpowers plan header 对应信息（目标/架构/技术栈）+ 文件结构 + 2-5 分钟 checkbox 步骤 + RED-GREEN-REFACTOR 测试节奏 + 精确验证命令 + 自检。
- 详细实现计划不得出现 TBD/TODO/「适当处理」/「类似上一步」等占位话术；每个 slice 展开为 checkbox 步骤。
- 详细实现计划不得省略 Source Coverage 中的任何验收点。
- **结果导向约束**：每个 slice 的「完成标准」必须落到 web 可观察结果（数据完整、操作合理），单测绿只是必要非充分条件；验收以「打开真实周期 + 完成一次人工项操作产生任务/审计记录」为准。
