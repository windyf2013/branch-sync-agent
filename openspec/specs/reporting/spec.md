# reporting Specification

## Purpose
TBD - created by archiving change add-branch-sync-agent. Update Purpose after archive.

## Requirements

### Requirement: HTML 报告

周期结束生成三区块 HTML 报告。
#### Scenario 周期结束生成 HTML 报告。
- **当** 周期结束（全部完成或提前失败结束）
- **则** 生成 HTML 报告，三区块：Action Required（人工要动手）/ 已成功同步 / 检测信息
- **且** 以参考模板为基础，扩展执行阶段（cherry-pick 结果、编译矩阵、Agent 修复记录、patch 路径）
- **且** 报告含全部四态结论明细、无需同步的 commit 及理由（审计闭环）
- **且** 页脚声明"不自动推送变更"

### Requirement: 邮件通知

周期末统一发送一封报告邮件，空检也发。
#### Scenario 发送周期报告邮件。
- **当** 周期结束
- **则** 周期末统一发一封邮件（一个周期一封），空检也发
- **且** 邮件正文显示必要摘要（窗口/分支数/commit 数/结论计数/警告），HTML 附件承载明细
- **且** 邮件支持 dry-run（验真不发送），真实 SMTP 可配置
- **且** 邮件失败仅告警，不影响已生成报告

### Requirement: Action Required 区块

报告展示需人工处理项与推送命令建议。
#### Scenario 报告展示需人工处理项。
- **当** 存在停批分支 / 未修复 commit / forbidden_paths 命中项 / ManualReview 项
- **则** 在 Action Required 区块列出，含 Agent 修改 diff 与失败原因
- **且** 提供推送命令建议（人工执行，Agent 不推送）

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

### Requirement: 候选 commit 只读 CLI

提供按源分支列出候选 commit 的只读命令，供平台新建同步选择。

#### Scenario: 列出源分支候选 commit

- **当** 调用 `bsa commits <src> [--limit N]`
- **则** 返回源分支最近 N 条 commit（默认 50），每条含 sha、提交说明、提交时间
- **且** 列表平铺，不做"已同步/已包含"过滤（用户勾选即信任）
- **且** 命令只读，不写任何状态、不改任何文件

#### Scenario: 运行中/失败容错

- **当** 源分支不存在或 git 命令失败
- **则** 返回非零退出码与错误信息，平台提示"候选 commit 加载失败"
- **且** 不影响平台其他操作
