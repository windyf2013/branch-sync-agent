# safety Specification

## Purpose
TBD - created by archiving change add-branch-sync-agent. Update Purpose after archive.

## Requirements

### Requirement: 五道闸门安全模型

Agent 写操作通过五道闸门强制，永不推送分支。
#### Scenario Agent 具备真实写权限，安全靠代码层强制。
- **当** Agent 执行任何写操作（git 写 / 文件编辑 / 编译）
- **则** 通过五道闸门：能力（白名单前置锁死）/ 正确性（编译+修改合理性+diff 留存）/ 禁止规则（safety_rules 红线）/ 审批（全事后）/ 审计（全落盘）
- **且** Agent 永不推送分支（能力边际，硬约束），推送必须由人工执行

### Requirement: Git 命令白名单

git 命令受 WhitelistExecutor 白名单强制。
#### Scenario git 命令受 WhitelistExecutor 强制。
- **当** GitService 执行 git 命令
- **则** 仅允许 fetch/checkout/cherry-pick/log/diff/show/format-patch/worktree/merge-base/status
- **且** 非白名单命令直接拒绝（SafetyViolation），LLM 无法绕过

### Requirement: 文件编辑安全

Agent 文件编辑受允许文件集合与 forbidden_paths 约束。
#### Scenario Agent 编辑代码受安全约束。
- **当** Conflict/Build Agent 修改文件
- **则** 编辑工具只接受"当前任务允许的文件集合"参数
- **且** SafetyEnforcer 在编辑前校验 forbidden_paths，命中即拒绝转人工
- **且** 修改 diff 独立落盘（审计）

### Requirement: 安全红线数据驱动

安全规则以 yaml 数据驱动且 LLM 不可覆盖。
#### Scenario 安全规则可维护且 LLM 不可覆盖。
- **当** 需要调整安全红线
- **则** 修改 safety_rules.yaml（forbidden_paths / required_models / forbidden_branches / max_single_edit_lines）
- **且** 新增红线只改 yaml 不碰代码（通用 SafetyEnforcer 校验器）
- **且** 安全规则由代码层强制，不交给 LLM 判断

### Requirement: 配置与密钥隔离

真实路径与密钥走环境变量不硬编码。
#### Scenario 真实仓库路径与密钥不硬编码。
- **当** 配置加载
- **则** 仓库路径/SSH/API key/模型名走环境变量 + pydantic-settings
- **且** 代码与文档禁止硬编码真实路径/密钥

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
