# Tasks: close-web-manual-review-loop

## 1. decision 层 ManualReview 操作可达

- 文件：`src/bsa_web/views/task_detail.py`
- 改动：`branch_results` 无该 target 时，若 `action_required` 存在该 target 的 ManualReview 项，则注入缺省 branch dict（status=MANUAL、worktree_path=""、commits=[]）继续渲染，不 404。
- 验证：`tests/web/test_detail_pages.py` 新增——branch_results 空 + 有 ManualReview 时 `/task/{cycle}/{target}` 返回 200 且渲染人工项操作区。

## 2. 待确认可点 + 达成度标记

- 文件：`src/bsa_web/views/workbench.py`、`src/bsa_web/templates/workbench.html`
- 改动：待确认 KPI 与 cycle_summary_row「待确认 N」可点；period-bar 与状态徽章在 SUCCESS+待人工时显示「完成 · N 待人工」。
- 验证：`tests/web/test_workbench.py` 新增——SUCCESS+待人工时达成度标记出现；待确认 KPI 为链接。

## 3. 拓扑对照呈现

- 文件：`src/bsa_web/views/workbench.py`、`src/bsa_web/views/detail.py` + 模板
- 改动：展示完整源/目标清单（优先 `payload.sources`/`targets`），零检出源标注「本期无新 commit（正常）」；字段缺失时降级推导并标注。
- 验证：`tests/web/test_task_center.py` 新增——含零检出源的 payload 显示零检出标注；字段缺失时显示降级标注。

## 4. override 按 cause 给控件 + 清除 + 重跑引导 + confirm 透明化

- 文件：`src/bsa_web/templates/task_detail.html`（前端回调）、`src/bsa_web/api/manual_review.py`（清除端点）
- 改动：按 `cause` 渲染 override 控件（pending→is_bug_fix、severity_gate/fix_missing→risk、其余→无 override）；移除 `is_bug_fix=false` 入口；新增清除 override（复用 `bsa override --clear`）；override/清除成功后显示「立即重跑该分支」；`/api/confirm` 触发前展示「将发起一条独立手动同步任务（单个 commit）」文案。
- 验证：`tests/web/test_manual_review.py` 新增——按 cause 给对控件、清除 override 调 `--clear`、override 成功回调含重跑引导、confirm 触发前含透明化文案。

## 5. 回滚说明

- 改动纯 web 呈现与操作闭环，不改引擎（依赖 change1 的 `cause` 与 `--clear`）、不改任务状态机、不改推送四道闸。
- 回滚 = revert 平台提交；无数据迁移。

## 验证命令

```bash
uv run pytest tests/web/test_detail_pages.py tests/web/test_workbench.py tests/web/test_task_center.py tests/web/test_manual_review.py
uv run ruff check src
uv run pytest
```

## 验收标准（结果导向）

### 存量验收（不重跑，直接打开现有 `cycle-2026-09-01`）

1. 工作台「待确认 4」可点；周期概览 4 个 ManualReview 项可点进详情页；详情页完成「确认继续 / 放弃」任意一项并产生对应任务/审计记录。
2. 状态徽章显示「完成 · 4 待人工」（达成度，不依赖新字段）。
3. 源 2 `br_v4.34_MSG_develop_20260805` 因旧数据 `sources` 缺失而隐形，web 显示「拓扑字段缺失，仅展示检出过 commit 的分支」降级标注（不误报漏扫）。
4. 存量 ManualReview 因旧数据无 `cause`，详情页不显示 override 控件（保守降级），仅「确认继续 / 放弃」——闭环仍完整。
5. 存量周期不崩、不 404，历史归档页正常。

### 增量验收（重跑 `cycle-2026-09-01` 后，依赖 change1）

6. 拓扑对照显示两个源（含零检出 `MSG` 标注「本期无新 commit」）与目标。
7. 4 个 ManualReview 均 `cause=pending`，详情页只显示「标记 bug fix」override 控件；override/清除成功后出现「立即重跑该分支」入口；confirm 前有「将发起独立手动同步」提示。

### 回归

8. `uv run pytest` 全绿、`uv run ruff check src` 零告警。

## 待设计确认

1. 降级视图：复用 `task_detail.html` + 缺省 branch dict（推荐）vs 新增独立「仅人工项」模板。
2. 清除 override 的端点形态：复用 `/api/override` body 加 `clear: true`，还是新增 `/api/override/clear` 独立端点（实现阶段定）。
