# 实现计划：add-branch-sync-agent

> 本文件记录已完成的实现计划（OpenFlow spec → 实现追溯）。实现已全部完成并合入 master（commit bf00cb7，498 tests 通过）。

## 来源
- 项目配置：openspec/config.yaml
- 提案：openspec/changes/add-branch-sync-agent/proposal.md（决策 1-41，含 grill 决策记录）
- 设计：openspec/changes/add-branch-sync-agent/design.md（含模块接口契约）
- 规格：openspec/changes/add-branch-sync-agent/specs/（6 能力 delta）
- 任务：openspec/changes/add-branch-sync-agent/tasks.md（批次 0-3，15 任务）

## Project Context
- Branch Sync Agent：定期检测 RCIOS 代码库分支合入的 bug-fix commit，判断是否同步到其他目标分支；需要时在独立 worktree 中 cherry-pick、解决冲突、多型号编译验证、生成 diff-patch，输出 HTML 报告 + 邮件。
- 技术栈：Python 3.13 + uv；LangGraph 主图（单周期=单 thread_id=单 checkpoint）；SQLite checkpoint；langchain-openai（api 后端，可选 claude_cli）；pydantic v2；pydantic-settings；PyYAML。
- 目标仓库：嵌入式 C（RCIOS），GitLab SSH；branch.md 同源矩阵（决策 35 修订：同产品线 section 内 develop/release/fix 全互联传播 bug-fix）。
- 编译：docker 容器 rcios-build-env:ubuntu24.04（已补 bzip2/xz/unzip/rsync/gawk/kmod/cpio/dtc/lz4），--user 宿主机 uid:gid，挂载宿主 ~/.ssh；RTL9617C_build_<customer>.sh <型号>（如 2600_CMCC）。
- 安全：五道闸门（能力/正确性/禁止规则/审批事后/审计），Agent 永不推送。

## Applicable OpenSpec Rules
- specs：引用现有 spec；每条需求有具体场景和验收条件。
- design：保持架构模式（Workflow 管流程、Agent 管判断、Tool 管执行）；移植参考逻辑不迁就方案。
- tasks：精确文件、测试、验证命令、回滚说明。
- implementation：node 出错不得崩溃（node_wrapper→state.errors）；LLM 有 timeout/重试/降级安全默认；路径/密钥不硬编码。
- artifacts：人类可读产物用 zh-CN；OpenSpec 标题/命令/代码标识符保持原文。

## Goal
- 完整周期：定时检测 → 决策四态（NeedSync/AlreadyIncluded/ManualReview/OutOfScope）→ 需同步则逐目标分支 cherry-pick + 冲突解决 + 多型号编译 + patch → HTML 报告 + 邮件。Agent 永不推送，patch 供人工应用。

## Non-Goals
- 不推送分支到远程（能力边际硬约束）。
- v2 web 平台不做，仅预留存储/路径演进。
- 多 repo 管理不做，配置结构预留。
- feature/personal 分支不参与跨分支同步（只回归父分支）。

## Source Coverage

