# 实现计划：fix-projection-and-override-semantics

## 来源

- 项目配置：`openspec/config.yaml`
- 当前规格：`openspec/specs/`（decision / detection / reporting）
- 提案：`openspec/changes/fix-projection-and-override-semantics/proposal.md`
- 设计：`openspec/changes/fix-projection-and-override-semantics/design.md`
- 规格：`openspec/changes/fix-projection-and-override-semantics/specs/`
- 任务：`openspec/changes/fix-projection-and-override-semantics/tasks.md`

## Project Context

- 目的：Branch Sync Agent 定期检测 RCIOS 代码库各分支合入 commit，判断是否需同步；在独立 worktree 完成 cherry-pick、冲突处理、多型号编译验证、diff-patch 生成，输出 HTML 报告 + 邮件。Agent 只做复杂判断，不直接控制 Git/Build 确定性操作。
- 技术栈：Python 3.13 + uv；LangGraph（单周期 = 单 thread_id = 单 checkpoint，SQLite checkpoint）；OpenAI 兼容 LLM 接口；pydantic 数据模型；PyYAML。
- 目标仓库：嵌入式 C（RCIOS），GitLab SSH；branch.md 同源矩阵清单。
- 构建环境：Docker 容器内编译，型号 RTL9617C 等，`required_models` 由 safety_rules.yaml 单一驱动。
- 架构：主图（LangGraph）只控流程与状态；3 个 Agent 子图在复杂判断点介入；GitService/BuildRunner 确定性执行层；规则层（classify + conclude 四态）先于 LLM；四态 NeedSync/AlreadyIncluded/ManualReview/OutOfScope。
- 容错：节点级隔离（异常入 state 不崩）、LLM 级（timeout/重试/降级安全默认）、Tool 级（timeout/幂等/git 真实状态为准）、周期级（单分支失败不拖垮整周期）。
- 测试：pytest + ruff；规则层带 golden-set；git 操作可 mock。
- 约束：只生成 patch 不推送；只操作用独立 worktree；不把真实路径/密钥硬编码。

## Applicable OpenSpec Rules

- 引用现有 spec 再描述新行为，不得凭空发明；每条需求必须有具体场景和验收条件。
- 保持既有架构模式（Workflow 管流程、Agent 管判断、Tool 管执行）。
- 每个实现任务以测试通过为完成条件；给出精确文件、测试、验证命令和回滚说明。
- 任何 node 出错不得让 workflow 崩溃，异常写入 state 并走报告；LLM 调用必须有 timeout + 重试 + 降级安全默认。
- 不把真实仓库路径/密钥硬编码进文档或代码，用占位符 + 环境变量注入。

## Goal

修复引擎侧三个数据契约缺陷，使平台能「正确呈现 + 正确给操作」：完整拓扑（含零检出源）落盘投影、merge commit 有 diff/统计/互异 patch_id、ManualReview 带结构化成因、override 语义精确（键缺失不覆盖、可清除）。**及格线：重跑 `cycle-2026-09-01` 后，web 上 merge 有内容、拓扑完整、4 个 ManualReview 均带 `cause=pending`，override 实操不误判、可回退。**

## Non-Goals

- 不改四态判定结果、不改 `conclude_pair` 判定逻辑。
- 不改 ledger/fingerprint 的**语义**（非 merge 的 patch_id 不变；merge 从恒空修成互异是修 bug）。
- 不新增「强制同步/强制排除」的四态 override。
- 不写离线回填脚本补历史周期缺失字段——存量降级、增量完整。
- 不改 Workflow 流程 / Agent 判断（is_bug_fix 判定逻辑本身）/ Tool 白名单 / SafetyEnforcer。

## Source Coverage

| OpenSpec 来源 | 验收点 | 对应 slice |
|---------------|--------|-----------|
| `reporting` / 投影含完整拓扑字段 | `sources` 含零检出源、`targets` 全集、缺失兜底 | Slice 1 |
| `detection` / merge commit 提取第一父聚合 diff | `patch_text` merge 非空、与 changed_files 同源 | Slice 2 |
| `detection` / patch_id 对 merge 不再恒空 | merge patch_id 互异、非 merge 不变、根 commit None | Slice 2 |
| `detection` / 变更统计始终落盘 | `diff_stat` merge/普通均产出、超限不丢 | Slice 2 |
| `reporting` / 四态结论透出 cause | ManualReview 带 cause、非 ManualReview cause=null | Slice 3 |
| `decision` / 只写 risk 不覆盖 is_bug_fix | classify 与 sync_decision 两层键缺失不覆盖 | Slice 4 |
| `decision` / 清除覆盖判定 | `--clear` 删 sha + fp 键、互斥、回到未判定 | Slice 4 |
| `decision` / 人工覆盖与四态结论分层边界 | 不变量 #5 表述澄清 | Slice 5 |
| `tasks.md` T1 | 投影拓扑字段 | Slice 1 |
| `tasks.md` T2 | patch_id/diff_stat | Slice 2 |
| `tasks.md` T3 | cause 字段 | Slice 3 |
| `tasks.md` T4 | override 收紧 | Slice 4 |
| `tasks.md` T5 | 不变量 #5 | Slice 5 |

