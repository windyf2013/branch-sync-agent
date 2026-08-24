# sync-execution Specification (delta)

## ADDED Requirements

### Requirement: 分支级同步 CLI

提供单目标分支的同步命令，支持源+目标分支与指定 commit 两种输入。

#### Scenario: 源+目标分支同步

- **当** 调用 `bsa sync <src> <target>`
- **则** 对目标分支执行完整链路：检测该源分支最近窗口 commit → 对目标重判结论 → 只同步 NeedSync
- **且** 结果写入周期记录，可供平台投影读取

#### Scenario: 指定 commit 直同步

- **当** 调用 `bsa sync <target> --sha <sha>`
- **则** 跳过决策直接对该 commit 执行同步链路（cherry-pick→build→patch）
- **且** 仍受白名单、编译验证与审计闸门约束，不因跳过决策而绕过安全闸门

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

### Requirement: 超期即弃

超过当前周期的分支现场不再提供续做与推送，仅支持重新同步。

#### Scenario: 超期分支处理

- **当** 某分支属于上一周期或更早
- **则** 其现场仅可只读查看（patch/日志），不提供续做/推送入口
- **且** 处理方式为基于当前远端重新同步（`bsa rerun --fresh`），不存在旧现场重建/重放机制
