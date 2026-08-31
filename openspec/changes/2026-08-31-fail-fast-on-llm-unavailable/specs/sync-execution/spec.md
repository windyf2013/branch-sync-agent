# sync-execution Specification Delta

## MODIFIED Requirements

### Requirement: Build Agent 修复编译错误

编译失败由 Build Agent 归因并仅修复本次引入的错误。

（新增/强化场景：LLM 不可用或归因「无法修复」时不再空转重编译）

#### Scenario 编译失败。

- **当** 某型号编译失败
- **则** Build Agent 归因分类（本次引入 / 原分支已有 / 环境问题 / 无法修复）
- **且** 仅"本次引入"允许自动修复（仅 commit 文件 + 报错文件，SafetyEnforcer 强制），快照回滚，max_attempts=3
- **且** 疑似"原分支已有"时切原 tip 编译对比确定性验证
- **且** 报错文件命中 forbidden_paths → 转人工
- **且** 用尽后触发 fail-fast 关联判断

#### Scenario: LLM 不可用或归因「无法修复」时立即停

- **当** `fix()` 返回 `category="unresolvable"`（LLM 不可用）或 `files_to_fix` 为空
- **则** 不进入 `_fix_loop`，不重编译，`fix_build` 节点直接产出 `stop_reason`
  「LLM 不可用，无法自动修复」并路由 `fail_fast`
- **且** 该 commit 的 `agent_attempts` 不虚增（当前实现会 +1 并重编译，导致 3 次空转）
- **且** 详情页仍透出 `reason`（LLM 不可用），维护者能一眼看到根因
