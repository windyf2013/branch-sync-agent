# decision Specification

## Purpose
TBD - created by archiving change add-branch-sync-agent. Update Purpose after archive.

## Requirements

### Requirement: 四态结论由规则输出

对每个 (commit, 目标分支) 对，由 conclude 确定性规则输出同步状态结论。
#### Scenario 对每个 (commit, 目标分支) 对判定同步状态。
- **当** commit 通过 classify 判定为 bug-fix
- **则** 用 conclude 确定性规则输出四态：`NeedSync / AlreadyIncluded / ManualReview / OutOfScope`
- **且** 四态结论全部由规则输出，LLM 不直接输出四态、不覆盖规则结论
- **且** 同一 commit 对不同目标分支可有不同结论

### Requirement: 规则层先行 + LLM 兜底

classify 机器规则无法判定的 commit，由 Sync Decision Agent 兜底判定。
#### Scenario 规则无法判定 is_bug_fix 的 commit。
- **当** classify 机器规则无法判定某 commit 是否为 bug-fix
- **则** 交 Sync Decision Agent（LLM）只判定 is_bug_fix
- **且** 判定按 SHA 缓存（agent_judgments），后续周期命中缓存不再调 LLM
- **且** 支持人工在 judgments 文件中覆盖判定，人工覆盖优先级最高
- **且** LLM 不可用时降级为人工审核（degrade_to_manual，默认 true）

### Requirement: classify 规则完整移植

classify 机器规则覆盖 RCIOS commit 习惯，数据驱动并带 golden-set 测试。
#### Scenario classify 机器规则覆盖 RCIOS commit 习惯。
- **当** classify 运行时
- **则** 支持 `[BUG]` / `:bug:` / `fix:` / 中文修复词 / issue-id(CQ/BUG/JIRA) / cherry-pick 标记判定为 bug-fix
- **且** 支持版本号 / chore/docs / AI IGNOR / openspec-only 判定为非 bug-fix
- **且** 规则以数据驱动（decision_rules.yaml），带 golden-set 测试
- **且** 判定来源（recognition_source）枚举统一：machine:[BUG] / machine:fix: / machine:issue / machine:cherry-pick / machine:version-bump / machine:chore-docs / machine:docs-only / agent:bug-fix / agent:not-bug-fix / pending:claude-agent / not-included
- **且** 规则层输出 Classification，Agent 判定输出 SyncDecision，SyncDecision 是 State classifications[sha] 的唯一类型（映射见 design.md agents 模型关系）

### Requirement: 决策先行固化批次

决策完成后固化所有分支批次供执行阶段使用。
#### Scenario 决策完成后固化分支批次。
- **当** 决策阶段完成
- **则** 固化所有分支批次（target → commits 按合入顺序）
- **且** 执行阶段只按固定批次顺序运行，checkpoint 恢复清晰
