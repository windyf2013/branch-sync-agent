# Proposal: close-web-manual-review-loop

## Why

`cycle-2026-09-01` 检测 8 个 commit，4 个落入 ManualReview（待人工），但这 4 个待人工项**全部发生在 decision 层**（`branch_results={}`，未进入任何分支执行），导致 web 上三个入口全部断裂：工作台「待确认 4」KPI 是纯 `<div>` 不可点；周期概览的 sha 链接到 commit 详情页，而该页只有展示无任何操作按钮；唯一有操作按钮的 task 详情页 `/task/{cycle}/{target}` 因 `branch_results` 无该 target 而 404。结果「人工处理」这一管理闭环的处置环节在本周期完全不可达。

叠加呈现层的两个误导：周期 `status=SUCCESS` 被渲染成纯绿「完成」，掩盖了「同步 0 + 待人工 4」的真相；「改判定（override）」对所有 ManualReview 一视同仁，而实际上 override 只对少数成因有效（见 `fix-projection-and-override-semantics`），且无「立即重跑」引导。根因是 web 的操作可达性过度信任投影 `branch_results` 的完整性，且人工项操作没有按成因区分。

## What Changes

- **decision 层 ManualReview 操作可达**：task 详情页在 `branch_results` 无该 target、但存在针对该 target 的 ManualReview 项时，降级渲染「仅人工项」视图（无 worktree/build/patch，但有人工项操作区），不再 404。
- **待确认可点**：工作台「待确认」KPI 与周期概览 ManualReview 项变为可点，落到可操作页。
- **状态达成度醒目标记**：周期 SUCCESS 但存在待人工项时，period-bar 与状态徽章显示「完成 · N 待人工」，不只用纯绿「完成」。
- **拓扑对照呈现**：工作台/周期概览显示完整源/目标清单（依赖 `sources`/`targets` 投影字段），零检出源标注「本期无新 commit」，让「零检出」与「漏扫」可区分。
- **override 按 cause 给控件 + 清除**：按 ManualReview 的 `cause` 精确渲染 override 控件（`pending`→标记 bug fix、`severity_gate`/`fix_missing`→风险、其余→不显示 override 只给确认/放弃）；移除 `is_bug_fix=false` 入口；新增「清除 override」。
- **改判定/清除后立即重跑引导**：override 或清除成功后提供「立即重跑该分支」入口（复用 rerun，整分支重判），让新判定尽快生效。
- **确认继续透明化**：「确认继续」在 UI 明确提示「将发起一条独立手动同步任务（单个 commit 直同步）」，避免误以为在原周期内收敛。

明确**不做**：不写离线回填脚本补历史周期缺失字段——**存量降级、增量完整**（历史周期信息缺口是引擎落盘时未写，重跑成本高且会动 ledger/worktree；历史待人工项用「确认继续/放弃」已能闭合）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `web-platform`: 人工项处理、实时工作台首页、历史报告与详情查看的呈现与操作闭环——decision 层 ManualReview 操作可达、拓扑对照、状态达成度、override 按 cause 给控件 + 清除、重跑引导、确认继续透明化。

## Impact

- 平台 `src/bsa_web/views/task_detail.py`：branch 缺失但有 ManualReview 时降级渲染（不再 404）。
- 平台 `src/bsa_web/views/workbench.py`：待确认 KPI 可点、拓扑对照、达成度标记。
- 平台 `src/bsa_web/views/detail.py`：周期概览 ManualReview 项可点、拓扑对照。
- 平台 `src/bsa_web/templates/`：`workbench.html`、`detail.html`、`task_detail.html` 相应区块。
- 平台 `src/bsa_web/api/manual_review.py`：清除 override 端点（复用 `bsa override --clear`）。
- 测试：`tests/web/` 新增 decision 层 ManualReview 可达、拓扑对照、达成度、按 cause 给控件、清除 override、重跑引导单测。
- 依赖：拓扑对照依赖 `fix-projection-and-override-semantics` 的 `sources`/`targets` 字段；按 cause 给控件依赖其 `cause` 字段；清除 override 依赖其 `--clear`。字段缺失时 web 侧均兜底降级。

## 待定项（实现阶段敲定）

1. 降级视图是否复用现有 `task_detail.html` + `_target_detail.html`（构造缺省 branch dict），还是新增独立「仅人工项」模板。
2. 清除 override 的端点形态：复用 `/api/override` body 加 `clear: true`，还是新增 `/api/override/clear` 独立端点。