| OpenSpec 来源 | 验收点 | 对应实现 |
|---------------|--------|----------|
| specs/detection | 固定时间窗、同源矩阵（决策35全互联）、commit 元数据、跨周期去重、fetch 容错 | src/bsa/graph/nodes.py detect_commits + rules/branch_md.py |
| specs/decision | 四态由规则输出、规则先行+LLM兜底、classify 完整移植、决策先行固化批次 | src/bsa/rules/classify.py + conclude.py + agents/sync_decision.py |
| specs/sync-execution | worktree、cherry-pick（EMPTY也build）、Conflict Agent、多型号编译、Build Agent、fail-fast、patch | src/bsa/graph/* + agents/conflict.py + build_agent.py + build/runner.py |
| specs/reporting | HTML报告三区块、邮件、Action Required | src/bsa/report/ + mail/ |
| specs/safety | 五道闸门、git白名单、文件编辑安全、红线数据驱动、配置隔离 | src/bsa/executor/whitelist.py + rules/safety.py |
| specs/reliability | 节点容错、LLM降级、命令容错、checkpoint恢复、周期级失败 | src/bsa/graph/workflow.py + agents/base.py + scheduler/cycle.py |
| tasks.md 批次 0 | domain/config/executor | src/bsa/domain/ config/ executor/ |
| tasks.md 批次 1 | rules/git/build/mail | src/bsa/rules/ git/ build/ mail/ |
| tasks.md 批次 2 | LLMClient + 3 Agent | src/bsa/agents/ |
| tasks.md 批次 3 | graph/cli/scheduler + bridge sender | src/bsa/graph/ cli.py scheduler/ mail/bridge_sender.py |

## File Responsibility Map

| 文件 | 操作 | 责任 |
|------|------|------|
| src/bsa/domain/models.py | create | 数据模型（CommitInfo/SyncDecision/Conclusion4/...） |
| src/bsa/config/settings.py | create | 配置（env注入，llm_backend 双后端可选） |
| src/bsa/executor/*.py | create | 命令抽象 + git 白名单 + 异常分层 |
| src/bsa/rules/*.py | create | classify/conclude/safety/branch_md/snapshot/decision_rules |
| src/bsa/git/service.py | create | GitService（worktree/cherry-pick/format-patch） |
| src/bsa/build/runner.py | create | docker 编译 + log 解析（fatal陷阱/三查） |
| src/bsa/agents/*.py | create | LLMClient + SyncDecision/Conflict/Build Agent |
| src/bsa/graph/*.py | create | LangGraph 主图 + node_wrapper + factory |
| src/bsa/report/renderer.py | create | HTML 报告 + 邮件正文 |
| src/bsa/mail/bridge_sender.py | create | 复用参考 bridge 发真实邮件 |
| src/bsa/cli.py | create | 4 命令（run-cycle/status/manual-scan/validate-config） |
| src/bsa/scheduler/cycle.py | create | 周期编排 + checkpoint |

## Implementation Slices（已交付，按批次）

### Slice 0: 底层三模块（批次 0）
- domain/config/executor 完成，含异常分层、命令白名单、FakeExecutor。
- 验证：tests/test_domain_models.py + test_config.py + test_executor.py 通过。

### Slice 1: 服务层（批次 1）
- rules（classify/conclude 移植 + 决策35全互联矩阵 + 严重性门控）+ git + build + mail。
- 验证：tests/test_rules.py + test_git_service.py + test_build.py + test_mail.py 通过。

### Slice 2: Agent 子图（批次 2）
- LLMClient（双后端互斥 api/claude_cli）+ SyncDecision（SHA缓存+人工覆盖）+ Conflict（快照回滚+安全闸门）+ Build（四类归因）。
- 验证：tests/test_llm_client.py + test_sync_decision_agent.py + test_conflict_agent.py + test_build_agent.py 通过。

### Slice 3: graph + cli + scheduler（批次 3）
- LangGraph 主图（node_wrapper 容错、checkpoint、fail-fast）+ factory + cli + scheduler + report + bridge_sender。
- 验证：tests/test_graph_nodes.py + test_workflow.py + test_cli_smoke.py + test_bridge_sender.py 通过。

## Verification Plan
- 单元/集成验证：uv run pytest（498 passed）
- 格式验证：uv run ruff check src/ tests/（clean）
- 配置验证：uv run bsa validate-config（exit 0）
- 真机验证：完整同步执行链路跑通（检测→决策 NeedSync→worktree→cherry-pick→编译→patch→报告）；编译推进到 strongswan 组件（业务仓库问题，用户处理）

## Blockers / Clarifications
- 无阻塞项。真机测试发现并修复 15 个真实问题（同步方向模型修订为决策35/36/41、编译链路 docker/ssh/型号配置等）。

## Superpowers Handoff
- 实现已全部完成并合入 master（commit bf00cb7，498 tests 通过）。
- 本 plan-ready 记录实现依据与验证结果，供追溯。
