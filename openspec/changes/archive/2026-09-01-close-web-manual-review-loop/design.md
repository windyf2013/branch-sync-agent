# Design: close-web-manual-review-loop

## 架构总览

纯 web 侧呈现与操作闭环修复，不碰引擎、不改任务状态机、不改推送四道闸。核心是把「decision 层 ManualReview」从「不可达」变「可达」，并把状态/拓扑语义从「误导」变「如实」。

```
周期 → bsa report --json（投影）
  ├ branch_results（执行层）—— 非空时走现有 task_detail 全量视图
  └ 空但有 ManualReview（决策层）—— 降级渲染「仅人工项」视图（新）
         └ 人工项操作：确认继续 / 改判定 / 放弃（复用既有 API）

工作台/周期概览
  ├ 待确认 KPI 可点 → 落到可操作页（新）
  ├ period-bar 达成度「完成 · N 待人工」（新）
  └ 拓扑对照：完整源/目标清单 + 零检出标注（新，依赖 sources/targets 字段）
```

## 改动 1：decision 层 ManualReview 操作可达

- **文件**：`src/bsa_web/views/task_detail.py`。
- **现状**：`branch = branch_results.get(target)` 为 None 且无活动任务 → 404（`task_detail.py:76-81`）。
- **修复**：branch 为 None 时，先检查 `payload.action_required` 是否存在 `kind==ManualReview and branch==target` 的项；存在则构造缺省 branch dict 继续渲染，不 404：
  ```python
  branch = {"target_branch": target, "status": "MANUAL", "worktree_path": "",
            "commits": [], "patch_path": None, "baseline": None, "stop_reason": None}
  ```
- **安全性**：缺省 branch 使 `_target_detail.html` 的 `{% if branch.baseline %}`/`{% if branch.commits %}` 条件渲染空安全；`show_webssh`（依赖 worktree_path）自动为 False，`show_push`（依赖 SUCCESS）自动为 False——不暴露任何本不存在的操作。review_items 提取（`task_detail.py:122-126`）本就独立于 branch_results，可直接渲染操作区。
- **不引入新问题**：不改 branch_results 结构、不改 `_target_detail.html` 逻辑，仅放宽 404 条件 + 注入缺省值。

## 改动 2：待确认可点 + 达成度

- **文件**：`src/bsa_web/views/workbench.py` + `templates/workbench.html`。
- **待确认可点**：KPI「待确认」由 `<div>` 改为链接，指向周期概览（`/cycle/{current_cycle_id}`）或第一个待处理 target 的任务详情页；`cycle_summary_row` 的「待确认 N」同样可点。
- **达成度**：`workbench.py` 渲染时计算 `review_pending` 计数（已有 `_build_todo`），当 `payload.status == "SUCCESS"` 且 `review_pending` 非空时，模板 period-bar 与状态徽章显示「完成 · N 待人工」；否则维持纯「完成」。

## 改动 3：拓扑对照

- **文件**：`src/bsa_web/views/workbench.py`、`src/bsa_web/views/detail.py` + 模板。
- **数据源**：优先 `payload.sources` / `payload.targets`（依赖 `fix-projection-topology-fields`）。
- **零检出判定**：`sources 全集 - {c.source_branch for c in detected_commits}` = 零检出源，标注「本期无新 commit（正常）」。
- **降级**：`sources`/`targets` 缺失时，从 `detected_commits[].source_branch` 与 `decisions` 键推导，并标注「拓扑字段缺失，仅展示检出过 commit 的分支」。

## 改动 4：override 按 cause 给控件 + 清除 + 重跑引导 + confirm 透明化

- **文件**：`src/bsa_web/templates/task_detail.html`（前端回调）、`src/bsa_web/api/manual_review.py`（清除端点）。
- **按 cause 给控件**：review_items 渲染时读 `cause`（`payload.decisions[sha][target].cause`，依赖 change1），按成因渲染：
  - `pending` → 「标记 bug fix」（`is_bug_fix=true`）
  - `severity_gate`/`fix_missing` → 「风险」下拉
  - 其余 → 不渲染 override，仅「确认继续 / 放弃」
  - 移除 `is_bug_fix=false` 入口
- **清除 override**：`api_override` 增加 clear 支持（body 传 `clear: true` → CLI `--clear`），或在 `api/manual_review.py` 新增 `/api/override/clear`。前端对已有 override 的项显示「清除 override」按钮。
- **重跑引导**：override 或清除成功后，前端显示「立即重跑该分支」链接（复用 rerun，target 预填）。
- **confirm 透明化**：`/api/confirm` 触发前，前端展示「将发起一条独立手动同步任务（单个 commit 直同步），不在原周期内收敛」。后端语义不变（`enqueue_task(kind="sync", shas=[sha])`）。

## 数据流与边界

- 只读仍经投影，写操作仍经既有 API（override/confirm/abandon/rerun），清除 override 复用 override CLI 的 `--clear`（change1）。
- 按 cause 给控件依赖引擎 `cause` 字段；字段缺失时 web 降级——不显示 override（保守默认，避免误引导），仅提供确认继续/放弃。
- 拓扑对照依赖引擎 `sources`/`targets` 字段；字段缺失时 web 降级并明确标注，不误报「漏扫」。

## 测试策略

- `tests/web/` 新增：decision 层 ManualReview（branch_results 空）访问 `/task/{cycle}/{target}` 返回 200 且渲染人工项操作区；工作台待确认 KPI 可点；达成度标记在 SUCCESS+待人工时出现；拓扑对照零检出标注；按 cause 给对控件（pending→is_bug_fix、severity_gate→risk、相似度→无 override）；清除 override 调用 `--clear`；override 成功回调含重跑引导；confirm 触发前含透明化文案。
- 复用现有 TestClient + fake projection + FakeExecutor 基建。
- 全量 `uv run pytest` + `uv run ruff check src` 通过。
