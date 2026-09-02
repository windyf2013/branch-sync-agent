# Design: fix-projection-and-override-semantics

## 架构总览

纯引擎数据契约 + override 语义修复，不引入新节点、不改路由、不改安全边界、不改四态判定结果。四块改动独立：

```
① TaskState 补 sources/targets → 完整拓扑（含零检出源）落盘
② commit_patch 对 merge 走第一父聚合 + diff_stat → merge 有 diff 有统计
③ Conclusion4 加 cause 枚举 → ManualReview 成因结构化，web 精确渲染控件
④ override 语义收紧 → is_bug_fix 键缺失不覆盖 + 加清除 + 不变量澄清
```

## 改动 1：TaskState 补齐 sources/targets

- **文件**：`src/bsa/graph/state.py` `TaskState` 增加 `sources: list[str]`、`targets: list[str]`。
- **机制**：LangGraph 以 TypedDict 声明为 state 建 channel；未声明键在节点返回的 update 中被静默丢弃（`cycle-2026-09-01` 实测确认）。声明后 `detect_commits` 返回的 update 进入 channel、落 checkpoint。
- **语义**：`detect_commits` 已按 `ctx.matrix` 计算全集（`sorted({s.name for hs in ctx.matrix for s in hs.sources})`），是「本应扫描的源全集」，零检出源自动包含。
- **兼容性**：旧 checkpoint/state.json 无该字段，`projection_payload` 的 `or []` 兜底。无迁移。

## 改动 2：commit_patch / patch_id 统一第一父聚合 + diff_stat

- **文件**：`src/bsa/git/service.py`（`commit_patch` + `patch_id` + 新增 `diff_stat`）；`src/bsa/domain/models.py`（`CommitInfo.diff_stat`）。
- **根因**：`patch_id()` 用 `git show`，而 `git show` 对 merge 恒空 → `_stable_patch_id("")` 对所有 merge 是**同一个值**。连带：`conclude._check_patch_id_match` 把「目标上有任一 merge」误判成「源 merge 已包含」；台账 `(patch_id, target)` 把不同 merge 当同一个 commit。
- **方案**：`commit_patch` 与 `patch_id` 统一走第一父聚合 `git diff-tree --no-commit-id -p -r <first_parent> <sha>`（`changed_files` 同源）。非 merge 语义不变（实测本仓 `git show` 与 `diff-tree -p` 字节一致，`_stable_patch_id` 只哈希内容行，结果相同）；merge 从「所有 merge 同一恒值」修成「内容派生的互异 patch_id」——修 bug，不改语义。
- **merge 判定**：复用 `_parent_sha` 的 `%P` 解析；根 commit（无父）`_parent_sha` 返回 None，`commit_patch`/`patch_id` 保持返回 `""`/None（现有 `_too_big` 语义不动）。
- **diff_stat**：数据源 `git diff-tree --numstat -r <first_parent> <sha>`，聚合 `{files, insertions, deletions}`，**不受 `_numstat` 超限丢弃**（`_numstat` 超限返回 None 仅为判大，`diff_stat` 单独计算、不设截断上限）。
- **关键边界**：台账幂等 `(patch_id, target)` 与 `fingerprint = sha256(subject+body+patch_id)` 的**语义**不变——非 merge 的值完全不变；merge 从恒空修成互异是修复「patch_id 对 merge 无区分度」的 bug。`_too_big`/`_numstat` 判大逻辑不动。
- **预期收益（非回归）**：merge 的 patch_text/diff_stat 变完整、patch_id 变互异，`classify`/sync_decision LLM 输入变全，且 `_check_patch_id_match`/台账不再把 merge 误判已包含（本周期 4 个 ManualReview merge 大概率因此得到更准判定）。

## 改动 3：Conclusion4 增加 cause 结构化成因

- **文件**：`src/bsa/domain/models.py` `Conclusion4` 加 `cause: Literal[...] | None`（默认 None）。
- **取值**（仅 ManualReview 填，对应 `conclude.py` 各返回点）：

| cause | evidence 现状 | override 有效 | 有效字段 |
|---|---|---|---|
| `pending` | "LLM 未判定（pending）" | ✅ | `is_bug_fix=true` |
| `severity_gate` | "fix 分支仅接收 high…" / 回灌门控 | ✅ | `risk=high` |
| `fix_missing` | "修复是否缺失无法判定" | ✅ | `risk=high` |
| `function_renamed` | "函数/符号疑似重命名" | ❌ | — |
| `similarity_gray` | "相似度处于灰区" | ❌ | — |
| `symbols_missing` | "关联符号未全部存在" | ❌ | — |
| `unknown_branch_type` | "目标分支类型未知" | ❌ | — |

- **实现**：`conclude_pair` 每处 `return Conclusion4(kind="ManualReview", ...)` 补 `cause=...`；`_need_sync_confidence`/NeedSync/AlreadyIncluded/OutOfScope 返回点 `cause` 保持 None。
- **纯标注**：不改判定结果、不改优先级，只给结论加结构化标签；web 据此精确渲染控件，不再靠 evidence 文案猜成因。

