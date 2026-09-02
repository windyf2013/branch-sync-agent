# decision Specification (delta)

## MODIFIED Requirements

### Requirement: 规则层先行 + LLM 兜底

classify 机器规则无法判定的 commit，由 Sync Decision Agent 兜底判定。

#### Scenario 规则无法判定 is_bug_fix 的 commit。

- **当** classify 机器规则无法判定某 commit 是否为 bug-fix
- **则** 交 Sync Decision Agent（LLM）只判定 is_bug_fix
- **且** 判定按 SHA 缓存（agent_judgments），后续周期命中缓存不再调 LLM
- **且** 支持人工在 judgments 文件中覆盖判定，人工覆盖优先级最高
- **且** LLM 不可用时降级为人工审核（degrade_to_manual，默认 true）

#### Scenario: 人工覆盖贯通全量 commit（G9）

- **当** 检测阶段对每个 commit 判定 is_bug_fix
- **则** 加载 `judgments.json` 人工覆盖并传入 classify，使人工覆盖对**全量** commit 生效
- **且** 人工覆盖优先级最高，可覆盖机器已判定的 bug-fix / 非 bug-fix 标记
- **且** 覆盖生效于下一次决策执行，不追溯改写已冻结的 decisions.json

## ADDED Requirements

### Requirement: 目标类型由决策规则单一驱动

conclude 判定用的目标分支类型（need_sync / ineligible）由 `decision_rules.yaml` 单一驱动。

#### Scenario: 目标类型数据驱动

- **当** conclude 判定某 (commit, 目标分支) 四态结论
- **则** `need_sync_target_types` / `ineligible_target_types` 从 `decision_rules.yaml`
  经 `ConcludeThresholds` 注入，不在 `conclude.py` 硬编码
- **且** 新增/调整目标类型只改 yaml 不碰代码
