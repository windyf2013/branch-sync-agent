# Capability: reporting

## ADDED Requirements

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
