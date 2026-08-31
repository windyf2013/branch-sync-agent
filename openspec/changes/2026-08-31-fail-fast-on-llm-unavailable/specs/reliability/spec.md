# reliability Specification Delta

## MODIFIED Requirements

### Requirement: LLM 容错与降级

LLM 失败重试后降级安全默认，确定性流程仍可运行。

（新增/强化场景：build 归因在 LLM 不可用时的降级必须**立即**转人工，不空转重编译）

#### Scenario LLM 调用失败或不可用。

- **当** LLM 调用超时/失败
- **则** 指数退避重试（max_retries 可配置）
- **且** 重试仍失败则降级安全默认：sync_decision → ManualReview、conflict → 转人工、build → 归因无法修复
- **且** LLM 完全不可用时确定性流程（检测/判定/报告）仍可运行，降级全人工审核

#### Scenario: build 归因 LLM 不可用时立即转人工

- **当** `classify_build_error` 因 LLM 不可用返回 `category="unresolvable"`（或抛出）
- **则** `fix_build` 节点**立即**标记 `stop_reason`（LLM 不可用，无法自动修复）并路由 fail-fast
- **且** **不**进入重编译循环、**不**空转 `agent_attempts`（当前实现会空转重编译 3 次）
- **且** `agent_attempts` 只在真正进入 `_fix_loop`（`files_to_fix` 非空）时累加，语义为「真实修复尝试次数」而非「fix_build 节点调用次数」
