# 分支同步引擎执行流程说明 V2

> 用途：方案文档。在 V1（`docs/engine-flow-demo.md`）基础上吸收评审结论，
> 新增基线编译、回滚先行、手动/自动共用内核、双身份标识、台账、互斥等决策。
> 内容与代码实现一致（`src/bsa/graph/*`、`src/bsa/agents/*`、`src/bsa_web/*`）。

---

## 1. 系统定位（不变）

**分支同步 Agent（Branch Sync Agent，简称 BSA）** 解决一个问题：

> 业务分支（如 `br_v4.34_develop_fttr_20260811`）上合入了 bug-fix 提交，需要把这些提交
> 逐个同步（cherry-pick + 编译验证 + 生成 patch）到主分支（如 `br_v4.34_develop_20260130`）。

**架构原则**：

- **Workflow 控制确定性流程**：做什么、按什么顺序做、失败往哪走，全部由流程图（LangGraph）
  决定，可检查点续跑。
- **Agent 只做复杂判断**：需要语义理解的地方（判定是否 bug-fix、解决冲突、归因编译错误、
  判断提交关联性）才调用 LLM。
- **安全闸门（SafetyEnforcer）永远高于 LLM**：禁止修改的路径、禁止同步的分支、单次修改
  行数上限，LLM 不可覆盖。

**V2 新增两条原则**：

- **单内核双入口**：cron（自动周期）与平台手动同步共用同一套同步内核，仅入口薄壳不同，
  零冗余。
- **服务器级任务不介入人工**：执行期全自动（含 LLM 不可用 = 失败 = 转人工），全部完成后
  统一由人工事后审核。

---

## 2. 两条入口（V2 修订）

| 入口 | 场景 | 区别 |
|---|---|---|
| 自动周期（cron） | 每日 0:00 触发 | detect + decision + 同步内核 |
| 手动同步（平台） | 用户在页面勾选 commit | 跳过 detect/decision（=人工审核），直接进同步内核 |

手动同步要点：

- 批次按合入时间升序重排（用户勾选顺序不可靠，保证依赖顺序正确）
- 编译型号按目标分支产品线自动解析（未配置 → 报错停止，绝不用错脚本）
- 与 cron 共用 prepare / 基线编译 / 逐 commit / 回滚 / report 全部逻辑，**零冗余**

---

## 3. 同步内核（V2 核心改动）

```
# 同步拓扑（V3 写死语义）：branch.md 标题人工标注角色
#   含"主分支"的 section → 目标（只收不扫，建 worktree + 基线编译 + 逐 commit）
#   含"业务分支"的 section → 源（只扫判定，不建 worktree / 不编译 / 不出 patch）
#   业务 → 主 单向；业务之间互不同步；主不向业务回灌。

for 主分支（目标）:
  prepare_worktree（远端 tip 建/复用）
    → baseline_build（主分支 tip 全量编译，未改动基线）   # V3 落地：先证可编译
        ├─ 失败 → 分支 blocked（status=FAILED + stop_reason）→ 移下一主分支
        └─ 通过 →
            for commit in 批次（合入时间升序）:
              cherry-pick
                ├─ EMPTY   → 记录已含，跳过编译        # 基线已证，EMPTY 不改变树
                ├─ CONFLICT→ resolve（LLM+安全闸门+字节快照+3轮）
                └─ OK      → 增量编译（不再批次首 commit 全量，基线已证）
                               ├─ 通过 → 下一 commit
                               └─ 失败 → fix_build（归因+最小修复）
                                           → 3 轮失败 → 回滚
              回滚先行 → 关联判断（文件/区域/LLM/编译兜底）
                        → 相关停批 / 无关继续
            → generate_patch + 状态汇总
  → report / action_required
```

**关键决策**：

- **基线编译前置消解 pre_existing 归因**：分支 tip 已证可编译，之后任何编译失败
  （干净状态下）只剩 `introduced_by_commit` / `environmental` / `unresolvable` 三类。
