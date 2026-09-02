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

#### Scenario: 周期记录枚举覆盖 scan 周期

- **当** 存在 `manual-scan` 落盘的 `scan-*` 周期（目录含 `cycle.json`）
- **则** `list_cycle_records` 同时枚举 `cycle-*` 与 `scan-*` 两类周期，按 `started_at` 升序混排
- **且** `manual-*`/`rerun-*` 目录（不写 `cycle.json`）不被枚举
- **且** `latest_completed_cycle` / `list_cycles` / `latest_cycle_start` 据此能取到 `scan-*` 周期
- **且** `bsa report <scan-id> --json` 对已完成的 `scan-*` 周期返回完整终态，不误判 `{status: running}`

#### Scenario: scan 周期终态折叠正确

- **当** `manual-scan` 执行结束（`cycle.json` 写 `SUCCESS`）
- **则** `_cycle_terminal_status` 能读到 `scan-*` 周期的 `cycle.json` 终态
- **且** `register_finish` 据此记 `succeeded`，不再因读不到记录而回退 `UNKNOWN`→`failed`

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
