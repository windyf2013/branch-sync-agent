# Capability: sync-execution

## ADDED Requirements

### Requirement: worktree 准备

为目标分支创建独立 worktree，周期内全保留。
#### Scenario 为目标分支创建独立 worktree。
- **当** 目标分支需要同步
- **则** 从该分支远程最新 tip（origin/<branch>）检出独立 worktree
- **且** worktree 命名含 cycle_id，周期内全保留（正常与失败分支），超过一周期删除

### Requirement: 按序 cherry-pick

批次内按合入顺序逐 commit cherry-pick。
#### Scenario 批次内逐 commit cherry-pick。
- **当** 执行某分支批次
- **则** 按合入顺序（git log --reverse）逐 commit cherry-pick
- **且** cherry-pick 前用 merge-base --is-ancestor 确认未应用
- **且** cherry-pick 变空提交视为成功跳过（状态记为 EMPTY，即已应用），不报错不计失败，继续下一个

### Requirement: Conflict Agent 解决冲突

cherry-pick 冲突由 Conflict Agent 安全解决。
#### Scenario cherry-pick 冲突。
- **当** cherry-pick 产生冲突
- **则** Conflict Agent 分析冲突并修改（仅 unmerged 冲突文件，SafetyEnforcer 强制）
- **且** 每轮修改前快照冲突文件，失败/越界恢复快照重试，max_attempts=3
- **且** 校验：冲突标记消失 + git diff --check 干净 + 仅冲突文件改动 + 未触安全红线
- **且** 质量底线：必须理解功能实现与合入目的综合判断，禁止机械删标记、禁止无理由丢弃任一边改动
- **且** 冲突文件命中 forbidden_paths → 禁止自动解决，转人工（进 Action Required）
- **且** 用尽后触发 fail-fast 关联判断

### Requirement: 多型号编译验证

每 commit 在目标分支 worktree 上按型号矩阵编译验证。
#### Scenario 每 commit 编译验证。
- **当** cherry-pick 完成后
- **则** 在目标分支 worktree 上编译（docker 容器挂载 worktree，exec 执行 RTL9617C_build.sh）
- **且** 批次开头 clean 一次，commit 间增量编译；公共文件改动时降级全量编译（额外 clean）
- **且** 按 required_models 顺序逐个型号编译，某型号失败即停该 commit
- **且** 编译成功判定三查：产物存在且完整 + 日志成功标志 + 容器状态

#### Scenario 编译日志解析（决策 26）。
- **当** 编译失败需要解析日志
- **则** 错误提取：错误行前后各 20 行上下文，总量 ≤3000 行；错误 >500 条取前 500 + 末尾 100
- **且** 区分 error/warning/note：warning 与 #pragma message note 不进 LLM 错误输入，仅提取 error: 行
- **且** 关键陷阱：`fatal:` 未必致命（如 `fatal: not a git repository` 是 make 探测 .git 的非致命警告），不得单凭 fatal 判失败，须结合 error 行与退出码综合判定
- **且** 编译日志落盘 `logs/<cycle_id>/build/<branch>/<commit>/build.log`

### Requirement: Build Agent 修复编译错误

编译失败由 Build Agent 归因并仅修复本次引入的错误。
#### Scenario 编译失败。
- **当** 某型号编译失败
- **则** Build Agent 归因分类（本次引入 / 原分支已有 / 环境问题 / 无法修复）
- **且** 仅"本次引入"允许自动修复（仅 commit 文件 + 报错文件，SafetyEnforcer 强制），快照回滚，max_attempts=3
- **且** 疑似"原分支已有"时切原 tip 编译对比确定性验证
- **且** 报错文件命中 forbidden_paths → 转人工
- **且** 用尽后触发 fail-fast 关联判断

### Requirement: fail-fast 三层关联判断

冲突/编译无法解决时三层判断后续 commit 关联性决定是否停批。
#### Scenario 冲突/编译无法解决，判断是否停批。
- **当** 某 commit 失败（冲突 3 轮或编译 3 轮无法解决，或 forbidden_paths 命中转人工）
- **则** 三层判断与后续 commits 关联性：文件级（无交集→继续）→ 区域级（行区间远→继续）→ LLM（兜底，有关联→停批）
- **且** 分支内：只停当前失败 commit 及判定相关的后续 commits，无关 commits 继续同步（不整批一刀切）
- **且** 跨分支：某目标分支停批不影响其他目标分支

### Requirement: patch 生成

批次完成后按分支独立生成 format-patch。
#### Scenario 批次完成后生成 patch。
- **当** 某分支批次完成（或失败停止）
- **则** 生成 `git format-patch <原tip>..<HEAD>`（含冲突解决修改）
- **且** 每分支独立一份，命名含 cycle_id / 分支 / commit 范围
