# reporting Specification (delta)

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
