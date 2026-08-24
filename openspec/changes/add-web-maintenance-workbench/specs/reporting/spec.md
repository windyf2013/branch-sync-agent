# reporting Specification (delta)

## ADDED Requirements

### Requirement: 结构化状态投影 CLI

提供只读投影命令，将周期结构化状态输出为 JSON，供平台消费。

#### Scenario: 已完成周期投影

- **当** 调用 `bsa report <cycle> --json` 且该周期已完成
- **则** 输出完整结构化状态：周期信息、检测 commits、四态结论、各目标分支结果（状态/commits/cherry-pick/冲突解决/build 结果/patch 路径/工作现场路径）、Action Required 列表
- **且** 输出遵循领域模型结构（pydantic model_dump），不含 LangGraph checkpoint 内部表示
- **且** 运行中周期仅返回 `{status: running}`，不提供详情

### Requirement: 投影为只读入口

投影命令只读取，不修改任何状态。

#### Scenario: 只读保证

- **当** 平台经投影读取 V1 数据
- **则** 只经投影 CLI，不直接解析 checkpoint 内部结构
- **且** 投影命令不写任何文件、不改任何状态
