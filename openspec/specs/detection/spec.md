# detection Specification

## Purpose
TBD - created by archiving change add-branch-sync-agent. Update Purpose after archive.

## Requirements

### Requirement: 固定时间窗检测

每次 cron 触发按固定时间窗扫描 commit。
#### Scenario 每次 cron 触发扫描上一完整日 22:00~22:00（+08:00）的 commit。
- **当** Agent 运行检测阶段
- **则** 以固定时间窗 22:00~22:00 为扫描边界
- **且** 无增量基线、无状态推进，扫描幂等（同一窗口扫多次结果一致）

### Requirement: 同源矩阵构建

从 branch.md 构建同源矩阵，排除 feature/personal 分支。
#### Scenario 从 branch.md 构建同源矩阵。
- **当** 检测阶段开始
- **则** 读取 branch.md（人工维护，Agent 只读）
- **且** 同源判定双源结合（决策 35）：产品线归属 = branch.md section（人工标注）；同步方向 = develop 父 → 前缀下子 release/fix（源类型 develop，目标类型 release/fix）
- **且** feature/personal 分支在矩阵层面排除（非源非目标，不扫描）

### Requirement: commit 收集与元数据提取

对窗口内 commit 提取完整元数据并做性能保护。
#### Scenario 对窗口内 commit 提取元数据。
- **当** 窗口内有 commit
- **则** 提取 sha/message/author/committed_at/changed_files/patch_text/symbols/patch_id/issue_ids/源分支/同源段
- **且** 大 commit 保护：超 500 文件 / 2 万行 / 12 万字符则截断或跳过 diff

### Requirement: 跨周期去重

重复检测靠 git 真实状态去重。
#### Scenario 同一 commit 多周期出现。
- **当** commit 已应用进目标分支（git log target 可见）或已判定（judgments 缓存命中）
- **则** 不重复处理，靠 git 真实状态去重

### Requirement: git fetch 容错

fetch 失败采用指数退避重试并在超限后提前结束发失败报告。
#### Scenario fetch 失败。
- **当** `git fetch --all --prune` 失败
- **则** 指数退避重试（次数/间隔可配置）
- **且** 超过次数后周期提前结束，发失败报告邮件
- **且** 报告区分认证错误 vs 网络超时
