# decision Specification (delta)

## ADDED Requirements

### Requirement: 覆盖判定 CLI

提供人工覆盖判定的命令，经该命令写入 judgments.json。

#### Scenario: 人工覆盖判定

- **当** 调用 `bsa override <sha> [--is-bug-fix] [--risk]`
- **则** 将 sha 的判定写入 judgments.json，人工覆盖优先级最高（V1 既有语义）
- **且** 执行时持有全局 flock，避免与周期决策的 judgments 写入竞争

### Requirement: 覆盖生效时机

覆盖判定生效于下一次决策执行，不追溯改写已冻结结论。

#### Scenario: 下次决策生效

- **当** judgments.json 被人工覆盖后
- **则** 下一次决策执行（cron 周期或重跑重判）读到新判定并据此得出结论
- **且** 已冻结的 decisions.json（上一周期结论）不被追溯改写，保持审计一致性