## File Responsibility Map

| 文件 | 操作 | 责任 | 相关 slice |
|------|------|------|------------|
| `src/bsa/graph/state.py` | modify | `TaskState` 补 `sources`/`targets` | Slice 1 |
| `src/bsa/git/service.py` | modify | `commit_patch`/`patch_id` 统一第一父聚合 + 新增 `diff_stat` | Slice 2 |
| `src/bsa/domain/models.py` | modify | `CommitInfo.diff_stat`、`Conclusion4.cause` | Slice 2, 3 |
| `src/bsa/rules/conclude.py` | modify | 各 ManualReview 返回点补 `cause` | Slice 3 |
| `src/bsa/rules/classify.py` | modify | `classify_commit` 键缺失不覆盖 is_bug_fix | Slice 4 |
| `src/bsa/agents/sync_decision.py` | modify | `run`/`_from_entry` 键缺失不短路 LLM | Slice 4 |
| `src/bsa/commands/override.py` | modify | `apply_override` 加 `clear` | Slice 4 |
| `src/bsa/cli.py` | modify | `_cmd_override` 加 `--clear` + 互斥 | Slice 4 |
| `CLAUDE.md` | modify | 不变量 #5 表述澄清 | Slice 5 |
| `openspec/specs/decision/spec.md` | modify（随归档 delta） | 不变量 #5 澄清同步进主 spec | Slice 5 |
| `tests/test_projection.py` | modify | 投影拓扑字段单测 | Slice 1 |
| `tests/test_git_service.py` | modify | patch_id/diff_stat 单测 | Slice 2 |
| `tests/test_rules.py` | modify | cause 单测 | Slice 3 |
| `tests/test_override.py` | modify | 键缺失/clear 单测 | Slice 4 |
| `tests/test_sync_decision.py`（或等价） | modify | sync_decision 键缺失单测 | Slice 4 |

## Implementation Slices

### Slice 1: TaskState 补齐 sources/targets

- 来源：`reporting` / 投影含完整拓扑字段 + tasks T1
- 目标：`detect_commits` 已算好 `sources`/`targets`（`nodes.py:347-348`），但 `TaskState` TypedDict 未声明，LangGraph 静默丢弃。声明后完整拓扑（含零检出源）落盘，web 才能区分「零检出」与「漏扫」。
- 依赖：无
- 改动文件：
  - Modify: `src/bsa/graph/state.py`
  - Test: `tests/test_projection.py`
- TDD 计划：
  1. 先写失败测试：构造含零检出源的 matrix，跑 detect_commits 后断言投影 `state.json` 顶层 `sources`/`targets` 含完整全集（含零检出源名）。
  2. 最小实现：`TaskState` 加 `sources: list[str]`、`targets: list[str]`。
  3. 边界补充：旧 checkpoint 无该字段时投影 `or []` 兜底不崩。
- 验证命令：
  - `uv run pytest tests/test_projection.py -x`
  - `uv run ruff check src/bsa/graph/state.py`
- 完成标准：`state.json` 顶层 `sources` 含两个源（含 `br_v4.34_MSG_develop_20260805`）、`targets` 含目标；旧周期读投影不崩（`or []`）。
- 风险/回滚：纯 TypedDict 声明，无迁移；回滚 = 删两字段。

### Slice 2: commit_patch / patch_id 统一第一父聚合 + diff_stat

- 来源：`detection`（merge 第一父聚合 diff、patch_id 不再恒空、diff_stat 落盘）+ tasks T2
- 目标：merge commit 有可展示 diff、有互异 patch_id（不再所有 merge 同一恒空值）、有增删行统计；非 merge 语义完全不变。
- 依赖：无
- 改动文件：
  - Modify: `src/bsa/git/service.py`（`commit_patch`、`patch_id`、新增 `diff_stat`）
  - Modify: `src/bsa/domain/models.py`（`CommitInfo.diff_stat`）
  - Test: `tests/test_git_service.py`
