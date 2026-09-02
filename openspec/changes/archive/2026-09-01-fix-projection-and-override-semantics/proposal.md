# Proposal: fix-projection-and-override-semantics

## Why

对 `cycle-2026-09-01` 的真实数据走查，暴露出三类引擎侧问题，共同导致 web 无法「正确呈现 + 正确给操作」：

1. **投影拓扑字段丢失**：`detect_commits`（`nodes.py:347-348`）计算了完整拓扑全集（含零检出源），但 `TaskState`（`state.py:15-31`）未声明 `sources`/`targets` 字段，LangGraph 按 TypedDict schema 静默丢弃，`state.json` 顶层恒为空。零检出的源分支（本周期 `br_v4.34_MSG_develop_20260805`）在 web 上彻底隐形，「零检出」与「漏扫」无法区分。

2. **merge commit 无 diff / 无统计**：`commit_patch` 用 `git show`，对 merge 不产出 diff（6 个 merge 全 0B）；增删行统计也未落盘。待人工的 merge commit 用户看不到任何改动内容，无法判断是否值得同步。

3. **override 语义缺陷**：`Conclusion4` 只有 `{kind, evidence, confidence}`，web 无法区分 ManualReview 的成因，只能靠中文文案猜，导致「改判定」对所有 ManualReview 一视同仁（含 override 无效的相似度灰区/函数重命名）；且 `classify_commit`（`classify.py:246`）把「无 is_bug_fix 键」当 `False`、`apply_override` 无清除能力，override 既会静默误判、又不可回退。

## What Changes

- **`TaskState` 补齐 `sources`/`targets`**：完整拓扑（含零检出源）落盘投影。
- **merge 第一父聚合 diff + `diff_stat` + `patch_id` 统一第一父聚合**：merge 有可展示内容、有可判 identity（patch_id 不再恒空）；`patch_id`/`patch_text` 切到 `diff-tree -p -r <first_parent> <sha>`，非 merge 语义不变，merge 从「所有 merge 同一恒值」修成互异。
- **`Conclusion4` 增加 `cause` 结构化成因字段**：ManualReview 标注成因枚举，web 据此精确渲染 override 控件。
- **override 语义收紧**：`is_bug_fix` 键缺失不覆盖（不再误判 false）；新增清除 override（`--clear`）；不变量 #5 表述澄清「人工覆盖作用于 classify 层，四态客观结论不接收 override，人工拍板四态走确认继续/放弃」。

明确**不做**：不改四态判定结果、不改 `conclude_pair` 判定逻辑、不改 ledger/fingerprint 的**语义**（非 merge 的 patch_id 不变；merge 的 patch_id 从恒空修成第一父聚合，属修 bug）、不新增强制同步/强制排除的四态 override、**不写离线回填脚本补历史周期缺失字段（存量降级、增量完整）**。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `reporting`: 投影 payload 补充 `sources`/`targets`（含零检出源）与 `cause`（ManualReview 成因）。
- `detection`: merge commit 的 `patch_text`/`patch_id` 统一第一父聚合 diff + `diff_stat` 统计字段（非 merge 语义不变）。
- `decision`: override 语义精确化——`is_bug_fix` 键缺失不覆盖（classify 与 sync_decision 两层）、新增清除、不变量 #5 边界澄清。

## Impact

- 引擎 `src/bsa/graph/state.py`：`TaskState` 加 `sources`/`targets`。
- 引擎 `src/bsa/domain/models.py`：`Conclusion4` 加 `cause`；`CommitInfo` 加 `diff_stat`。
- 引擎 `src/bsa/git/service.py`：`commit_patch` 与 `patch_id` 统一走第一父聚合 `diff-tree -p -r <parent> <sha>`；新增 `diff_stat`。
- 引擎 `src/bsa/rules/classify.py` + `src/bsa/agents/sync_decision.py`：`classify_commit` 与 `SyncDecisionAgent.run`/`_from_entry` 键缺失不覆盖 `is_bug_fix`（区分显式 false 与键缺失）。
- 引擎 `src/bsa/commands/override.py` + `src/bsa/cli.py`：`apply_override` 加 clear 分支；CLI 加 `--clear`。
- 测试：graph 投影单测、git service patch_id/diff_stat 单测、classify 键缺失单测、sync_decision 键缺失单测、override clear 单测。

## 设计阶段已敲定

1. `cause` 枚举 7 值：`pending / severity_gate / fix_missing / function_renamed / similarity_gray / symbols_missing / unknown_branch_type`，与 `conclude_pair` 7 个 ManualReview 返回点一一对应（见 design）。
2. `diff_stat` 结构：单对象 `{files, insertions, deletions}`。
3. `patch_id` 不再保持 `git show`（恒空缺陷），统一切到第一父聚合 `diff-tree -p -r <first_parent> <sha>`（非 merge 语义不变，merge 从恒空修成互异）。
