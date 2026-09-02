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

#### Scenario: merge commit 提取第一父聚合 diff

- **当** 检测到 merge commit
- **则** `patch_text` 使用第一父聚合 diff（`diff-tree -r <first_parent> <sha>` 语义，与 `changed_files` 一致）
- **且** `changed_files` 仍为第一父聚合改动文件列表（现有行为不变）
- **且** 大 commit 保护沿用现有上限（超 500 文件 / 2 万行 / 12 万字符则截断或为空）

#### Scenario: patch_id 对 merge 不再恒空，非 merge 语义不变

- **当** 计算 commit 的 `patch_id`
- **则** `patch_id` 与 `patch_text` 统一基于第一父聚合 diff（`diff-tree -p -r <first_parent> <sha>`）
- **且** 非 merge commit 的 `patch_id` 与「基于 `git show` 的稳定 patch-id」一致（`git show` 与 `diff-tree -p` 对非 merge 等价）
- **且** merge commit 的 `patch_id` 为内容派生的互异值（不再所有 merge 同一恒空值），使 `_check_patch_id_match` 与台账 `(patch_id, target)` 不再把不同 merge 误判为同一 commit
- **且** `fingerprint = sha256(subject+body+patch_id)` 语义不变：非 merge 值不变，merge 因 patch_id 从恒空修成互异而获得区分度
- **且** 根 commit（无父）保持返回 None（`_too_big` 语义不变）

#### Scenario: 变更统计始终落盘

- **当** 提取 commit 元数据
- **则** `diff_stat` 记录 `{files, insertions, deletions}`（第一父聚合 `diff-tree --numstat -r <parent> <sha>` 语义）
- **且** `diff_stat` 与 `changed_files`/`patch_text` 同源，merge 与普通 commit 均产出
- **且** 超大 commit 即使 `patch_text` 因超限截断为空，`diff_stat` 仍保留文件数与增删行统计，不因超限丢弃

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
