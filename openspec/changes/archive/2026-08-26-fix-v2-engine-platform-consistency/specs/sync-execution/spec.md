# sync-execution Specification (delta)

## MODIFIED Requirements

### Requirement: 分支级重跑 CLI

提供单目标分支重跑命令，支持保留现场与重建两种语义。

#### Scenario: 保留现场续跑

- **当** 调用 `bsa rerun <target>` 且该分支存在当前周期活 worktree
- **则** 按 git 真实状态重入：未完成 cherry-pick 继续、已应用跳过，全量 build 验证后更新 patch
- **且** 现场有未提交修改时拦截并提示（防 Agent 自身残留混入）

#### Scenario: 重建重同步

- **当** 调用 `bsa rerun <target> --fresh`
- **则** 基于当前远端重建现场并先重判结论：若该 commit 已被合入（AlreadyIncluded/OutOfScope）则停止并提示
- **且** 重判后仍需同步时才执行同步链路，不产空 patch

#### Scenario: 重跑单一线程 id（P2-3）

- **当** 调用 `bsa rerun <target>`
- **则** retained 重跑生成**一个** rerun 线程 id，任务登记（register_start）与实际
  checkpoint 线程一致
- **且** fresh 重跑生成**一个** manual cycle id，登记与重建现场一致

## ADDED Requirements

### Requirement: manual-scan 登记任务

manual-scan 与每日周期统一进 tasks 表，cycle_id 一致。

#### Scenario: manual-scan 登记（P2-7）

- **当** 调用 `bsa manual-scan --since/--until`（或 `bsa run-cycle --since/--until`）
- **则** 引擎登记 kind=cycle 任务（source=cli）
- **且** 登记的 cycle_id 与 `run_cycle` 实际使用的 scan-* 周期 id 同源一致
- **且** 周期终态按 fold 规则折叠到 tasks.state
