# sync-execution Specification

## Purpose
TBD - created by archiving change add-branch-sync-agent. Update Purpose after archive.

## Requirements

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
- **且** cherry-pick EMPTY（内容已应用）不引入改动，跳过编译（建立 worktree 时的基线全量编译已验证目标 tip）
- **且** 基线编译（prepare 后）为唯一 clean 全量；commit 间按改动文件解析编译模块（build_rules.yaml 的 build_modules 路径前缀映射），解析不出（未命中 / 多模块）才回退非 clean 全量
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

#### Scenario: 重跑单一线程 id（P2-3）

- **当** 调用 `bsa rerun <target>`
- **则** retained 重跑生成**一个** rerun 线程 id，任务登记（register_start）与实际
  checkpoint 线程一致
- **且** fresh 重跑生成**一个** manual cycle id，登记与重建现场一致

### Requirement: 超期即弃

超过当前周期的分支现场不再提供续做与推送，仅支持重新同步。

#### Scenario: 超期分支处理

- **当** 某分支属于上一周期或更早
- **则** 其现场仅可只读查看（patch/日志），不提供续做/推送入口
- **且** 处理方式为基于当前远端重新同步（`bsa rerun --fresh`），不存在旧现场重建/重放机制

### Requirement: manual-scan 登记任务

manual-scan 与每日周期统一进 tasks 表，cycle_id 一致。

#### Scenario: manual-scan 登记（P2-7）

- **当** 调用 `bsa manual-scan --since/--until`（或 `bsa run-cycle --since/--until`）
- **则** 引擎登记 kind=cycle 任务（source=cli）
- **且** 登记的 cycle_id 与 `run_cycle` 实际使用的 scan-* 周期 id 同源一致
- **且** 周期终态按 fold 规则折叠到 tasks.state
