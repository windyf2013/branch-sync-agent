# 实现计划：fix-cycle-enumeration-and-archive-boundary

## 来源

- 项目配置：`openspec/config.yaml`
- 当前规格：`openspec/specs/`（reporting / web-platform）
- 提案：`openspec/changes/fix-cycle-enumeration-and-archive-boundary/proposal.md`
- 设计：`openspec/changes/fix-cycle-enumeration-and-archive-boundary/design.md`
- 规格：`openspec/changes/fix-cycle-enumeration-and-archive-boundary/specs/`
- 任务：`openspec/changes/fix-cycle-enumeration-and-archive-boundary/tasks.md`

## Project Context

- 目的：Branch Sync Agent 定期检测 RCIOS 各分支合入 commit，判断是否需同步；在独立 worktree 完成 cherry-pick、冲突处理、编译验证、patch 生成，输出报告 + 邮件。
- 技术栈：Python 3.13 + uv；LangGraph；SQLite checkpoint；pydantic；PyYAML。
- 架构：V1 引擎（`src/bsa/`）与 V2 平台（`src/bsa_web/`）解耦，靠共享 SQLite + 文件系统通信；web 读经投影（`bsa report --json`）、写经 CLI。
- 周期概念：`cycle-YYYY-MM-DD`（每日 cron）/ `scan-*`（manual-scan 独立重扫）/ `manual-*`（手动同步）/ `rerun-*`（保留现场续跑）。只有 `cycle-*` 与 `scan-*` 由 `run_cycle` 落盘 `cycle.json`；`manual-*`/`rerun-*` 走单目标子图，不写 cycle.json。
- 测试：pytest + ruff；git 操作可 mock。

## Applicable OpenSpec Rules

- 引用现有 spec 再描述新行为；每条需求必须有具体场景和验收条件。
- 保持既有架构模式；每个实现任务以测试通过为完成条件。
- 不把真实路径/密钥硬编码。

## Goal

修复周期枚举 + 归档边界两个缺陷，让「重跑的 cron 任务」（`scan-*` 周期）在 web 上如实可见且终态正确，且最近完成周期不再误归档进历史页。**及格线：`bsa status`/工作台能看到 `scan-*` 周期且记 `succeeded`；历史页不再出现最近完成周期。**

## Non-Goals

- 不改周期 id 规则、不改 cron 调度写行时刻与引擎写 cycle.json 时刻的对齐。
- 不改 `manual-*`/`rerun-*` 的 cycle record 落盘行为（设计约束）。
- 不动 `latest_cycle_start` 语义（它同时供工作台手动任务边界使用）。

## Source Coverage

| OpenSpec 来源 | 验收点 | 对应 slice |
|---------------|--------|-----------|
| `reporting` / 周期记录枚举覆盖 scan 周期 | `list_cycle_records` 枚举 cycle-* + scan-* | Slice 1 |
| `reporting` / scan 周期终态折叠正确 | `_cycle_terminal_status` 读到 scan 终态，记 succeeded | Slice 1 |
| `web-platform` / 历史报告查看不包含最近完成周期 | `_archived_tasks` 排除 latest_completed_cycle | Slice 2 |
| `tasks.md` T1 | 引擎 glob 修复 | Slice 1 |
| `tasks.md` T2 | 平台归档排除 | Slice 2 |

## File Responsibility Map

| 文件 | 操作 | 责任 | 相关 slice |
|------|------|------|------------|
| `src/bsa/scheduler/cycle.py` | modify | `list_cycle_records` 统一 glob cycle-* + scan-* | Slice 1 |
| `src/bsa_web/views/history.py` | modify | `_archived_tasks` 排除最近完成周期本身 | Slice 2 |
| `tests/test_scheduler_cycle.py` | modify | scan-* 枚举单测 | Slice 1 |
| `tests/web/test_detail_pages.py` | modify | 当前周期不归档单测 | Slice 2 |

## Implementation Slices

### Slice 1: list_cycle_records 统一 glob cycle-* + scan-*

