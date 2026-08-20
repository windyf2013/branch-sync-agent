# Tasks: add-branch-sync-agent

> 实现按依赖批次组织（批次内并行，批次间严格先后）。每个任务对应一个 subagent 派发单元，契约见 `design.md` 模块接口契约。

## 批次 0：底层三模块（可并行）

### Task 0.1: domain 数据模型
- 文件：`bsa/domain/models.py` + `tests/test_domain_models.py`
- 内容：pydantic 模型（CommitInfo / SyncDecision / Conclusion4 / ConflictResolution / BuildOutcome / CommitResult / BranchResult / ErrorRecord / Report），校验 Literal 枚举与必填字段
- 契约：见 design.md 批次 0 domain
- 验证：`uv run pytest tests/test_domain_models.py`
- 依赖：无（纯 pydantic，零外部依赖）

### Task 0.2: config 配置
- 文件：`bsa/config/settings.py` + `tests/test_config.py`
- 内容：pydantic-settings Settings + load_settings()，环境变量注入，必填校验，无硬编码
- 契约：见 design.md 批次 0 config
- 验证：`uv run pytest tests/test_config.py`
- 依赖：无

### Task 0.3: executor 命令抽象层
- 文件：`bsa/executor/{base,subprocess,whitelist,fake}.py` + `tests/test_executor.py`
- 内容：CommandExecutor Protocol + SubprocessExecutor + WhitelistExecutor（git 白名单 + SafetyViolation）+ FakeExecutor；异常分层 BsaError/SafetyViolation
- 契约：见 design.md 批次 0 executor
- 验证：`uv run pytest tests/test_executor.py`
- 依赖：无

## 批次 1：服务层（可并行，依赖批次 0）

### Task 1.1: rules 规则层
- 文件：`bsa/rules/{classify,conclude,safety,branch_md}.py` + `bsa/rules/decision_rules.yaml` + `bsa/rules/safety_rules.yaml` + `tests/test_rules.py`
- 内容：classify/conclude 移植参考实现逻辑（完整移植 + golden-set 测试）；SafetyEnforcer 数据驱动校验；branch_md 同源矩阵解析 + feature/personal 排除
- 契约：见 design.md 批次 1 rules
- 验证：`uv run pytest tests/test_rules.py`（含参考移植的 golden-set 用例）
- 依赖：Task 0.1（domain 模型）

### Task 1.2: git 服务层
- 文件：`bsa/git/service.py` + `tests/test_git_service.py`
- 内容：GitService（注入 executor），fetch/窗口扫描/元数据/大 commit 保护/patch-id/worktree/cherry-pick/unmerged/快照回滚/format_patch
- 契约：见 design.md 批次 1 git
- 验证：`uv run pytest tests/test_git_service.py`（用 FakeExecutor，零真实命令）
- 依赖：Task 0.1 + Task 0.3

### Task 1.3: build 服务层
- 文件：`bsa/build/runner.py` + `bsa/build/log_parser.py` + `tests/test_build.py`
- 内容：BuildRunner（docker exec 封装，注入 executor）+ log_parser（extract_errors 20 行上下文 ≤3000 行、has_success_marker 三查）+ 用 spec/rcios-compiling-log-info.md 样例做测试
- 契约：见 design.md 批次 1 build
- 验证：`uv run pytest tests/test_build.py`（用真实日志样例 + FakeExecutor）
- 依赖：Task 0.1 + Task 0.3

### Task 1.4: mail 服务层
- 文件：`bsa/mail/service.py` + `tests/test_mail.py`
- 内容：MailService（sender 可注入，dry-run 默认，失败仅告警）
- 契约：见 design.md 批次 1 mail
- 验证：`uv run pytest tests/test_mail.py`
- 依赖：Task 0.1

## 批次 2：Agent 子图（可并行，依赖批次 1）

### Task 2.1: LLMClient 统一封装
- 文件：`bsa/agents/base.py` + `tests/test_llm_client.py`
- 内容：LLMClient（langchain-openai ChatOpenAI，timeout/重试/降级安全默认；judge_bug_fix / solve_conflict / classify_build_error / judge_failfast_related）
- 契约：见 design.md 批次 2 agents/base
- 验证：`uv run pytest tests/test_llm_client.py`（mock LLM）
- 依赖：Task 1.1 + Task 1.2 + Task 1.3

### Task 2.2: SyncDecisionAgent
- 文件：`bsa/agents/sync_decision.py` + `tests/test_sync_decision_agent.py`
- 内容：pending commit → is_bug_fix 判定，按 SHA 缓存 + 人工覆盖优先
- 契约：见 design.md 批次 2 sync_decision
- 验证：`uv run pytest tests/test_sync_decision_agent.py`
- 依赖：Task 2.1

### Task 2.3: ConflictAgent
- 文件：`bsa/agents/conflict.py` + `tests/test_conflict_agent.py`
- 内容：冲突解决循环（快照/LLM/Apply/校验/回滚，max_attempts=3，SafetyEnforcer 强制，质量底线）
- 契约：见 design.md 批次 2 conflict
- 验证：`uv run pytest tests/test_conflict_agent.py`
- 依赖：Task 2.1 + Task 1.2

### Task 2.4: BuildAgent
- 文件：`bsa/agents/build_agent.py` + `tests/test_build_agent.py`
- 内容：编译错误归因四类 + 修复循环（仅本次引入，max_attempts=3，SafetyEnforcer 强制）
- 契约：见 design.md 批次 2 build_agent
- 验证：`uv run pytest tests/test_build_agent.py`
- 依赖：Task 2.1 + Task 1.3

## 批次 3：graph + cli + scheduler（串行，依赖批次 2）

### Task 3.1: graph state + node_wrapper
- 文件：`bsa/graph/state.py` + `bsa/graph/nodes.py`（node_wrapper + 各 node 骨架，依赖批次 0-2 的服务调用）+ `tests/test_graph_nodes.py`
- 内容：TaskState TypedDict + node_wrapper 容错 + 各 node 编排（调用下层服务，只编排无实现）
- 契约：见 design.md 批次 3 graph
- 验证：`uv run pytest tests/test_graph_nodes.py`（mock 服务层）
- 依赖：批次 2 全部

### Task 3.2: 主图 workflow + checkpoint
- 文件：`bsa/graph/workflow.py` + `tests/test_workflow.py`
- 内容：build_workflow（conditional edges + SqliteSaver checkpoint，thread_id=cycle_id）+ 集成测试
- 契约：见 design.md 批次 3 graph/workflow
- 验证：`uv run pytest tests/test_workflow.py`
- 依赖：Task 3.1

### Task 3.3: cli + scheduler
- 文件：`bsa/cli.py` + `bsa/scheduler/cycle.py` + `tests/test_cli_smoke.py`
- 内容：cli 入口（run-cycle/status/manual-scan(--since/--until 覆盖默认窗口)/validate-config）+ run_cycle 周期编排 + 周期产物落盘（run.log / decisions.json / build.log / patch）
- 契约：见 design.md 批次 3 cli+scheduler
- 验证：`uv run pytest tests/test_cli_smoke.py` + `uv run bsa run-cycle --dry-run`
- 依赖：Task 3.2

## 验收标准（全批次完成后）

1. `uv run pytest` 全部通过
2. `uv run ruff check .` 通过
3. `uv run bsa validate-config` 通过（配置校验）
4. `uv run bsa run-cycle --dry-run` 能跑通完整周期（mock 服务层）
5. 真实环境项（docker/sudo/SSH/SMTP）待运维配置后做真实空跑