## 改动 4：override 语义收紧

### 4a. classify_commit 与 SyncDecisionAgent 键缺失不覆盖 is_bug_fix

- **文件**：`src/bsa/rules/classify.py` `classify_commit`（第 243-257 行）；`src/bsa/agents/sync_decision.py` `run`（42-49 行）与 `_from_entry`（105-117 行）。
- **缺陷（两处同构）**：`bool(judgment.get("is_bug_fix"))` 把「无 is_bug_fix 键」（如只写了 risk 的 judgment）当 `False`。classify 层误判成 not-bug-fix；Agent 层 `_from_entry` 还会把 `recognition_source="manual-override"` 当非 pending，`needs_agent` 被压成 False——对 pending commit 只写 `--risk high` 时，本想让 LLM 判 is_bug_fix，结果被 Agent 层当非 bug-fix 短路，`--risk` 想推过 `severity_gate`/`fix_missing` 门控的目的达不成。
- **修复**：区分「显式 false」与「键缺失」。classify 层：键缺失时不覆盖 `is_bug_fix`，继续走机器规则/LLM。Agent 层：`is_bug_fix` 键**显式存在**才短路 LLM；键缺失（只写 risk）时不短路，交 LLM 判 is_bug_fix，仅把 entry 的 risk 当覆盖应用（规则层 `prior_risks` 优先级仍最高）。`risk` 单独读取，不与 is_bug_fix 耦合。

### 4b. apply_override 加 clear

- **文件**：`src/bsa/commands/override.py` `apply_override`；`src/bsa/cli.py` `_cmd_override`。
- **缺陷**：`apply_override` 只合并写，无删除能力，override 写入后无法回退。
- **修复**：`apply_override` 加 `clear: bool = False` 分支——`clear=True` 时删除 `sha` 键，再用 `message`+`patch_id` 现算 `fp_key = "fp:..."` 直接删除（fp 是派生别名键，非一一对应映射，无需维护关联）；sha 不可达、patch_id 查不出时降级只删 sha 键、接受 fp 残留（罕见路径）。CLI 加 `--clear`，与 `--is-bug-fix`/`--risk` 互斥。

### 4c. 不变量 #5 表述澄清

- **文件**：`CLAUDE.md` 不变量 #5 与 `decision` spec。
- **缺陷**：「人工覆盖 > 规则 > LLM」未说明覆盖的作用层，读起来像「人工能在任何一层推翻一切」。
- **修复**：表述精确为——人工覆盖作用于**分类与风险**（classify 层，`is_bug_fix`/`risk`），压过规则与 LLM；**四态结论由规则层按 git 快照独立判定，客观结论（已包含/不同产品线）不接受人工覆盖**——人工对四态的拍板通过「确认继续/放弃」实现，而非 override。

## 数据流与边界

- 只改数据产出（state 字段、patch_text/patch_id/diff_stat、cause）与 override 写读语义，不改 Workflow 流程 / Agent 判断 / Tool 白名单 / SafetyEnforcer / `conclude_pair` 判定逻辑。
- `projection_payload` 已读 `sources`/`targets`，无需改；`cause` 经 `Conclusion4.model_dump()` 自动进 `decisions`，无需改投影。

## 测试策略

- `state.py` 字段：graph 投影单测——含零检出源的 matrix 跑 detect_commits 后 `state.json` 的 `sources`/`targets` 含全集。
- `git/service.py`：merge `commit_patch` 非空、`patch_id` 互异（不同 merge 不同值、不再恒空）、非 merge `patch_id` 不变、`diff_stat` 产出统计（超限不丢）。
- `classify.py`：键缺失不覆盖 is_bug_fix（只写 risk 的 judgment 不再误判 false）；显式 false 仍生效。
- `sync_decision.py`：`run`/`_from_entry` 对只写 risk 的 judgment 不短路 LLM、不把 `needs_agent` 压成 False；显式 is_bug_fix 仍短路。
- `override.py`/`cli.py`：`--clear` 删除 sha 与 fp 键、与写字段互斥。
- `conclude.py`：ManualReview 各返回点 cause 正确、非 ManualReview 的 cause 为 None。
- 全量 `uv run pytest` + `uv run ruff check src` 通过。

## 验收标准（结果导向）

1. 用真实 `cycle-2026-09-01` 数据重跑：`state.json` 顶层 `sources` 含两个源（含零检出 `br_v4.34_MSG_develop_20260805`）、`targets` 含目标；merge commit 的 `diff_stat`/`patch_text` 非空、`patch_id` 互异；4 个 ManualReview 的 `decisions[sha][target].cause == "pending"`。
2. override 实操：`bsa override <sha> --risk high` 不再把该 commit 误判为 not-bug-fix；`bsa override <sha> --clear` 后回到未判定。
3. web 走查：周期概览/工作台显示完整拓扑与零检出标注；commit 详情页显示 merge 统计；ManualReview 项按 cause 显示对应控件。
4. 回归：`uv run pytest` 全绿、`uv run ruff check src` 零告警。
