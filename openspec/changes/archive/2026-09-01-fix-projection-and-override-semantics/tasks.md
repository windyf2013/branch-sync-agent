# Tasks: fix-projection-and-override-semantics

## 1. 引擎：TaskState 补齐 sources/targets

- 文件：`src/bsa/graph/state.py`（`TaskState`）
- 改动：新增 `sources: list[str]`、`targets: list[str]` 字段。
- 验证：`tests/test_projection.py` 新增单测——含零检出源的 matrix 跑 detect_commits 后 `state.json` 的 `sources`/`targets` 含全集。

## 2. 引擎：commit_patch / patch_id 统一第一父聚合 + diff_stat

- 文件：`src/bsa/git/service.py`（`commit_patch` + `patch_id` + 新增 `diff_stat`）、`src/bsa/domain/models.py`（`CommitInfo.diff_stat`）
- 改动：`commit_patch` 与 `patch_id` 统一走第一父聚合 `git diff-tree --no-commit-id -p -r <first_parent> <sha>`（`changed_files` 同源）；新增 `diff_stat = {files, insertions, deletions}`（`diff-tree --numstat -r <first_parent> <sha>`，不因超限丢弃）。
- 验证：`tests/test_git_service.py` 新增单测——merge `commit_patch` 非空、`patch_id` 互异（不再恒空）、非 merge `patch_id` 不变、根 commit 返回 None/空、`diff_stat` 产出统计（超限不丢）。

## 3. 引擎：Conclusion4 增加 cause 成因字段

- 文件：`src/bsa/domain/models.py`（`Conclusion4.cause`）、`src/bsa/rules/conclude.py`（各 ManualReview 返回点补 `cause`）
- 改动：`cause` 枚举 `pending / severity_gate / fix_missing / function_renamed / similarity_gray / symbols_missing / unknown_branch_type`；仅 ManualReview 填值，其余为 None。
- 验证：`tests/test_rules.py`（或 `tests/test_domain_models.py`）新增单测——各 ManualReview 成因 cause 正确、非 ManualReview 为 None。

## 4. 引擎：override 语义收紧

- 文件：`src/bsa/rules/classify.py`（`classify_commit` 键缺失不覆盖 is_bug_fix）、`src/bsa/agents/sync_decision.py`（`run`/`_from_entry` 键缺失不短路）、`src/bsa/commands/override.py`（`apply_override` 加 clear）、`src/bsa/cli.py`（`--clear`）
- 改动：
  - `classify_commit`：`is_bug_fix` 键缺失时不覆盖（区分显式 false 与键缺失）。
  - `sync_decision.run`/`_from_entry`：`is_bug_fix` 键显式存在才短路 LLM；键缺失（只写 risk）时不短路、不把 `needs_agent` 压成 False，仅应用 risk。
  - `apply_override`：`clear=True` 删除 sha 键 + 现算 `fp:` 键；CLI `--clear` 与写字段互斥。
- 验证：`tests/test_override.py` 新增——只写 risk 不再误判 false（classify 与 sync_decision 两层）、显式 false 仍生效、`--clear` 删除 sha/fp 键、互斥报错。

## 5. 不变量 #5 表述澄清

- 文件：`CLAUDE.md` 不变量 #5；`openspec/specs/decision/spec.md`（delta）
- 改动：澄清「人工覆盖作用于 classify 层（is_bug_fix/risk），四态客观结论不接收 override，人工拍板四态走确认继续/放弃」。

## 6. 回滚说明

- 改动不改变 Workflow 流程 / Agent 判断 / Tool 白名单 / SafetyEnforcer / `conclude_pair` 判定结果；非 merge 的 patch_id 语义不变（merge 从恒空修成互异属修 bug）。
- 回滚 = revert 引擎提交；无数据迁移（旧周期投影靠 `or []` 兜底、旧 judgments 无 clear 键不报错）。

## 验证命令

```bash
uv run pytest tests/test_projection.py tests/test_git_service.py tests/test_rules.py tests/test_override.py tests/test_domain_models.py
uv run ruff check src
uv run pytest
```

## 验收标准（结果导向）

1. 真实 `cycle-2026-09-01` 重跑：`sources` 含两个源（含零检出 `br_v4.34_MSG_develop_20260805`）、`targets` 含目标；merge `diff_stat`/`patch_text` 非空、`patch_id` 互异；4 个 ManualReview `cause == "pending"`。
2. override 实操：`--risk high` 不误判 not-bug-fix；`--clear` 回到未判定。
3. web 走查：拓扑与零检出标注、merge 统计、ManualReview 按 cause 显示对应控件。
4. `uv run pytest` 全绿、`uv run ruff check src` 零告警。
