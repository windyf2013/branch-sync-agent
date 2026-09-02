# reporting Specification Delta

## MODIFIED Requirements

### Requirement: 结构化状态投影 CLI

提供只读投影命令，将周期结构化状态输出为 JSON，供平台消费。

#### Scenario: 已完成周期投影

- **当** 调用 `bsa report <cycle> --json` 且该周期已完成
- **则** 输出完整结构化状态：周期信息、检测 commits、四态结论、各目标分支结果（状态/commits/cherry-pick/冲突解决/build 结果/patch 路径/工作现场路径）、Action Required 列表
- **且** 输出遵循领域模型结构（pydantic model_dump），不含 LangGraph checkpoint 内部表示
- **且** 运行中周期仅返回 `{status: running}`，不提供详情

#### Scenario: 投影数据源为结构化 state.json（G11）

- **当** 周期/sync/rerun 执行结束
- **则** 把结构化投影（pydantic model_dump）写入 `<log_dir>/<cycle_id>/state.json`
- **且** `bsa report <cycle> --json` 优先读 state.json；state.json 缺失/损坏回退读
  checkpoint 投影（兼容旧周期）
- **且** 平台只消费 JSON，不解析 LangGraph checkpoint 内部结构（get_tuple/channel_values）

#### Scenario: 投影含完整拓扑字段

- **当** 周期完成并写入 `state.json` / 响应 `bsa report <cycle> --json`
- **则** 顶层 `sources` 为**完整源分支全集**（同源矩阵中所有 `sources` 分支名，含扫描窗口内零检出的源）
- **且** 顶层 `targets` 为**完整目标分支全集**（同源矩阵中所有 `need_sync_targets` 分支名）
- **且** 该全集来自同源矩阵，而非「检测到过 commit 的源/目标」的去重
- **且** 零检出的源分支名必须出现在 `sources` 中，使平台能区分「本期无新 commit（正常）」与「该源被漏扫」

#### Scenario: 拓扑字段缺失的向后兼容

- **当** 读取旧周期（无 `sources`/`targets` 字段）的投影
- **则** 平台侧以空列表兜底，不报错、不崩溃
- **且** 平台可在 `sources` 缺失时从 `detected_commits[].source_branch` / `decisions` 键降级推导（仅能覆盖检出过 commit 的源，零检出源不可推导）

#### Scenario: 四态结论透出 cause 成因

- **当** 周期完成并写入 `state.json` / 响应 `bsa report <cycle> --json`
- **则** `decisions[sha][target]` 的每条 `Conclusion4` 携带 `cause` 字段
- **且** `ManualReview` 结论的 `cause` 为成因枚举（`pending`/`severity_gate`/`fix_missing`/`function_renamed`/`similarity_gray`/`symbols_missing`/`unknown_branch_type`）
- **且** `NeedSync`/`AlreadyIncluded`/`OutOfScope` 结论的 `cause` 为 null
- **且** 平台据此精确渲染人工项操作控件（无需从 `evidence` 中文文案猜测成因）
