# detection Specification Delta

## MODIFIED Requirements

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
