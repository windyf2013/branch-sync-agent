# reliability Specification

## Purpose
TBD - created by archiving change add-branch-sync-agent. Update Purpose after archive.

## Requirements

### Requirement: 节点级容错

node 异常由 wrapper 捕获写入 state，不崩溃。
#### Scenario 单个 node 出错不崩溃整个 workflow。
- **当** 任何 node 抛异常
- **则** node_wrapper 捕获，异常写入 state.errors，不冒泡出 graph
- **且** workflow 继续执行到报告节点
- **且** 失败节点状态记录可审计

### Requirement: LLM 容错与降级

LLM 失败重试后降级安全默认，确定性流程仍可运行。
#### Scenario LLM 调用失败或不可用。
- **当** LLM 调用超时/失败
- **则** 指数退避重试（max_retries 可配置）
- **且** 重试仍失败则降级安全默认：sync_decision → ManualReview、conflict → 转人工、build → 归因无法修复
- **且** LLM 完全不可用时确定性流程（检测/判定/报告）仍可运行，降级全人工审核

### Requirement: 外部命令容错

外部命令超时遵循 timeout≠未发生，先查真实状态。
#### Scenario git/docker 命令超时或失败。
- **当** 外部命令超时
- **则** 遵循"timeout ≠ 操作一定没有发生"，先查真实状态（git 状态/产物/容器状态）再判定，不盲目重试
- **且** fetch 失败指数退避重试（次数/间隔可配置）
- **且** 编译超时三查后判定（产物/日志标志/容器状态）

### Requirement: checkpoint 与恢复

周期中断通过 SQLite checkpoint 恢复。
#### Scenario 周期中断恢复。
- **当** 进程崩溃/退出后重启
- **则** 通过 SQLite checkpoint（thread_id = cycle_id）恢复执行
- **且** 决策先行固化的批次（决策后 state）在恢复时直接沿用，不重算
- **且** 恢复后以 git 真实状态为准，不盲目重跑已应用操作

### Requirement: 周期级失败语义

单分支失败不拖垮整周期，周期末必出报告。
#### Scenario 单分支失败不拖垮整周期。
- **当** 某目标分支内某 commit 失败/停批（fail-fast 或 forbidden_paths 转人工）
- **则** 分支内后续 commits 按决策 3 三层方案逐个判断关联性，相关停、无关继续（不整批一刀切）
- **且** 其他目标分支继续各自的批次（跨分支隔离）
- **且** 无论成败，周期结束必出报告 + 邮件（含失败明细、Failure Stage、Agent Attempts、Recommendation）

### Requirement: 异常分层

异常按 BsaError 分层处理。
#### Scenario 异常分类处理。
- **当** 异常发生
- **则** 按 BsaError 分层：DomainError（业务状态走状态转移，不抛异常）/ InfrastructureError（节点边界捕获写 state.errors）/ SafetyViolation（强制拒绝转人工）