- 来源：`reporting`（周期记录枚举覆盖 scan 周期、scan 周期终态折叠正确）+ tasks T1
- 目标：`scan-*` 周期（manual-scan 落盘）不再隐身/误标 failed，所有调用点自然覆盖。
- 依赖：无
- 改动文件：
  - Modify: `src/bsa/scheduler/cycle.py`
  - Test: `tests/test_scheduler_cycle.py`
- TDD 计划：
  1. 先写失败测试：同时造 `cycle-*` 与 `scan-*` 目录（含 cycle.json），断言 `list_cycle_records` 返回两类、按 `started_at` 升序；无 cycle.json 的 `manual-*` 目录不被枚举。
  2. 最小实现：`list_cycle_records` 改为遍历 `cycle-*` + `scan-*` 两个 glob。
  3. 边界：`scan-*` 目录名含 `:`/`+`，glob 正常匹配；排序逻辑不变。
- 验证命令：
  - `uv run pytest tests/test_scheduler_cycle.py tests/test_projection.py -x`
  - `uv run ruff check src/bsa/scheduler/cycle.py`
- 完成标准：`latest_completed_cycle` 能取到 `scan-*`；`bsa report <scan-id> --json` 返回完整终态；`register_finish` 记 succeeded。
- 风险/回滚：纯枚举改动，回滚 = revert 该 slice。

### Slice 2: 归档排除最近完成周期本身

- 来源：`web-platform`（历史报告查看不包含最近完成周期）+ tasks T2
- 目标：最近完成周期自己的 tasks 行不被归档进历史页，同一周期不再同时出现在工作台和历史页。
- 依赖：Slice 1（`latest_completed_cycle` 能正确返回，含 scan-*）
- 改动文件：
  - Modify: `src/bsa_web/views/history.py`
  - Test: `tests/web/test_detail_pages.py`
- TDD 计划：
  1. 先写失败测试：`latest_completed_cycle` 的 cycle 任务（tasks 行 created_at 早于 latest_cycle_start）不进归档列表，其余历史周期照常归档。
  2. 最小实现：`_archived_tasks` 内，对 `kind=cycle` 且 `cycle_id == latest_completed_cycle` 跳过归档。
  3. 边界：只排除最近完成周期这一条；手动 sync/rerun 归档边界不变。
- 验证命令：
  - `uv run pytest tests/web/test_detail_pages.py -x`
  - `uv run ruff check src/bsa_web/views/history.py`
- 完成标准：历史页不再出现 `latest_completed_cycle`，它只留在工作台。
- 风险/回滚：只改 `_archived_tasks` 一处判断，回滚 = revert 该 slice。

## Verification Plan

- 单元/集成：`uv run pytest tests/test_scheduler_cycle.py tests/test_projection.py tests/web/test_detail_pages.py`，再全量 `uv run pytest`。
- 静态：`uv run ruff check src`。
- 结果导向验收：重扫 `cycle-2026-09-01` 窗口后 `bsa status` 显示 `scan-*` 周期、tasks 行记 `succeeded`；历史页不再出现 `cycle-2026-09-01`。

## Blockers / Clarifications

- 无。

## Superpowers Handoff

- `writing-plans` 必须基于本文件生成 `docs/superpowers/plans/YYYY-MM-DD-fix-cycle-enumeration-and-archive-boundary.md`
- 详细实现计划必须把本文件的 `## Project Context` 与 `## Applicable OpenSpec Rules` 复制/压缩到 plan header 或专门的「项目规则」章节。
- 详细实现计划必须遵循 `openspec/config.yaml` 的 `language.artifacts: zh-CN`（模板骨架中文化），代码标识符/路径/命令保持原文。
- 详细实现计划不得出现 TBD/TODO/占位话术；每个 slice 展开为 checkbox 步骤。
- **结果导向约束**：完成标准落到 web 可观察结果（周期可见、终态正确、不误归档），单测绿是必要非充分条件。
