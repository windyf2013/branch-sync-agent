# safety Specification (delta)

## ADDED Requirements

### Requirement: 平台推送四道闸

平台推送仅在四道前置条件全部满足时允许，任一不满足即拒绝。

#### Scenario: 推送前置校验

- **当** 操作者在平台触发推送
- **则** 校验四道闸，任一不过则拒绝并显示原因：
  1. 分支状态为 SUCCESS（全部 cherry-pick + 全部 required_models 编译通过）
  2. worktree 存在
  3. 目标分支不在 safety_rules 的 forbidden_branches
  4. git status --porcelain 干净
- **且** 仅当前周期活 worktree 可推送；超期分支无推送入口

### Requirement: 平台推送执行约束

平台推送使用受限执行器，仅允许白名单命令且禁止 force。

#### Scenario: 推送命令受限

- **当** 平台执行推送
- **则** 仅允许 `git push origin HEAD:<target>`（目标分支白名单校验）
- **且** 禁止 `--force`；远端非 fast-forward 时 git 拒绝，不得绕过
- **且** 推送由人工触发并经二次确认，平台无任何自动推送逻辑
- **且** Agent 侧 git 白名单不新增 push，推送仅存在于平台受限执行器

### Requirement: 超期即弃安全边界

超过当前周期的分支不提供续做与推送，处理方式为基于当前远端重新同步。

#### Scenario: 超期分支仅重同步

- **当** 某分支现场超过当前周期
- **则** 不提供续做/推送入口，仅只读查看与重新同步（`bsa rerun --fresh`）
- **且** 不存在旧现场重建/重放机制，避免把旧 base 验证结果推上已前进的远端
