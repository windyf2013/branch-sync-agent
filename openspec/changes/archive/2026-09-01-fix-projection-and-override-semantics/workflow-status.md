# Workflow Status: fix-projection-and-override-semantics

## Summary

- Phase: close
- Capture Mode: none
- Status: completed
- Last Updated: 2026-09-01
- Next Command: 无（变更已完成）
- Next Action: 可以开始新的 change

## Gates

| Gate | Status | Evidence |
|------|--------|----------|
| Requirements captured | passed | proposal.md |
| Grill decision | passed | brainstorming 反向追问 3 决策点已敲定（patch_id 统一第一父聚合 / fp 派生键直接删除 / sync_decision 键缺失不短路） |
| Specs validated | passed | specs/*、tasks.md；`openspec validate --strict` 无错误 |
| Plan ready | passed | plan-ready.md |
| Implementation complete | passed | 5 个 slice 全部提交 |
| Verification complete | passed | `uv run pytest` 1079 passed / 3 skipped；`ruff check src` 零告警；真实 scan 重跑验证 sources/merge patch_id/diff_stat/cause |
| Archived | passed | 已归档 |

## Tasks

| ID | Task | Status | Verification | Blocked By | Notes |
|----|------|--------|--------------|------------|-------|
| T1 | 引擎：TaskState 补齐 sources/targets | verified | `tests/test_projection.py` | - | 完整拓扑（含零检出源）落盘 |
| T2 | 引擎：commit_patch/patch_id 统一第一父聚合 + diff_stat | verified | `tests/test_git_service.py` | - | merge patch_id 不再恒空、非 merge 不变 |
| T3 | 引擎：Conclusion4 增加 cause 成因字段 | verified | `tests/test_rules.py` | - | 7 值枚举，仅 ManualReview 填 |
| T4 | 引擎：override 语义收紧（classify + sync_decision + clear） | verified | `tests/test_override.py` | - | 键缺失不覆盖 + `--clear` |
| T5 | 不变量 #5 表述澄清 | verified | `ruff check src` + spec delta | - | CLAUDE.md + decision spec |

## Amendments

| Date | Reason | Affected Specs | Affected Tasks | Status |
|------|--------|----------------|----------------|--------|
| 2026-09-01 | patch_id 不再保持 git show（恒空缺陷），统一第一父聚合；override 键缺失不覆盖扩到 sync_decision 层 | detection, decision | T2, T4 | applied |