- TDD 计划：
  1. 先写失败测试：merge `commit_patch` 非空、merge `patch_id` 互异（两个不同 merge 不同值）、非 merge `patch_id` 与旧实现一致、根 commit 返回 None/空、`diff_stat` 产出 `{files, insertions, deletions}`（超大 commit 超限不丢）。
  2. 最小实现：`commit_patch`/`patch_id` 改 `git diff-tree --no-commit-id -p -r <first_parent> <sha>`；新增 `diff_stat` 用 `diff-tree --numstat -r <first_parent> <sha>`。
  3. 边界：`_too_big`/`_numstat` 判大逻辑不动；根 commit `_parent_sha` 返回 None 时维持 `""`/None。
- 验证命令：
  - `uv run pytest tests/test_git_service.py -x`
  - `uv run ruff check src/bsa/git/service.py src/bsa/domain/models.py`
- 完成标准：merge `patch_text`/`diff_stat` 非空、`patch_id` 互异；非 merge `patch_id`/fingerprint/台账语义不变。
- 风险/回滚：非 merge 经实测 `git show` 与 `diff-tree -p` 字节一致、`_stable_patch_id` 只哈希内容行，故非 merge patch_id 不变；回滚 = revert 该 slice。

### Slice 3: Conclusion4 增加 cause 结构化成因

- 来源：`reporting` / 四态结论透出 cause + tasks T3
- 目标：ManualReview 结论携带 `cause` 枚举，web 据此精确渲染 override 控件，不再靠 evidence 中文文案猜成因。
- 依赖：无
- 改动文件：
  - Modify: `src/bsa/domain/models.py`（`Conclusion4.cause`）
  - Modify: `src/bsa/rules/conclude.py`（各 ManualReview 返回点补 `cause`）
  - Test: `tests/test_rules.py`
- TDD 计划：
  1. 先写失败测试：`conclude_pair` 7 个 ManualReview 返回点各带正确 `cause`；非 ManualReview（NeedSync/AlreadyIncluded/OutOfScope）`cause=None`。
  2. 最小实现：`Conclusion4` 加 `cause: Literal[...] | None = None`；7 个 ManualReview 返回点补值（`pending`/`severity_gate`/`fix_missing`/`function_renamed`/`similarity_gray`/`symbols_missing`/`unknown_branch_type`）。
  3. 边界：`cause` 经 `model_dump()` 自动进 `decisions`，投影无需改。
- 验证命令：
  - `uv run pytest tests/test_rules.py -x`
  - `uv run ruff check src/bsa/rules/conclude.py src/bsa/domain/models.py`
- 完成标准：4 个 ManualReview `cause == "pending"`（本周期 merge 走 `needs_agent`/`pending` 路径）；非 ManualReview cause 为 null；web 侧（change2）据此给对控件。
- 风险/回滚：纯标注字段，不改判定结果/优先级；回滚 = revert 该 slice。

### Slice 4: override 语义收紧（classify + sync_decision + clear + CLI）

- 来源：`decision`（只写 risk 不覆盖、清除覆盖判定）+ tasks T4
- 目标：`is_bug_fix` 键缺失不再被当 `false`（classify 与 sync_decision 两层）；`--clear` 可回退 override；只写 risk 不误判、不短路 LLM。
- 依赖：无
- 改动文件：
  - Modify: `src/bsa/rules/classify.py`、`src/bsa/agents/sync_decision.py`、`src/bsa/commands/override.py`、`src/bsa/cli.py`
  - Test: `tests/test_override.py`、`tests/test_sync_decision.py`（或等价）
- TDD 计划：
  1. 先写失败测试：只写 risk 的 judgment 不再误判 `false`（classify 层）、sync_decision 层 `_from_entry` 键缺失不短路 LLM、不把 `needs_agent` 压成 False；显式 false 仍生效；`--clear` 删 sha + 现算 fp 键、与写字段互斥报错。
  2. 最小实现：`classify_commit` 区分「显式 false」与「键缺失」；`sync_decision.run`/`_from_entry` 键显式存在才短路；`apply_override` 加 `clear` 分支；CLI 加 `--clear`。
  3. 边界：`--clear` 时 sha 不可达、patch_id 查不出则降级只删 sha、接受 fp 残留。
