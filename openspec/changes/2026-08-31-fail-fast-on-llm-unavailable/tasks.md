# Tasks: fail-fast-on-llm-unavailable

## 1. 引擎：fix_build 识别 unresolvable 立即停

- 文件：`src/bsa/graph/nodes.py`（`fix_build` 节点）、`src/bsa/graph/workflow.py`
  （`_make_route_after_fix_build`）
- 改动：`fix_build` 拿到 `attribution` 后，若 `attribution.category == "unresolvable"`
  或 `not attribution.files_to_fix`（无法进入修复循环），则：
  - 不 `agent_attempts+1`、不调用 `build_commit` 重编译；
  - 写 `stop_reason`（如「LLM 不可用，无法自动修复」/「归因无法修复」）；
  - 返回一个状态使路由立即走 `fail_fast`（或 `_END_NODE` 直接转报告）。
- 验证：新增单测——stub `build_agent.fix()` 返回 `unresolvable` 时，断言
  `agent_attempts` 不虚增、无第二次 `build_commit` 调用、状态路由到 fail_fast。

## 2. 引擎：agent_attempts 语义回归

- 文件：`src/bsa/graph/nodes.py`（`fix_build`）
- 改动：`agent_attempts = failed.agent_attempts + 1` 只在 `files_to_fix` 非空
  （真正进入修复循环）时执行；空转不再计数。
- 验证：单测覆盖「LLM 不可用 → agent_attempts 保持 0」与「正常修复 → agent_attempts 递增」。

## 3. 回滚说明

- 改动仅收敛「LLM 不可用」的失败路径：从「空转重编译 3 次」改为「立即 fail-fast」，
  不改降级安全默认（仍转人工、不崩溃）、不改成功路径。
- 回滚 = revert 引擎提交；无数据迁移。

## 验证命令

```bash
uv run pytest tests/test_graph_nodes.py tests/test_workflow.py
uv run ruff check src
```

## 待设计确认

1. `unresolvable` 的两条来源（LLM 不可用 vs LLM 正常但「无法归因」）是否都立即 fail-fast
   ——建议都停（二者均无 `files_to_fix`，无法进入修复循环）。
2. 立即停时的状态：走 `fail_fast`（保留关联判断语义）还是 `_END_NODE`（直接转报告）。
   建议走 `fail_fast`，与「编译 3 轮无法解决」的语义对齐。