- **失败 commit 必须先回滚出树**，再谈后续 commit 的"无关则继续"。
- **build fix 本地化**：只随所在分支 patch 交付，不跨分支作为 bug-fix 同步。
- **每次 LLM 修改（冲突解决 / build 修复）强制产出独立 diff**，落台账，供事后审核与追责。
- **LLM 不可用 = 节点失败 = 转人工（action_required）**，统一约定，无"重试恢复"语义。

---

## 4. 核心模块

### 4.1 检测模块（detect_commits）

- 窗口：每日 0:00 执行，前一日 22:00–22:00 为窗口（24h + 2h 沉降缓冲）
- 时间字段用 **committer date**（合入时间），不用 author date（rebased commit 会漂移）
- **merge 提交展开**：`--first-parent` 定位窗口内 merge → `merge^1..merge` 取 PR 引入的
  commit → patch-id 去重后分类
- **只扫业务分支**（branch.md 标题含"业务分支"的 section）：主分支作为目标不被扫描，
  消除 V2 全互联的回声重检与平方级无效工作（V3 写死语义）
- 历史积压不在周期范围内，由平台手动任务兜底（用户自处理）

### 4.2 决策模块（sync_decision）

四态结论（`conclude_pair`）：

| 结论 | 含义 | 处置 |
|---|---|---|
| **NeedSync** | 目标分支缺这个修复，需要同步 | 进入该分支的同步批次 |
| **AlreadyIncluded** | 目标分支已经包含这个修复 | 跳过（patch-id 判定） |
| **OutOfScope** | 目标分支不该同步它（非同源/类型不符/禁同步清单） | 跳过并记录原因 |
| **ManualReview** | 判定有风险或有歧义 | 转人工，进待办区 |

- 规则优先级高于 LLM；人工判定（judgments）优先级最高，按 **fingerprint** 匹配
- risk 分级参与展示（显著标识），供事后审核排序
- 手动同步跳过此步（用户勾选 = 人工审核通过）

**同步拓扑（V3 写死语义，`build_matrix`）**：不靠分支名推断方向，直接读
branch.md section 标题 —— 含"主分支" → 目标，含"业务分支" → 源。同一产品线
（剥编号与角色后缀后的前缀）的业务 section → 主 section，**单向**。业务之间
互不同步；主不向业务回灌；未标注 section 不产出同步边（安全默认）。
`conclude_pair` 的 AlreadyIncluded（patch-id / issue-id / 相似度）仍负责
"是否已包含"的去重，避免同窗口重复入批。

### 4.3 冲突解决模块（resolve_conflict / ConflictAgent）

- 安全前置：冲突文件命中 `forbidden_paths` → 直接转人工
- 编码无损：UTF-8 / GB18030（GBK 超集）可解才处理；真二进制转人工
- 校验四关：冲突标记消失 + `git diff --check` 干净 + 只改冲突文件 + 未触安全红线
- 快照回滚重试，最多 3 轮；每次产出独立 diff

### 4.4 编译模块（build / baseline_build）

- **主分支基线编译（baseline_build）**：prepare_worktree 建好 worktree 后、首个
  cherry-pick 前，对原始 tip 全量编译（clean）。失败 → 分支 blocked（FAILED +
  stop_reason），不进入逐 commit。业务分支不经过此步（只当源，不建 worktree）。
- **批次首 commit 不再重复 clean**（基线已全量验证）；改动公共文件仍降级全量编译
  （决策 2）。EMPTY 跳过编译；commit 之间增量编译。
- 产品线 → 编译型号随目标分支解析，**不是全局一个型号**；查不到 → 报错停止
- 多个型号串行编译，任一失败即停该 commit
- 成功判定三查：退出码 0 + 成功标志 + 产物存在完整

### 4.5 编译错误归因与修复（fix_build / BuildAgent）

- 归因三类（基线已证可编译）：`introduced_by_commit` / `environmental` / `unresolvable`
- environmental 支持重试策略；unresolvable 才判死
- 修复边界：仅"本次 commit 改过的文件 + 日志指向文件"，SafetyEnforcer 强制
- 快照回滚，最多 3 轮；每次修复产出独立 diff

### 4.6 停批判断模块（fail_fast）

- **前置动作：失败 commit 回滚出树**
- 三层关联判断：文件级（无 `changed_files` 交集 → 继续）→ 区域级（hunk 行区间相距远 → 继续）
  → LLM（模糊者判断；LLM 不可用 → 保守停批）
