# Proposal: fail-fast-on-llm-unavailable

## Why

可用性深挖发现一个真实缺陷：**LLM 不可用时，build 修复链路在空转烧时间，而非立即转人工。**

代码链（已核实）：
1. `fix()` 第一步调 `self._llm.classify_build_error(ctx)`；LLM 不可用且 `llm_degrade_to_manual=True`
   时返回 `category="unresolvable", files_to_fix=[]`（`base.py:285-292`）。
2. `files_to_fix=[]` → `_fix_loop` 根本不进入，`fix_diff` 恒 `None`（`build_agent.py`）。
3. `fix_build` 节点仍记 `agent_attempts = failed.agent_attempts + 1`，并**再调一次
   `build_commit` 重新编译**（什么都没改，必然还是 FAILED）（`nodes.py:895-928`）。
4. 路由 `_make_route_after_fix_build` 只看 `agent_attempts >= max_build_attempts`，**不看
   `reason == "LLM 不可用"`**，于是回到 `fix_build` 再空转一轮（`workflow.py:342-352`）。

结果：`agent_attempts=3` 的真实含义是「空转 3 次 + 同一错误重新编译 3 次」，不是「修复尝试 3 次」。
实测 8/31 一批任务跑 144 分钟——正常编译约 27 分钟，LLM 不可用却空转重编译约 2 轮，白烧编译时间。

这与 CLAUDE.md 不变量 #6「LLM 不可用 = 节点失败 = 转人工」直接矛盾：当前实际是
「LLM 不可用 = 空转 3 次重编译」。

## What Changes

- **LLM 不可用立即 fail-fast**：`fix_build` 节点在 `attribution.category == "unresolvable"`
  且 `reason` 含「LLM 不可用」时，不再 `agent_attempts+1` 重编译，而是直接标记
  `stop_reason`（如「LLM 不可用，无法自动修复」）并路由到 `fail_fast`。
- **`agent_attempts` 语义回归真实**：只有真正进入 `_fix_loop`（`files_to_fix` 非空）才累加
  尝试次数；空转不再计数，避免误导「尝试了 N 次」。
- **（可选，若仍要留痕）** LLM 不可用时落一条 `reason` 到 outcome（已有），确保详情页能
  显示「LLM 不可用」这个根因（当前已透出，见 `_target_detail.html` 的 `o.reason`）。

明确**不做**：不改 LLM 降级安全默认（仍转人工）；不重试 LLM（保持不变量 #6）；不引入
「修复尝试日志」落盘（那是我此前提案的错误前提——LLM 不可用时根本没有修复尝试，只有空转）。

## Capabilities

### Modified Capabilities

- `reliability`: 修正 LLM 不可用的 build 失败路径——立即 fail-fast 转人工，不空转重编译；
  `agent_attempts` 语义回归「真实修复尝试次数」。
- `sync-execution`: `fix_build` 节点对 `unresolvable`（LLM 不可用）不再进入重编译循环。

## Impact

- 引擎：`bsa/graph/nodes.py`（`fix_build` 识别 LLM 不可用 → 直接 stop_reason + 路由）、
  `bsa/graph/workflow.py`（`_make_route_after_fix_build` 或新增状态分支）、
  `bsa/agents/base.py`（可选：明确 unresolvable 语义）。
- 平台：`bsa_web/templates/_target_detail.html` 已有 `o.reason` 透出，无需改。
- 数据：`agent_attempts` 不再虚增；`stop_reason` 新增「LLM 不可用」文案。
- 测试：新增 graph 节点单测（LLM 不可用 → 单次即 fail-fast，agent_attempts 不虚增）、
  路由单测；全量 pytest + ruff 通过。

## 待定项（设计阶段敲定）

1. `unresolvable` 的两条来源（LLM 不可用 vs LLM 正常但判断「无法归因」）是否都立即 fail-fast，
   还是仅「LLM 不可用」这条？——LLM 正常但「无法归因」也应立即转人工（没有 files_to_fix，
   无法进入 _fix_loop，同样空转），建议都 fail-fast。
2. fail-fast 的 `stop_reason` 文案统一（与现有 fail-fast 语义对齐）。
3. `agent_attempts` 累加时机：从「fix_build 每次调用」改为「_fix_loop 真正跑了一次」。