- 验证命令：
  - `uv run pytest tests/test_override.py tests/test_sync_decision.py -x`
  - `uv run ruff check src/bsa/rules/classify.py src/bsa/agents/sync_decision.py src/bsa/commands/override.py src/bsa/cli.py`
- 完成标准：`bsa override <sha> --risk high` 不误判 not-bug-fix、pending commit 不被 Agent 层短路；`--clear` 回到未判定、下次决策 LLM 重判。
- 风险/回滚：改动只影响 judgments 写读语义，不改判定结果本身；回滚 = revert 该 slice。

### Slice 5: 不变量 #5 表述澄清

- 来源：`decision` / 人工覆盖与四态结论分层边界 + tasks T5
- 目标：澄清「人工覆盖 > 规则 > LLM」的作用层——override 作用于 classify 层（is_bug_fix/risk），四态客观结论不接收 override，人工拍板四态走「确认继续/放弃」。
- 依赖：Slice 4（澄清其落地语义）
- 改动文件：
  - Modify: `CLAUDE.md`（不变量 #5）
  - Modify: `openspec/specs/decision/spec.md`（随归档 delta）
- TDD 计划：
  1. 核对 CLAUDE.md 不变量 #5 与 decision spec 的 `人工覆盖与四态结论的分层边界` 场景一致。
  2. 更新 CLAUDE.md 表述 + decision spec delta 已含该场景。
- 验证命令：
  - `uv run ruff check src`（文档不改代码，确认无新增告警）
- 完成标准：CLAUDE.md 不变量 #5 与 decision spec 表述一致，不再读成「人工能在任何一层推翻一切」。
- 风险/回滚：纯文档；回滚 = revert 文档改动。

## Verification Plan

- 单元/集成：`uv run pytest tests/test_projection.py tests/test_git_service.py tests/test_rules.py tests/test_override.py tests/test_sync_decision.py`，再全量 `uv run pytest`。
- 静态：`uv run ruff check src`。
- 结果导向验收（web 数据完整性及格线，重跑 `cycle-2026-09-01` 后走查）：
  1. `state.json` 顶层 `sources` 含两个源（含零检出 `br_v4.34_MSG_develop_20260805`）、`targets` 含目标。
  2. 6 个 merge commit 的 `diff_stat`/`patch_text` 非空、`patch_id` 互异；web 提交详情页能看到 merge 改动内容与统计。
  3. 4 个 ManualReview 的 `decisions[sha][target].cause == "pending"`。
  4. `bsa override <sha> --risk high` 不把该 commit 误判 not-bug-fix；`--clear` 回到未判定。
- 存量降级（不重跑，直接打开旧 `cycle-2026-09-01` 的 state.json）：`sources`/`targets`/`cause`/`diff_stat` 缺失时投影不崩、web 兜底不 404（由 change2 验证）。

## Blockers / Clarifications

- 无。

## Superpowers Handoff

- `writing-plans` 必须基于本文件生成 `docs/superpowers/plans/YYYY-MM-DD-fix-projection-and-override-semantics.md`
- 详细实现计划必须把本文件的 `## Project Context` 与 `## Applicable OpenSpec Rules` 复制/压缩到 plan header 或专门的「项目规则」章节，使后续 `executing-plans` 不需要重新读取 OpenSpec 也能遵守项目规范。
- 详细实现计划必须遵循 `openspec/config.yaml` 的 `language.artifacts: zh-CN`：自然语言标题、段落、任务名和步骤说明使用中文（模板骨架中文化：`目标`/`架构`/`技术栈`/`文件结构`/`项目规则`/`任务`/`步骤`/`自检`），代码标识符、路径、命令、事件名、OpenSpec schema 标题和协议关键字保持原文。
- 详细实现计划必须包含 Superpowers plan header 对应信息（目标/架构/技术栈）+ 文件结构 + 2-5 分钟 checkbox 步骤 + RED-GREEN-REFACTOR 测试节奏 + 精确验证命令 + 自检。
- 详细实现计划不得出现 TBD/TODO/「适当处理」/「类似上一步」等占位话术；每个 slice 展开为 checkbox 步骤。
- 详细实现计划不得省略 Source Coverage 中的任何验收点。
- **结果导向约束**：每个 slice 的「完成标准」必须落到 web 可观察结果（数据完整、操作合理），单测绿只是必要非充分条件；验收以「重跑真实周期 + 打开 web 走查」为准。