- 编译兜底：判"无关继续"的后续 commit 若编译失败且归因指向前失败 commit 中间态 →
  回滚该中间态或改判停批
- 分支内局部，不影响其他目标分支；停批记 `stop_reason`，页面可见

### 4.7 交付与收尾（generate_patch / report）

- `git format-patch 原tip..HEAD`，每分支独立一份
- 终态收敛：SUCCESS / FAILED / PARTIAL，写入 cycle.json / 任务状态
- 人工项统一进 action_required：ManualReview、冲突/编译转人工、基线失败分支

---

## 5. 数据与可靠性（V2 新增）

> **实现状态标注**：以下各节按「已实现 / 部分实现 / 规划中」标注，避免文档与代码脱节。
> 已实现项均有测试覆盖；规划中项为后续增强方向，当前不阻塞使用。

### 5.1 双身份标识（已实现）

- **变更身份 `patch_id`**（`git patch-id --stable`）：同一次代码变更，无论被 rebase /
  cherry-pick 多少次、sha 如何变，patch-id 稳定。用于 AlreadyIncluded 判定与台账主键。
- **语义身份 `fingerprint`** = `sha256(subject + body + patch_id)`（`compute_fingerprint`）：
  用于判断类缓存（is_bug_fix / risk / 人工覆盖）。rebase 不改内容 → 命中；内容改动 →
  重新判定。人工覆盖按 fingerprint 匹配（judgments.json 的 `fp:<fingerprint>` 键），
  rebase 后 sha 漂移仍命中；内容变了自动失效。**UI 提示"覆盖失效"尚未实现**（规划中）。

### 5.2 同步台账 ledger（append-only，已实现）

`{log_dir}/ledger.json`，每行一条：
`{patch_id, target_branch, source_sha, status(synced), ts}`。

- 检测时按 `(patch_id, target_branch)` 查台账（`is_synced`）：synced → AlreadyIncluded，
  跳过快照构建与相似度/LLM 判定，跨天/跨周期幂等（已接入 `sync_decision`）
- `generate_patch` 对成功同步的 commit 记录 synced（append-only）
- **全操作记录 / LLM 修改 diff 归档**：规划中（当前仅 synced 一条；failed/blocked/review
  与 llm_diffs 归档尚未实现）

### 5.3 互斥（flock，部分实现）

- **全局锁 `{log_dir}/bsa.lock`**（已实现）：cron 周期（`run_cycle`）与手动同步
  （`run_sync_command`）启动均取此锁 → **手动与 cron 串行**，同一时刻只有一个同步在跑。
- **分分支锁 `branch_lock_path`**（能力已提供 `locks/branch/<branch>.lock`，**未接入工作流**）：
  因全局锁已保证手动/cron 串行，分分支锁当前为冗余能力。**规划中**：若未来去掉全局锁、
  允许手动与 cron 并行处理不同分支，则在此接入（取锁按分支名排序防死锁；
  手动遇被占 → 有界等待提示"分支忙"；周期遇被占 → 先做其他再回头）。

### 5.4 窗口边界（cron 专用，已实现）

- 22:00–22:00 + 2h 沉降，committer date 过滤，merge 展开（见 4.1）
- 历史积压由平台手动任务兜底，周期不扫

### 5.5 推送（能力边界，硬约束，已实现）

- 仅当前周期活 worktree + SUCCESS + 四道闸（状态/存在/禁推分支/工作树干净）
- **禁 `--force`**；人工触发，Agent 不自动推；平台是执行器不是决策者
- 实现：`bsa_web/api/push.py`（confirm 回显 + 四道闸 → 受限 `git push` + 审计），
  仅此端点触发推送，无自动/定时推送路径

---

## 6. 一句话总结

确定性流程串起「检测 → 决策 → 同步内核（基线编译 + 逐 commit + 回滚 + 关联判断）→ 交付」；
手动与自动共用内核，零冗余；基线编译前置消解归因；patch-id / fingerprint 双身份；
台账收口幂等与审计；flock 保证互斥；LLM 修改全部产出独立 diff 供事后审核。
